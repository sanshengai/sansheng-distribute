"""Douyin / Weibo video: release an uploaded-but-never-sent attempt after explicit author confirmation.

Applies when a publish attempt uploaded (or began uploading) the video but stopped
before the persisted send checkpoint. The send path writes
``submission_attempted_at`` durably before any native click, so its absence plus
no other send trace is the record-side proof. The author confirmation file
(written by ``confirm_unsubmitted.py``) adds the author's statement and a fresh
read-only listing with no match. If the original owned window still exists it
must still be on the native upload/post editor, never a manager or success page.
Verify marks not submitted; execute archives the attempt, closes only that owned
window and lets the executor start one fresh upload.
"""
import time
from urllib.parse import urlsplit
from publish_core import PublishError, digest, read_json, write_json
import browser_preinput as pre

STATES = {'owned_window_opened', 'attempted'}
# Weibo video: the executor stopped after the one file selection (upload may be
# complete) but before any field fill or send checkpoint.
WEIBO_STATES = {'before_file_selection', 'selected_once'}
RELEASE_KEYS = ('release_original_state', 'release_verified_at', 'release_confirmation_digest')
EDITOR_PATHS = {'/creator-micro/content/upload', '/creator-micro/content/post/video'}
WEIBO_EDITOR = ('weibo.com', '/upload/channel')
FILLED = ('native_video', 'cover', 'cover_upload', 'fields_filled_at', 'dwell_seconds', 'video_verified_at')


def eligible(saved, platform):
    if platform not in ('douyin', 'weibo') or any(k in saved for k in pre.SENDING):
        return False
    state = saved.get('release_original_state', saved.get('state'))
    if platform == 'weibo':
        return (state in WEIBO_STATES and not any(k in saved for k in FILLED)
                and bool(saved.get('target_id')) and bool(saved.get('anchor_target_id')))
    return (state in STATES and saved.get('phase') == 'upload'
            and bool(saved.get('target_id')) and bool(saved.get('anchor_target_id')))


def original(saved):
    value = {k: v for k, v in saved.items() if k not in RELEASE_KEYS}
    value['state'] = saved.get('release_original_state', saved.get('state'))
    return value


def owned_pages(browser, target_id):
    found = []
    for ctx in browser.contexts:
        for page in ctx.pages:
            session = ctx.new_cdp_session(page)
            try: tid = session.send('Target.getTargetInfo')['targetInfo']['targetId']
            finally: session.detach()
            if tid == target_id: found.append(page)
    return found


def check_window(pages, platform='douyin'):
    if len(pages) > 1:
        raise PublishError('原窗口不唯一；保持未知')
    for page in pages:
        u = urlsplit(page.url)
        if platform == 'weibo':
            if (u.netloc, u.path) != WEIBO_EDITOR:
                raise PublishError('微博原窗口已离开视频发布页；不能认定未发送')
            continue
        if u.netloc != 'creator.douyin.com' or u.path not in EDITOR_PATHS:
            raise PublishError('抖音原窗口已离开上传编辑页；不能认定未发送')


def release(action, request, pack, bound, marker, profile, saved):
    if any(saved.get(k) != v for k, v in bound.items()) or request['job'].get('remote_id'):
        raise PublishError('抖音释放：原任务绑定不符或已有作品 ID')
    confirmed = pre.author_confirmation(marker, original(saved), request, bound)
    with pre.connect(profile) as browser:
        pages = owned_pages(browser, saved['target_id']); check_window(pages, pack['platform'])
        if action == 'verify':
            if request['job'].get('state') != 'submission_unknown':
                raise PublishError('抖音释放只接受结果未知任务')
            saved.update(release_original_state=saved.get('release_original_state', saved['state']),
                         state='unsent_release_verified', release_verified_at=time.time(),
                         release_confirmation_digest=confirmed)
            write_json(marker, saved)
            return dict({k: pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                        state='not_submitted', absence_verified=True,
                        absence_scope='author_confirmed_uploaded_never_sent',
                        evidence_path=str(marker), observed_account_identity=bound['identity'])
        if (action != 'execute' or request['job'].get('state') != 'prepared'
                or saved.get('state') != 'unsent_release_verified'
                or saved.get('release_confirmation_digest') != confirmed):
            raise PublishError('抖音释放须先通过独立核对')
        for page in pages:
            page.close()  # Only this task's own window, proven never sent.
    suffix = '.released-' + digest(saved)[:16]
    archive = marker.with_name(marker.name + suffix)
    if archive.exists(): raise PublishError('抖音释放证据已存在；先查询')
    marker.rename(archive)
    confirmation = pre.confirmation_path(marker)
    if confirmation.exists(): confirmation.rename(confirmation.with_name(confirmation.name + suffix))
    chosen = marker.with_name(marker.name + '.author-release.json')
    if chosen.exists(): chosen.rename(chosen.with_name(chosen.name + suffix))
    assets = marker.parent / marker.name.replace('.browser-attempt.json', '.browser-assets')
    if assets.exists(): assets.rename(assets.with_name(assets.name + suffix))
    return None
