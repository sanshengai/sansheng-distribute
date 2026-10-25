#!/usr/bin/env python3
"""Bundled bounded browser actions and opt-in X ordinary text submission candidate.

Editor identities are Chrome session targets, not persistent platform draft IDs.
Losing that session keeps the result unknown; it never authorizes another fill.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from browser_runtime import browser_config, connect, select_page, render_background_page
from environment import load_profile
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json


def settings(profile, target):
    if target['platform'] == 'douyin':
        from douyin_browser import settings as douyin_settings
        return douyin_settings(profile, target)
    if target['platform'] == 'weibo':
        from weibo_browser import settings as weibo_settings
        return weibo_settings(profile, target)
    if target['platform'] != 'x':
        raise PublishError('当前内置浏览器引擎仅含 X 文本、微博图文与抖音视频预填；其他平台使用现役接力')
    account = profile.get('accounts', {}).get(target['account'], {})
    identity = account.get('public_identities', {}).get('x')
    if 'x' not in account.get('platforms', []) or not isinstance(identity, str) or not re.fullmatch(r'@[A-Za-z0-9_]{1,15}', identity):
        raise PublishError('X 账号需要绑定实际 @handle')
    url, browser = browser_config(profile)
    return identity.lower(), url, browser


def check_pack(pack):
    if pack['platform'] == 'douyin':
        from douyin_browser import check_pack as check_douyin
        return check_douyin(pack)
    if pack['platform'] == 'weibo':
        from weibo_browser import check_pack as check_weibo
        return check_weibo(pack)
    if (pack['platform'] != 'x' or pack['endpoint'] not in {'prefill', 'publish'}
            or pack['content_type'] not in {'text','images','video'}):
        raise PublishError('当前 X 引擎承接 text/images/video:prefill 或普通 text:publish 候选')
    if pack['endpoint'] == 'publish' and pack['content_type'] != 'text':
        raise PublishError('X 图片/视频发送尚未接入；不能静默转为纯文字')
    document = pack['document']
    if (document and document['format'] != 'text') or pack['cover_asset_id'] or pack['subtitle_asset_ids'] or pack['platform_metadata']:
        raise PublishError('当前 X 引擎不承接文章、独立封面、字幕或平台扩展字段')
    if pack['content_type']=='images':
        from x_images import check
        check(pack)
    elif pack['content_type']=='video':
        from x_video import check
        check(pack)
    elif pack['assets']:
        raise PublishError('X 纯文本目标不能静默省略素材；图片应选择 images 类型')
    fields = pack['fields']
    if fields['title'] or (fields['text'] and fields['description'] and fields['text'] != fields['description']):
        raise PublishError('X 无独立标题或两套描述；交回制作端明确完整文案')
    if not fields['composed_text'].strip():
        raise PublishError('不能预填空文案')


def preflight_browser(manifest, target, profile, argv):
    expected = settings(profile, target)
    if profile['accounts'][target['account']].get('restricted'):
        raise PublishError('账号受限，只准备本地包')
    check_pack(build_pack(manifest, target['id'], profile))
    if '--profile' not in argv or argv.index('--profile') + 1 >= len(argv):
        raise PublishError('内置浏览器执行器需要 --profile')
    selected = load_profile(argv[argv.index('--profile')+1])
    if (settings(selected, target) != expected
            or selected['accounts'][target['account']] != profile['accounts'][target['account']]
            or selected.get('runtime_dir') != profile.get('runtime_dir')):
        raise PublishError('浏览器引擎与调度器账号、profile 或运行环境不符')
    from runtime import inspect
    result = inspect(profile)
    if not result['ok'] or not result.get('python'):
        raise PublishError('先用 setup.py --prepare-runtime 准备所选浏览器依赖')
    return {'local_only':True, 'implemented':target['platform']+' '+target['content_type']+':'+target['endpoint'],
            'final_click_available':target['platform']=='x' and target['endpoint']=='publish'}


def assert_identity(page, expected):
    parts = urlsplit(page.url)
    if parts.scheme != 'https' or parts.netloc != 'x.com' or parts.path != '/compose/post':
        raise PublishError('X 编辑页来源或路径改变；不猜测替代页面')
    switch = page.locator('[data-testid="SideNav_AccountSwitcher_Button"]')
    link = page.locator('a[data-testid="AppTabBar_Profile_Link"]')
    if switch.count() != 1 or link.count() != 1:
        raise PublishError('无法唯一核对 X 实际登录账号')
    handles = set(h.lower() for h in re.findall(r'(?<!\w)@[A-Za-z0-9_]{1,15}\b', switch.inner_text()))
    href = link.get_attribute('href')
    if handles != {expected} or not isinstance(href, str) or href.lower() != '/'+expected[1:]:
        raise PublishError('X 实际账号与任务绑定不符')


def editor(page):
    # The compose page also has a home-timeline editor and a nested outer dialog.
    modal = page.locator('[role="dialog"][aria-modal="true"]')
    if modal.count() != 1:
        raise PublishError('X 编辑弹窗不是唯一，不选择第一个编辑器')
    node = modal.locator('[data-testid="tweetTextarea_0"]')
    if node.count() != 1 or not node.is_visible():
        raise PublishError('X 编辑框不可唯一观察')
    return node


def assert_no_media(page):
    modal = page.locator('[role="dialog"][aria-modal="true"]')
    if (modal.locator('[data-testid="attachments"], video, img[src^="blob:"]').count()
            or modal.locator('input[type="file"]').evaluate_all(
                '(inputs) => inputs.some(input => input.files && input.files.length > 0)')):
        raise PublishError('X 编辑器已有媒体；不替换，也不认定为纯文本预填完成')


def empty_editor(page):
    node = editor(page)
    if node.inner_text().strip(' \t\r\n\u200b\ufeff'):
        raise PublishError('X 编辑器已有文字；保留原稿，先由作者明确处理')
    assert_no_media(page)
    return node


def target_id(page):
    session = page.context.new_cdp_session(page)
    try: ident = session.send('Target.getTargetInfo')['targetInfo']['targetId']
    finally: session.detach()
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', ident):
        raise PublishError('无法取得真实 Chrome 编辑会话身份')
    return ident


def assert_no_warning(page):
    for alert in page.locator('[role="alert"]').all():
        if alert.is_visible() and re.search(r'captcha|verify your|unusual activity|rate limit|验证|异常活动|频率|限流', alert.inner_text(), re.I):
            raise PublishError('X 出现验证或频率警告；保留现场，不自动重试')


def marker_path(request_path):
    return Path(request_path).with_suffix('.browser-attempt.json')


def dispatch(request_path, action, profile, lock_fd):
    candidate=read_json(request_path).get('target', {})
    if candidate.get('platform') == 'x' and candidate.get('endpoint') == 'publish':
        from x_publish_browser import dispatch as dispatch_x_publish
        return dispatch_x_publish(request_path, action, profile, lock_fd)
    if candidate.get('platform')=='weibo' and candidate.get('content_type')=='video':
        from weibo_video_browser import dispatch as dispatch_weibo_video
        return dispatch_weibo_video(request_path,action,profile,lock_fd)
    if candidate.get('platform')=='x' and candidate.get('content_type')=='video':
        from x_video_browser import dispatch as dispatch_x_video
        return dispatch_x_video(request_path,action,profile,lock_fd)
    if read_json(request_path).get('target', {}).get('platform') == 'douyin':
        from douyin_browser import dispatch as dispatch_douyin
        return dispatch_douyin(request_path, action, profile, lock_fd)
    if read_json(request_path).get('target', {}).get('platform') == 'weibo':
        from weibo_browser import dispatch as dispatch_weibo
        return dispatch_weibo(request_path, action, profile, lock_fd)
    if action not in {'execute','verify'}:
        raise PublishError('不支持的浏览器阶段')
    try: os.fstat(lock_fd)
    except OSError as exc: raise PublishError('浏览器执行必须继承核心账号锁') from exc
    request = read_json(request_path); manifest = read_json(request['manifest_path'])
    if digest(manifest) != request['manifest_digest'] or request['target'] not in manifest.get('targets', []):
        raise PublishError('浏览器请求与封存清单不符')
    target = request['target']; identity, endpoint, browser = settings(profile, target)
    if action == 'execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('账号受限，只准备本地包')
    pack = build_pack(manifest, target['id'], profile, check_files=action=='execute'); check_pack(pack)
    bound = {'pack_digest':digest(pack), 'identity':identity, 'cdp_url':endpoint, 'browser_profile':browser}
    marker = marker_path(request_path)
    if action == 'execute' and marker.exists():
        raise PublishError('该编辑任务已尝试；先只读核对，不重复填稿')
    if action == 'verify':
        saved = read_json(marker)
        if any(saved.get(k) != v for k,v in bound.items()):
            raise PublishError('原编辑会话绑定改变；不认领其他草稿')
        if not saved.get('target_id'):
            raise PublishError('缺少真实编辑会话身份；保持未知，不重新填稿')
    with connect(profile, writing=action=='execute') as connection:
        page = select_page(connection, 'x', path='/compose/post',
                           target_id=saved['target_id'] if action=='verify' else None)
        render_background_page(page)
        assert_identity(page, identity); assert_no_warning(page)
        if action == 'execute':
            # Never overwrite a restored or manually edited draft.
            empty_editor(page)
            if pack['assets']:
                from browser_assets import stage
                import x_images
                assets=stage(pack,request_path)
                x_images.prepare(page,assets)
            saved = dict(bound, target_id=target_id(page), state='attempted', tracking_scope='chrome_session_only')
            # Persist before the first browser mutation. There is no click action.
            fd = os.open(marker, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd,'w') as out:
                json.dump(saved,out); out.flush(); os.fsync(out.fileno())
            assert_identity(page, identity); assert_no_warning(page)
            # Fetch the editor again after all identity/receipt work: the author
            # may have started a draft during those browser round trips.
            node = empty_editor(page)
            node.fill(pack['fields']['composed_text'])
            if node.inner_text() != pack['fields']['composed_text']:
                raise PublishError('X 实际文案与封存包不符；先只读检查原稿')
            if pack['assets']:
                assert_identity(page,identity);assert_no_warning(page);assert_no_media(page)
                if editor(page).inner_text()!=pack['fields']['composed_text']:
                    raise PublishError('X 图片选择前文案改变；不继续上传')
                saved['images']=x_images.upload(page,assets)
            saved['state']='filled'; write_json(marker, saved)
        else:
            node = editor(page)
            if pack['assets']:
                import x_images
                x_images.verify(page,pack,saved)
            else:
                assert_no_media(page)
            if node.inner_text() != pack['fields']['composed_text']:
                raise PublishError('X 原编辑会话文案已变；不报告预填完成')
        result = {k:pack[k] for k in ('manifest_digest','target_id','platform','account')}
        result.update(state='pending' if action=='execute' else 'prefill_ready',
            remote_id='browser-editor:'+saved['target_id'], remote_url=page.url,
            observed_account_identity=identity, tracking_scope='chrome_session_only',
            platform_draft_saved=False, final_click_available=False)
        if action=='verify':
            result.update(content_verified=True, verified_pack_digest=digest(pack),
                          verified_asset_ids=[a['id'] for a in pack['assets']])
        if pack['assets']:
            result['media_tracking_scope']='local_blob_preview_only'
        return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['execute','verify'])
    parser.add_argument('--profile',required=True); parser.add_argument('--request',required=True)
    args=parser.parse_args(argv)
    try:
        profile=load_profile(args.profile)
        fd=int(os.environ.get('SANDY_PUBLISH_LOCK_FD','-1')); os.fstat(fd)
        from runtime import directory, worker_command
        if Path(sys.prefix).resolve() != directory(profile):
            command=worker_command(profile,'browser_engine',[args.action,'--profile',args.profile,'--request',args.request])
            return subprocess.run(command,pass_fds=(fd,)).returncode
        print(json.dumps(dispatch(args.request,args.action,profile,fd),ensure_ascii=False));return 0
    except Exception as exc:
        failure = {'ok':False, 'error_type':type(exc).__name__,
            'error':str(exc) if isinstance(exc,PublishError) else
            '浏览器执行或读回未完成；保留原编辑会话，先查询，不重试填稿'}
        # Core intentionally does not echo arbitrary child output. Persist this
        # bounded message privately so the failed stage remains diagnosable.
        try: write_json(Path(args.request).with_suffix('.browser-error.json'), dict(failure, action=args.action))
        except OSError: pass
        print(json.dumps(failure,ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
