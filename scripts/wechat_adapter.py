#!/usr/bin/env python3
"""Official API draft adapter. Native media remains pending until separately verified."""
from __future__ import annotations
import argparse
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
from urllib.parse import urlsplit, unquote
from html.parser import HTMLParser
import re

from publish_core import PublishError, digest, file_hash, read_json, resolve_file, validate, visible_text, write_json
from wechat_media import reconcile, component_info
from wechat_links import project_links
from wechat_identity import image_identity, article_identity, load_link_identities, editor_visible_text


def normalize_legacy_anchors(content, slots):
    """Translate observed old text placeholders in memory; never rewrite the draft."""
    class TextNodes(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.nodes = []
        def handle_data(self, data):
            line, column = self.getpos()
            offset = sum(len(x) for x in content.splitlines(keepends=True)[:line-1]) + column
            self.nodes.append((offset, data))
    parser = TextNodes(); parser.feed(content)
    changes = []
    for slot in slots:
        anchor = slot.get('legacy_anchor')
        if not anchor:
            continue
        hits = [(offset + match.start(), len(anchor)) for offset, data in parser.nodes
                for match in re.finditer(re.escape(anchor), data)]
        token = '【发布媒体:' + slot['id'] + '】'
        if len(hits) > 1 or (hits and token in visible_text(content)):
            raise PublishError('旧媒体占位不唯一或与新占位并存')
        if hits:
            changes.append((*hits[0], token))
    for start, size, token in sorted(changes, reverse=True):
        content = content[:start] + token + content[start+size:]
    return content


class Links(HTMLParser):
    def __init__(self, identities=None):
        super().__init__(); self.images=[]; self.links=[]; self.identities=identities or {}
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='img': self.images.append(image_identity(attrs))
        if tag=='a': self.links.append(article_identity(self.identities.get(attrs.get('href',''),attrs.get('href',''))))


class BodyOrder(HTMLParser):
    def __init__(self, identities=None):
        super().__init__(); self.parts=[]; self.hidden=0; self.identities=identities or {}
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in {'script','style'}: self.hidden+=1
        if not self.hidden:
            if tag=='img': self.parts.append(('image',image_identity(attrs)))
            if tag=='a': self.parts.append(('link',article_identity(self.identities.get(attrs.get('href',''),attrs.get('href','')))))
    def handle_endtag(self,tag):
        if tag in {'script','style'} and self.hidden: self.hidden-=1
        if not self.hidden and tag=='a': self.parts.append(('end-link',))
    def handle_data(self,data):
        if not self.hidden: self.parts.append(data)
    def signature(self):
        # Adjacent text split by harmless spans remains equivalent; media order does not.
        result=[]; text=''
        for item in self.parts+[('end',)]:
            if isinstance(item,str): text+=item
            else:
                normalized=re.sub(r'\s+','',text)
                if normalized: result.append(('text',normalized))
                result.append(item); text=''
        return result


def article_equivalent(expected, actual, identities=None):
    for key in ('title','author','digest','content_source_url','thumb_media_id','need_open_comment','only_fans_can_comment'):
        if key in expected and str(expected[key]) != str(actual.get(key,'')):
            return False
    if editor_visible_text(expected.get('content','')) != editor_visible_text(actual.get('content','')):
        return False
    a,b=Links(identities),Links(identities); a.feed(expected.get('content','')); b.feed(actual.get('content',''))
    x,y=BodyOrder(identities),BodyOrder(identities); x.feed(expected.get('content','')); y.feed(actual.get('content',''))
    return a.images==b.images and a.links==b.links and x.signature()==y.signature()


def credentials(profile, account):
    cfg=profile.get('accounts',{}).get(account,{})
    values={}
    path=cfg.get('wechat_env_file')
    if path:
        for line in Path(path).expanduser().read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key,value=line.split('=',1); values[key.strip()]=value.strip().strip('\"\'')
    app=values.get('WECHAT_APP_ID') or os.environ.get('WECHAT_APP_ID','')
    secret=values.get('WECHAT_APP_SECRET') or os.environ.get('WECHAT_APP_SECRET','')
    if not app or not secret:
        raise PublishError('公众号凭证未配置')
    if hashlib.sha256(app.encode()).hexdigest()!=cfg.get('wechat_appid_sha256'):
        raise PublishError('公众号凭证与显式账号绑定不符')
    return app,secret


def validate_images(manifest, target):
    """Every file the API may upload must be in this target's sealed assets."""
    document = next(d for d in manifest['documents'] if d['id'] == target['document_id'])
    root = Path(manifest['source_root'])
    source = resolve_file(root, document['path'])
    permitted = {resolve_file(root, a['path']):a for a in manifest['assets']
                 if a['id'] in target.get('asset_ids', []) and a['kind'] == 'image'}
    class Images(HTMLParser):
        def __init__(self): super().__init__(); self.refs = []
        def handle_starttag(self, tag, attrs):
            if tag == 'img': self.refs.append(dict(attrs))
    parser = Images(); parser.feed(source.read_text(encoding='utf-8'))
    for item in parser.refs:
        src = item.get('src') or ''
        parsed = urlsplit(src)
        if parsed.scheme == 'https' and parsed.hostname == 'mmbiz.qpic.cn':
            continue
        if src.startswith('data:image/'):
            continue  # Bytes are already part of the sealed document, not another file.
        ref = item.get('data-local-path') or src
        parts = urlsplit(ref)
        if not ref or parts.scheme or parts.netloc or parts.query or parts.fragment:
            raise PublishError('正文图必须为已登记本地图片或微信图片地址')
        path = (source.parent / unquote(ref)).resolve()
        asset = permitted.get(path)
        if asset is None or not path.is_file() or file_hash(path) != asset.get('sha256'):
            raise PublishError('正文图未列入当前目标的封存素材或文件已变化；上传前拒绝')


def preflight_wechat(manifest, target, profile, argv):
    """Resolve local prerequisites without token requests or uploads."""
    if target['content_type'] != 'article' or target['endpoint'] != 'draft':
        raise PublishError('公众号适配器目前仅验收文章草稿')
    if not isinstance(target.get('title'), str) or not target['title'].strip():
        raise PublishError('公众号草稿必须明确非空标题 title')
    assets = {a['id']: a for a in manifest['assets']}
    cover = target.get('cover_asset_id')
    if cover not in assets or cover not in target.get('asset_ids', []) or assets[cover]['kind'] != 'image':
        raise PublishError('公众号草稿缺少已登记的封面 cover_asset_id')
    docs = {d['id']: d for d in manifest['documents']}
    if docs[target['document_id']]['format'] != 'html':
        raise PublishError('公众号草稿需要最终 HTML')
    validate_images(manifest, target)
    for flag in ('--profile',):
        if flag not in argv or argv.index(flag) + 1 >= len(argv):
            raise PublishError(f'公众号适配器缺少 {flag}')
    module = api_module_path(argv)
    if not module.is_file():
        raise PublishError('微信 API 模块缺失')
    from environment import load_profile
    adapter_profile = load_profile(Path(argv[argv.index('--profile') + 1]).expanduser())
    current_account = dict(profile.get('accounts', {}).get(target['account'], {}))
    if current_account.get('wechat_env_file'):
        current_account['wechat_env_file'] = str(Path(current_account['wechat_env_file']).expanduser().resolve())
    if adapter_profile.get('accounts', {}).get(target['account']) != current_account:
        raise PublishError('执行器 profile 与当前账号配置不一致')
    try:
        credentials(profile, target['account'])
    except OSError as exc:
        raise PublishError('公众号凭证文件不可读取') from exc


def visual_verified(receipt,actual,req,remote_id):
    review=receipt.get('layout_review',{})
    if not review: return False
    if receipt.get('manifest_digest')!=req['manifest_digest'] or receipt.get('remote_id')!=remote_id:
        raise PublishError('视觉回执与目标草稿不符')
    if review.get('content_sha256')!=hashlib.sha256(actual.get('content','').encode()).hexdigest():
        raise PublishError('视觉检查对应旧正文；需重新观察现稿')
    if review.get('reviewed') is not True or not review.get('evidence'):
        return False
    for item in review['evidence']:
        path=Path(item.get('path',''))
        if not path.is_absolute() or not path.is_file() or not path.stat().st_size or file_hash(path)!=item.get('sha256'):
            raise PublishError('视觉观察证据缺失或已变化')
    return True


def dispatch(request_path, profile, api, *, action):
    request_path=Path(request_path)
    req=read_json(request_path)
    manifest=read_json(req['manifest_path']); target=req['target']; job=req['job']
    if digest(manifest)!=req['manifest_digest']:
        raise PublishError('请求快照指纹不符')
    if target['platform']!='wechat' or target['content_type']!='article' or target['endpoint']!='draft':
        raise PublishError('此适配器当前只处理公众号文章草稿；不静默升级发表/群发')
    app,secret=credentials(profile,target['account'])
    link_policy=profile.get('accounts',{}).get(target['account'],{}).get('external_link_policy','strict')
    if link_policy not in {'strict','visible_url'}:
        raise PublishError('未知公众号外链策略')
    record_path=request_path.with_suffix('.wechat.json')
    record=read_json(record_path) if record_path.exists() else {}
    if record and record.get('manifest_digest')!=req['manifest_digest']:
        raise PublishError('公众号远端记录版本不符')
    account_fingerprint = hashlib.sha256(app.encode()).hexdigest()
    if record.get('account_appid_sha256') and record['account_appid_sha256'] != account_fingerprint:
        raise PublishError('当前账号与原提交账号身份不符')
    if 'recovery_remote_id' in req and action != 'verify':
        raise PublishError('候选草稿恢复只允许官方读回，禁止上传或新建')

    binding = target.get('existing_draft')
    if binding:
        if action == 'execute':
            raise PublishError('旧草稿绑定禁止上传或新建；使用 adopt 只读核验')
        if job.get('remote_id') != binding['remote_id']:
            raise PublishError('认领草稿与账本身份不符')
        if not record:
            snapshots = read_json(req['document_snapshot_path'])
            expected_id = binding['expected_document_id']
            doc = next(d for d in manifest['documents'] if d['id'] == expected_id)
            raw = snapshots.get(expected_id, '')
            if hashlib.sha256(raw.encode()).hexdigest() != doc['sha256']:
                raise PublishError('认领预期正文快照指纹不符')
            try:
                expected = json.loads(raw)
            except ValueError as exc:
                raise PublishError('认领预期正文不是 JSON') from exc
            if not isinstance(expected, dict) or not expected.get('content'):
                raise PublishError('认领缺原稿预期正文')
            for field, source in (('title','title'),('author','author'),('digest','digest'),('source_url','content_source_url'),
                                  ('open_comment','need_open_comment'),('fans_only_comment','only_fans_can_comment')):
                if field in target and str(target[field]) != str(expected.get(source, '')):
                    raise PublishError('认领目标与原稿预期元数据不符：' + field)
            record = dict(manifest_digest=req['manifest_digest'], expected=expected, account_appid_sha256=account_fingerprint,
                          remote_id=binding['remote_id'], submission_started=True, adopted=True)
            write_json(record_path, record)

    if action=='execute':
        validate(manifest)
        validate_images(manifest, target)
        if not isinstance(target.get('title'), str) or not target['title'].strip():
            raise PublishError('公众号草稿必须明确非空标题 title')
        if record.get('submission_started'):
            raise PublishError('已有建稿尝试，只允许读回')
        doc=next(d for d in manifest['documents'] if d['id']==target['document_id'])
        source=resolve_file(Path(manifest['source_root']),doc['path'])
        cover=next((a for a in manifest['assets'] if a['id']==target.get('cover_asset_id')),None)
        if not cover or cover['kind']!='image':
            raise PublishError('公众号文章必须明确封面素材')
        cover_path=resolve_file(Path(manifest['source_root']),cover['path'])
        body=source.read_text(encoding='utf-8')
        projection=None
        if link_policy=='visible_url':
            original_body=body
            body,_,changes=project_links(body)
            if changes:
                projection=dict(policy='visible_url',source_sha256=hashlib.sha256(original_body.encode()).hexdigest(),changes=changes)
        def upload(token):
            content,images=api.upload_html_images(body,source.parent,token)
            cover_result=api.upload_cover(cover_path,token)
            article=dict(article_type='news',title=target['title'],content=content,
                         thumb_media_id=cover_result['media_id'],need_open_comment=target.get('open_comment',1),
                         only_fans_can_comment=target.get('fans_only_comment',0))
            for key,source_key in (('author','author'),('digest','digest'),('content_source_url','source_url')):
                if target.get(source_key): article[key]=target[source_key]
            return article,images
        expected,images=api._authorized_call(app,secret,upload)
        record=dict(manifest_digest=req['manifest_digest'],expected=expected,images=images,submission_started=True,
                    account_appid_sha256=account_fingerprint)
        if projection: record['link_projection']=projection
        # Persist the mutation window and expected content before the POST.
        write_json(record_path,record)
        remote_id=api._authorized_call(app,secret,lambda token:api.add_draft(token,expected))
        record['remote_id']=remote_id
        write_json(record_path,record)
        return response(req,'pending',remote_id,stage='draft_created')

    recovery_id = req.get('recovery_remote_id')
    if recovery_id is not None:
        if (action != 'verify' or job.get('state') != 'submission_unknown' or job.get('remote_id')
                or not isinstance(recovery_id, str) or not recovery_id.strip()
                or record.get('submission_started') is not True or not record.get('expected')):
            raise PublishError('缺少原提交窗口和预期正文，不能绑定候选草稿')
        if record.get('account_appid_sha256') != account_fingerprint:
            raise PublishError('缺少原提交账号身份绑定，不能自动恢复候选草稿')
    remote_id=recovery_id or job.get('remote_id') or record.get('remote_id')
    if not remote_id:
        raise PublishError('尚无草稿 ID；提交状态未知，禁止再次建稿，需查草稿列表定位')
    if record.get('remote_id') and record['remote_id']!=remote_id:
        raise PublishError('请求和公众号记录的远端身份不符')
    data=api.get_draft(media_id=remote_id,app_id=app,secret=secret)
    articles=data.get('news_item',[])
    if len(articles)!=1:
        raise PublishError('草稿内容数不符')
    actual=articles[0]
    def observed(state, **extra):
        if recovery_id is not None:
            if not (extra.get('article_body_verified') is True or extra.get('content_verified') is True):
                raise PublishError('候选草稿尚未核验正文与封面')
            record['remote_id'] = remote_id
            record['recovery'] = {'method': 'official_draft_get', 'content_sha256': hashlib.sha256(actual.get('content', '').encode()).hexdigest()}
            write_json(record_path, record)
            extra['recovery_verified'] = True
        return response(req, state, remote_id, **extra)
    # Bound to this readback, not a previously observed draft. Consumers can retain
    # the raw evidence without pretending the normalized body is the remote HTML.
    write_json(request_path.with_suffix('.readback.json'), dict(
        manifest_digest=req['manifest_digest'],remote_id=remote_id,article=actual,
        verification_nonce=req.get('verification_nonce')))
    if action=='inspect-media':
        snapshot=dict(manifest_digest=req['manifest_digest'],remote_id=remote_id,article=actual,
                      components=component_info(actual.get('content','')))
        destination=request_path.with_suffix('.native-snapshot.json'); write_json(destination,snapshot)
        return dict(snapshot_path=str(destination),remote_id=remote_id,components=snapshot['components'],
                    content_sha256=hashlib.sha256(actual.get('content','').encode()).hexdigest())
    slots=[s for s in manifest.get('media_slots',[]) if s['document_id']==target['document_id']]
    comparison_content = normalize_legacy_anchors(actual.get('content',''), slots) if binding else actual.get('content','')
    expected=record.get('expected',{})
    identities=load_link_identities(request_path, req['manifest_digest'], remote_id)
    # A historical draft may have lost ONLY its external anchor tags. Keep the
    # original expectation; require the missing URLs to be restored visibly.
    # This is a repair stage, never a successful readback or permission to rewrite.
    if link_policy=='visible_url' and not binding and not record.get('link_projection'):
        projected,stripped,changes=project_links(expected.get('content',''))
        if changes:
            projected_expected=dict(expected,content=projected)
            if not component_info(comparison_content) and article_equivalent(dict(expected,content=stripped),dict(actual,content=comparison_content),identities):
                return observed('pending',stage='external_link_repair_required',
                                link_work=changes,article_body_verified=True,content_verified=False)
            expected=projected_expected
    media_receipt=request_path.with_suffix('.native-media.json')
    receipt=read_json(media_receipt) if media_receipt.exists() else {}
    if slots and media_receipt.exists():
        restored,remaining=reconcile(comparison_content,slots,{a['id']:a for a in manifest['assets']},receipt,
                           manifest_digest=req['manifest_digest'],remote_id=remote_id)
        if not article_equivalent(expected,dict(actual,content=restored),identities):
            raise PublishError('插入媒体后正文、位置或图片链接有差异；不覆盖远端')
        if remaining:
            return observed('pending',stage='native_media_insertion_required',media_work=remaining,
                            article_body_verified=True,content_verified=False)
        if not visual_verified(receipt,actual,req,remote_id):
            return observed('pending',stage='visual_review_required',article_body_verified=True,content_verified=False)
        return observed('draft_ready',content_verified=True,layout_verified=True,
                        verification='official_draft_get_and_observed_media_receipt',
                        remote_media_bytes_verified=False)
    if not expected or not article_equivalent(expected,dict(actual,content=comparison_content),identities):
        raise PublishError('草稿读回内容有差异；需核对正文、媒体和排版，不覆盖远端')
    if slots:
        return observed('pending',stage='native_media_insertion_required',media_slot_ids=[s['id'] for s in slots],
                        article_body_verified=True,content_verified=False)
    if not visual_verified(receipt,actual,req,remote_id):
        return observed('pending',stage='visual_review_required',article_body_verified=True,content_verified=False)
    return observed('draft_ready',content_verified=True,layout_verified=True,verification='official_draft_get_and_visual_review')


def response(req,state,remote_id,**extra):
    t=req['target']
    return dict(manifest_digest=req['manifest_digest'],target_id=t['id'],platform=t['platform'],account=t['account'],
                state=state,remote_id=remote_id,**extra)


def api_module_path(argv):
    """Default to this package; an explicit legacy path never silently falls back."""
    if '--write-scripts' in argv:
        index = argv.index('--write-scripts')
        if index + 1 >= len(argv) or argv[index + 1].startswith('--'):
            raise PublishError('公众号适配器缺少 --write-scripts 路径')
        return Path(argv[index + 1]).expanduser().resolve() / 'wechat_api.py'
    return Path(__file__).resolve().with_name('wechat_api.py')


def load_api(write_scripts=None):
    if write_scripts is None:
        import wechat_api
        return wechat_api
    root = Path(write_scripts).expanduser().resolve()
    if not (root / 'wechat_api.py').is_file():
        raise PublishError('显式指定的微信 API 模块缺失')
    # Old profiles retain their chosen module and companion tunnel imports.
    package_name = 'distribute_legacy_wechat_' + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(package_name + '.api', root / 'wechat_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['execute','verify','inspect-media'])
    p.add_argument('--request',required=True)
    p.add_argument('--profile',required=True)
    p.add_argument('--write-scripts',help='兼容旧配置：显式复用指定模块；默认使用本包内置 API')
    args=p.parse_args()
    try:
        api=load_api(args.write_scripts)
        from environment import load_profile
        value=dispatch(args.request,load_profile(args.profile),api,action=args.action)
        print(json.dumps(value,ensure_ascii=False)); return 0
    except Exception as exc:
        # Never dump arbitrary upstream exception text (it may include token URLs).
        print(json.dumps(dict(ok=False,error_type=type(exc).__name__,errcode=getattr(exc,'errcode',None),
                              step=getattr(exc,'step',None),detail=str(exc) if isinstance(exc,PublishError) else '微信调用失败；凭证与请求地址不回显'),ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__': raise SystemExit(main())
