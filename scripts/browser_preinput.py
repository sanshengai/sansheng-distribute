"""Prove a marked owned editor stopped before its first platform file selection.

This deliberately does not recover uploads, filled editors, or native send attempts.
"""
from pathlib import Path
import time
from publish_core import PublishError, digest, read_json, write_json
from browser_runtime import connect

STAGES = {'x': 'before_native_selection', 'weibo': 'owned_window_opened',
          'douyin': 'owned_window_opened'}
SENDING = ('submission_attempted_at', 'native_input_checkpointed_at', 'accepted',
           'native_input_receipt', 'publication_media', 'receipt_error',
           'click_reply_unknown','native_input_count','native_response_count','submission_snapshot')


def eligible(saved, platform):
    phase = saved.get('preinput_original_state', saved.get('state'))
    if phase != STAGES.get(platform) or any(k in saved for k in SENDING):
        return False
    if not saved.get('target_id'):
        return False
    if platform=='x' and saved.get('submission_protocol')!=2:return False
    if platform in {'weibo','douyin'} and not saved.get('anchor_target_id'):return False
    if platform == 'douyin':
        return (saved.get('phase') == 'upload' and saved.get('video') is None
                and 'video_source' not in saved
                and saved.get('video_upload_events') == []
                and saved.get('video_upload_errors') == [])
    return saved.get('upload_events') == [] and saved.get('upload_errors') == []


def prove(page, saved, platform):
    # No navigation: the exact original target must still be present.
    if not page.locator('input[type=file]').evaluate_all('(ns)=>ns.every(n=>!n.files.length)'):
        raise PublishError('首次上传前核对发现已选文件；保持未知')
    if platform == 'weibo':
        if page.url != 'about:blank':
            raise PublishError('微博已离开本地媒体检查页；不能证明未上传')
        return
    if platform == 'x':
        if page.url != 'https://x.com/compose/post':
            raise PublishError('X 原编辑页位置已改变')
        import browser_engine as home
        home.assert_identity(page, saved['identity']); home.assert_no_warning(page)
        home.empty_editor(page); home.assert_no_media(page)
    elif platform == 'douyin':
        import douyin_browser as home
        home.origin(page, home.UPLOAD); home.no_warning(page)
        if page.locator(home.TITLE+', '+home.DESCRIPTION+', video').count():
            raise PublishError('抖音已有编辑内容；不能认定未上传')
    else:
        raise PublishError('不支持的首次上传前恢复平台')


def recover(action, request, pack, bound, marker, profile):
    if not marker.exists():
        return None
    saved = read_json(marker); platform = pack['platform']
    if not eligible(saved, platform):
        return None
    if any(saved.get(k) != v for k,v in bound.items()) or request['job'].get('remote_id'):
        raise PublishError('首次上传前原任务绑定不符')
    with connect(profile) as browser:
        if platform in {'weibo','douyin'}:
            from browser_runtime import select_page
            anchor = select_page(browser,platform,target_id=saved.get('anchor_target_id'))
            if platform == 'weibo':
                from weibo_browser import assert_identity
            else:
                from douyin_browser import assert_identity
            assert_identity(anchor,bound['identity'])
        pages = []
        for ctx in browser.contexts:
            for page in ctx.pages:
                session = ctx.new_cdp_session(page)
                try: tid = session.send('Target.getTargetInfo')['targetInfo']['targetId']
                finally: session.detach()
                if tid == saved['target_id']: pages.append(page)
        if len(pages) != 1:
            raise PublishError('首次上传前原窗口已丢失；不能认定未提交')
        prove(pages[0], saved, platform)
    if action == 'verify':
        saved.update(preinput_original_state=saved.get('preinput_original_state',saved['state']),
                     state='preinput_absence_verified', preinput_verified_at=time.time())
        write_json(marker,saved)
        return dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},
                    state='not_submitted', absence_verified=True,
                    absence_scope='owned_editor_before_first_platform_upload',
                    evidence_path=str(marker), observed_account_identity=bound['identity'])
    if action != 'execute' or request['job']['state'] != 'prepared' or saved.get('state') != 'preinput_absence_verified':
        raise PublishError('首次上传前恢复须先通过独立核对')
    # Keep the failed attempt and immutable local copies; never discard receipts.
    suffix = '.preinput-'+digest(saved)[:16]
    archive = marker.with_name(marker.name+suffix)
    if archive.exists(): raise PublishError('首次上传前恢复证据已存在；先查询')
    marker.rename(archive)
    assets = Path(request['manifest_path']).parent.parent/'requests'/(Path(request['manifest_path']).stem+'.browser-assets')
    if assets.exists(): assets.rename(assets.with_name(assets.name+suffix))
    return None
