"""Disabled-by-default native Douyin sender candidate; no private API replay."""
from __future__ import annotations
import json
import math
import os
import re
import time
from urllib.parse import urlsplit

from browser_assets import stage
from browser_runtime import background_window, click_background
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json
import douyin_browser as home
import douyin_cover as cover
import douyin_post as post
import douyin_upload as upload
import douyin_video as video

CREATE_PATHS={'/web/api/media/aweme/create/':'old','/web/api/media/aweme/create_v2/':'v2'}
JS_SPACE='\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
CHOICES_READ='''ns=>ns.filter(n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0&&[n,...function*(x){while(x.parentElement){x=x.parentElement;yield x;}}(n)].every(x=>{const s=getComputedStyle(x);return s.visibility==='visible'&&s.display!=='none'&&Number(s.opacity)>0;});}).map(n=>({text:n.innerText.trim(),inputs:[...n.querySelectorAll('input')].map(x=>({type:x.type,checked:x.checked,disabled:x.disabled,value:x.value}))}))'''


def check_pack(pack):
    home.check_pack(pack,publishing=True)
    if home.declaration(pack['platform_metadata'])!='内容由AI生成':
        raise PublishError('抖音发送候选仅覆盖已核对的 AI 声明详情；其他声明不得省略或替换')
    if pack['fields']['title']!=pack['fields']['title'].strip(JS_SPACE):
        raise PublishError('抖音原生发布会去除标题首尾空白；先修订交接包')


def public_text(pack):
    # Observed native serializer: text.trim(), title.trim(), then one separator.
    # Do not collapse internal whitespace or infer rules from a legacy work.
    title=pack['fields']['title'].strip(JS_SPACE)
    caption=home.topic_document(pack)['text'].strip(JS_SPACE)
    return dict(title=title,caption=caption,description=title+' '+caption)


def selected_options(rows):
    groups=[{'公开':('0',True),'好友可见':('2',False),'仅自己可见':('1',False)},
            {'立即发布':('0',True),'定时发布':('1',False)},
            {'不同时发布':('0',True),'同时发布到':('1',False)}]
    if not isinstance(rows,list):raise PublishError('抖音发布选项未能读回')
    if any(isinstance(r,dict) and r.get('text') in {'有试看','无试看'} for r in rows):
        raise PublishError('抖音发送候选未覆盖试看或付费设置；不修改设置')
    for group in groups:
        for label,(value,checked) in group.items():
            found=[r for r in rows if isinstance(r,dict) and r.get('text')==label]
            expected=dict(type='checkbox',checked=checked,disabled=False,value=value)
            if (len(found)!=1 or found[0].get('inputs')!=[expected]
                    or any(type(found[0]['inputs'][0][k]) is not bool for k in ('checked','disabled'))):
                raise PublishError('抖音须原生明确为公开、立即发布且不跨发；不改变作者设置')
    return dict(visibility='public',schedule='immediate',cross_post=False)


def native_account(anchor, identity, endpoint):
    bodies=[];errors=[]
    with background_window(anchor,cdp_url=endpoint) as page:
        def capture(response):
            u=urlsplit(response.url)
            if u.scheme!='https' or u.netloc!='creator.douyin.com' or u.path!=post.ACCOUNT_PATH:return
            try:
                if response.request.method!='GET' or response.status!=200:raise ValueError()
                bodies.append(post.account(response.json()))
            except Exception:errors.append(True)
        page.on('response',capture)
        page.goto(post.HOME,wait_until='domcontentloaded',timeout=25000)
        label=page.get_by_text(re.compile(r'^抖音号[：:]?'+re.escape(identity)+'$'))
        label.wait_for(state='visible',timeout=15000)
        deadline=time.monotonic()+15
        while not bodies and not errors and time.monotonic()<deadline:page.wait_for_timeout(100)
        if page.url!=post.HOME or label.count()!=1 or errors or len(set(bodies))!=1:
            raise PublishError('抖音原生个人身份与公开账号编号不能独立核对')
    return bodies[0]


class Capture:
    def __init__(self,uid,save):
        self.uid=uid;self.save=save;self.count=0;self.accepted=None;self.error=None

    def response(self,response):
        u=urlsplit(response.url)
        if u.scheme!='https' or u.netloc!='creator.douyin.com' or u.path not in CREATE_PATHS:return
        self.count+=1
        try:
            if self.count!=1 or response.request.method!='POST' or response.status!=200:raise ValueError()
            raw=response.body()
            if len(raw)>256*1024:raise ValueError()
            def pairs(values):
                value={}
                for key,item in values:
                    if key in value:raise ValueError()
                    value[key]=item
                return value
            body=json.loads(raw,object_pairs_hook=pairs)
            if (not isinstance(body,dict) or type(body.get('status_code')) is not int
                    or body['status_code']!=0 or 'error' in body and body['error'] is not None):raise ValueError()
            rid=body.get('item_id') if CREATE_PATHS[u.path]=='v2' else body.get('aweme',{}).get('aweme_id')
            if not post.decimal(rid) or not post.decimal(self.uid):raise ValueError()
            if isinstance(body.get('aweme'),dict):
                aweme=body['aweme']
                if 'aweme_id' in aweme and aweme['aweme_id']!=rid:raise ValueError()
                if 'author' in aweme and aweme['author'].get('uid')!=self.uid:raise ValueError()
            value=dict(remote_id=rid,bound_native_user_id=self.uid,observed_at=time.time(),
                       response_digest=digest(body),native_route=CREATE_PATHS[u.path])
            self.save(value)  # Persist native identity before in-memory success.
            self.accepted=value
        except Exception:
            self.error='抖音原生发送回执未能完整核对或保存；不重复发送'


def checkpoint(saved,marker,**fields):
    value=dict(saved,**fields);write_json(marker,value);saved.update(value)


def ready(page,pack,identity,uid,endpoint,saved):
    if native_account(page,identity,endpoint)!=uid:raise PublishError('抖音发送前实际作者身份已改变')
    home.no_warning(page);home.verify_fields(page,pack,saved)
    if not video.verify(page,pack,saved):raise PublishError('抖音发送缺少原片字节证明')
    if upload.verify(pack,saved,saved.get('source_video')) is None:
        raise PublishError('抖音发送缺少原生 VID 与原片绑定，不补造旧回执')
    # Require new source-object receipts even before a native click can occur.
    roles=cover.requested(pack)
    try:objects={role:saved['covers'][role]['source_object']['uri'] for role in roles}
    except (KeyError,TypeError) as exc:raise PublishError('抖音缺少封面原始上传对象，不补造旧证据') from exc
    cover.post_cover_bindings(dict(covers={('vertical' if r=='portrait' else 'horizontal'):v for r,v in objects.items()}),pack,saved)
    return selected_options(page.locator('label').evaluate_all(CHOICES_READ))


def submit_once(page,pack,identity,uid,endpoint,saved,marker):
    if saved.get('submission_attempted_at') is not None or saved.get('accepted'):
        raise PublishError('抖音已有发送尝试；只查询原作品，不再次点击')
    options=ready(page,pack,identity,uid,endpoint,saved)
    button=page.get_by_role('button',name='发布',exact=True)
    if button.count()!=1 or not button.is_visible() or button.is_disabled():
        raise PublishError('抖音发布按钮不唯一、不可见或禁用')
    checkpoint(saved,marker,submission_attempted_at=time.time(),state='submission_unknown',publication_options=options)
    capture=Capture(uid,lambda value:checkpoint(saved,marker,accepted=value,state='accepted'))
    page.on('response',capture.response)
    def before_input():
        ready(page,pack,identity,uid,endpoint,saved)
        checkpoint(saved,marker,native_input_checkpointed_at=time.time())
    try:
        try:click_background(page,button,before_input=before_input)
        except Exception as exc:
            checkpoint(saved,marker,click_error_type=type(exc).__name__,
                       click_error_phase='native_input_uncertain' if saved.get('native_input_checkpointed_at') else 'before_native_input')
            if not saved.get('native_input_checkpointed_at'):
                raise PublishError('抖音点击前检查未完成；保留原稿，不重试') from exc
        deadline=time.monotonic()+20
        while capture.accepted is None and capture.error is None and time.monotonic()<deadline:
            page.wait_for_timeout(100)
    finally:
        page.remove_listener('response',capture.response)
        checkpoint(saved,marker,native_response_count=capture.count,**({'receipt_error':capture.error} if capture.error else {}))
    if not saved.get('accepted'):raise PublishError('抖音发送结果未知，未取得可靠作品 ID；不重复发送')
    return saved['accepted']


def match_readback(observed,pack,saved,identity,uid):
    accepted=saved['accepted'];details=observed.get('details',{});text=public_text(pack)
    wanted_collection=pack['platform_metadata'].get('collection_name')
    actual_collection=details.get('collection')
    if (observed.get('remote_id')!=accepted['remote_id'] or observed.get('native_user_id')!=uid
            or observed.get('observed_account_identity')!=identity
            or details.get('remote_id')!=accepted['remote_id'] or details.get('native_user_id')!=uid
            or any(details.get(k)!=v for k,v in text.items())
            or details.get('topics')!=home.topic_names(pack)
            or details.get('declaration')!=home.declaration(pack['platform_metadata'])
            or observed.get('description')!=text['description']
            or type(observed.get('native_review_status')) is not int or observed['native_review_status']!=2
            or type(observed.get('native_visibility_status')) is not int or observed['native_visibility_status']!=1
            or (actual_collection is not None if wanted_collection is None else
                not isinstance(actual_collection,dict) or actual_collection.get('name')!=wanted_collection)):
        raise PublishError('抖音独立作品身份、完整文案、话题、声明或合集与封存任务不符')
    source_video=upload.post_video_binding(details,pack,saved)
    source_covers=cover.post_cover_bindings(details,pack,saved)
    return dict(source_video=source_video,source_covers=source_covers)


def dispatch(request_path,action,profile,lock_fd):
    if action not in {'execute','verify'}:raise PublishError('不支持的抖音发送阶段')
    try:os.fstat(lock_fd)
    except OSError as exc:raise PublishError('抖音发送须继承核心账号锁') from exc
    request=read_json(request_path);manifest=read_json(request['manifest_path'])
    if digest(manifest)!=request['manifest_digest'] or request['target'] not in manifest.get('targets',[]):
        raise PublishError('抖音发送请求与封存清单不符')
    target=request['target'];identity,endpoint,browser=home.settings(profile,target)
    if action=='execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('抖音账号受限，只准备本地包')
    pack=build_pack(manifest,target['id'],profile,check_files=action=='execute');check_pack(pack)
    marker=home.marker_path(request_path)
    bound=dict(pack_digest=digest(pack),identity=identity,cdp_url=endpoint,browser_profile=browser)
    from browser_preinput import recover
    recovered = recover(action, request, pack, bound, marker, profile)
    if recovered is not None: return recovered
    if action=='execute' and marker.exists():raise PublishError('抖音已有尝试；只查询，不重新上传或填报')
    if action=='verify':
        saved=read_json(marker);accepted=saved.get('accepted',{})
        if not isinstance(accepted,dict):raise PublishError('抖音受理回执格式不完整；不重新发送')
        stamps=[saved.get('submission_attempted_at'),saved.get('native_input_checkpointed_at'),accepted.get('observed_at')]
        ordered_times=(all(type(t) in (int,float) and math.isfinite(t) and t>0 for t in stamps)
                       and stamps==sorted(stamps))
        if (any(saved.get(k)!=v for k,v in bound.items()) or not saved.get('anchor_target_id')
                or not saved.get('submission_attempted_at') or saved.get('receipt_error')
                or not ordered_times or saved.get('publication_options')!=dict(visibility='public',schedule='immediate',cross_post=False)
                or not post.decimal(saved.get('native_user_id')) or not post.decimal(accepted.get('remote_id'))
                or accepted.get('bound_native_user_id')!=saved['native_user_id']
                or request['job'].get('remote_id') not in (None,accepted['remote_id'])):
            raise PublishError('抖音原发送身份或受理回执不完整；保持未知，不重新发送')
    from browser_engine import target_id
    with home.connect(profile,writing=True) as connection:
        anchor=home.select_page(connection,'douyin',**(dict(target_id=saved['anchor_target_id']) if action=='verify' else {}))
        uid=native_account(anchor,identity,endpoint)
        if action=='execute':
            video.prepare(next(a for a in pack['assets'] if a['kind']=='video'))
            assets=stage(pack,request_path)
            with background_window(anchor,cdp_url=endpoint,retain=True) as page:
                saved=dict(bound,target_id=target_id(page),anchor_target_id=target_id(anchor),native_user_id=uid,
                           state='owned_window_opened',phase='upload',video=None,video_upload_events=[],video_upload_errors=[])
                try:
                    fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'w') as stream:
                        json.dump(saved,stream);stream.flush();os.fsync(stream.fileno())
                except Exception:
                    page.close()  # No upload has begun; close only this new owned window.
                    raise
                page.goto('https://creator.douyin.com'+home.UPLOAD,wait_until='domcontentloaded',timeout=25000)
                home.empty_upload(page)
                cover_sources=cover.prepare(page,assets,cover.requested(pack))
                source=next(a for a in assets if a['kind']=='video')
                video.prepare(source);checkpoint(saved,marker,video_source=upload.source(source),state='attempted')
                home.fill_new(page,pack,assets,cover_sources,identity,saved,marker)
                accepted=submit_once(page,pack,identity,uid,endpoint,saved,marker)
                if not saved.get('receipt_error'):
                    try:page.close()
                    except Exception as exc:checkpoint(saved,marker,owned_composer_cleanup_error=type(exc).__name__)
                    else:checkpoint(saved,marker,owned_composer_closed_after_acceptance=True)
        else:
            if uid!=saved['native_user_id']:raise PublishError('抖音原任务账号原生身份已改变')
        accepted=saved['accepted'];rid=accepted['remote_id']
        result=dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},
                    state='pending',remote_id=rid,remote_url='https://www.douyin.com/video/'+rid,
                    observed_account_identity=identity,tracking_scope='native_post_id',content_verified=False)
        if action=='execute':return result
        try:observed=post.readback(anchor,cdp_url=endpoint,public_identity=identity,remote_id=rid,include_details=True)
        except PublishError:
            result.update(stage='independent_readback_unavailable',verification_pending=True)
            return result  # An accepted ID is retained even while review/list queries fail.
        proof=match_readback(observed,pack,saved,identity,uid)
        write_json(marker.with_suffix('.douyin-readback.json'),dict(observation=observed,source_bindings=proof))
        result.update(state='published',content_verified=True,source_media_verified=True,
                      verified_pack_digest=digest(pack),verified_asset_ids=[a['id'] for a in pack['assets']])
        return result
