"""Opt-in ordinary X text/video submission; one native click, same-ID recovery."""
from __future__ import annotations
import json
import os
import time

import browser_engine as engine
from browser_runtime import background_window, click_background
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json
import x_post


def video_evidence(page, pack, saved):
    import x_video
    native, cloud = x_video.verify(page, pack, saved)
    value = dict(source_asset_id=native['source_asset_id'], source_sha256=native['source_sha256'],
                 native=dict(media_id=cloud['media_id'], media_key=cloud['media_key'], type='video',
                    width=native['width'], height=native['height'],
                    duration_millis=round(native['duration']*1000), availability='Available'))
    x_post.check_publication_media(pack, value)
    return value


def submit_once(page, pack, identity, saved, marker):
    if saved.get('submission_attempted_at') is not None or saved.get('accepted'):
        raise PublishError('X 已有发送尝试；只能独立查询，不再次点击')

    def ready():
        engine.assert_identity(page, identity); engine.assert_no_warning(page)
        if pack['content_type'] == 'video':
            if saved.get('publication_media') != video_evidence(page, pack, saved):
                raise PublishError('X 发送前原片云端或预览证据已改变；不发送')
        else:
            engine.assert_no_media(page)
        if engine.editor(page).inner_text() != pack['fields']['composed_text']:
            raise PublishError('X 发送前文案已改变；保留原稿，不发送')

    ready()
    button = page.locator('[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]')
    if button.count() != 1 or not button.is_visible() or button.is_disabled():
        raise PublishError('X 发送按钮不唯一、不可见或禁用；不发送')
    saved.update(submission_attempted_at=time.time(), state='submission_unknown')
    write_json(marker, saved)  # Must precede the only native click, including after restart.

    def accepted(value):
        saved.update(accepted=value, state='accepted')
        write_json(marker, saved)

    capture = x_post.Capture(pack, identity, accepted)
    page.on('response', capture.response)
    def before_input():
        # A checkpoint means input may happen next, never that X accepted it.
        saved['native_input_checkpointed_at'] = time.time()
        write_json(marker, saved)
    try:
        ready()
        try:
            click_background(page, button, before_input=before_input)
        except Exception as exc:
            saved['click_error_type'] = type(exc).__name__
            saved['click_error_phase'] = ('native_input_uncertain' if saved.get('native_input_checkpointed_at')
                                         else 'before_native_input')
            if isinstance(exc, PublishError):
                saved['click_error'] = str(exc)[:300]
            if saved['click_error_phase'] == 'before_native_input':
                raise PublishError('X 原生点击前检查未完成；保留同一稿，不自动重试') from exc
            saved['click_reply_unknown'] = True
        deadline = time.monotonic() + 20
        while capture.accepted is None and capture.error is None and time.monotonic() < deadline:
            page.wait_for_timeout(100)
    finally:
        page.remove_listener('response', capture.response)
        saved['native_response_count'] = capture.count
        if capture.error:
            saved['receipt_error'] = capture.error
        write_json(marker, saved)
    if not saved.get('accepted'):
        raise PublishError('X 发送结果未知且无可核对作品 ID；保留首次尝试，不重复发送')
    return saved['accepted']


def dispatch(request_path, action, profile, lock_fd):
    if action not in {'execute', 'verify'}:
        raise PublishError('不支持的浏览器阶段')
    try:
        os.fstat(lock_fd)
    except OSError as exc:
        raise PublishError('X 发送必须继承核心账号锁') from exc
    request = read_json(request_path); manifest = read_json(request['manifest_path'])
    if digest(manifest) != request['manifest_digest'] or request['target'] not in manifest.get('targets', []):
        raise PublishError('X 发送请求与封存清单不符')
    target = request['target']; identity, endpoint, browser = engine.settings(profile, target)
    if action == 'execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('账号受限，只准备本地包')
    pack = build_pack(manifest, target['id'], profile, check_files=action == 'execute')
    engine.check_pack(pack)
    if pack['endpoint'] != 'publish' or pack['content_type'] not in {'text', 'video'}:
        raise PublishError('X 发送候选仅承接普通 text/video:publish')
    bound = dict(pack_digest=digest(pack), identity=identity, cdp_url=endpoint, browser_profile=browser)
    marker = engine.marker_path(request_path)
    if action == 'execute' and marker.exists():
        raise PublishError('X 发送任务已有尝试；只读查询原作品，不重填或重新发送')
    if action == 'verify':
        saved = read_json(marker)
        if any(saved.get(k) != v for k, v in bound.items()):
            raise PublishError('X 原发送绑定已改变；不切账号或认领其他作品')
        if not saved.get('submission_attempted_at') or not saved.get('accepted') or saved.get('receipt_error'):
            raise PublishError('X 缺少完整原生受理身份；保持未知，不重新发送')
        if request['job'].get('remote_id') not in (None, saved['accepted']['remote_id']):
            raise PublishError('X 原作品 ID 与核心账本矛盾')
        if pack['content_type'] == 'video':
            x_post.check_publication_media(pack, saved.get('publication_media'))
    with engine.connect(profile, writing=action == 'execute') as connection:
        anchor = engine.select_page(connection, 'x', path='/home')
        if action == 'execute':
            if pack['content_type'] == 'video':
                from browser_assets import stage
                import x_video
                assets = stage(pack, request_path); size = x_video.prepare(assets[0])
            with background_window(anchor, cdp_url=endpoint, retain=True) as page:
                saved = dict(bound, target_id=engine.target_id(page), state='owned_window_opened', submission_protocol=2)
                if pack['content_type'] == 'video':
                    saved.update(source_size=size, upload_events=[], upload_errors=[])
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as out:
                    json.dump(saved, out); out.flush(); os.fsync(out.fileno())
                page.goto('https://x.com/compose/post', wait_until='domcontentloaded', timeout=25000)
                page.locator('[role="dialog"][aria-modal="true"] [data-testid="tweetTextarea_0"]').wait_for(
                    state='visible', timeout=20000)
                engine.assert_identity(page, identity); engine.assert_no_warning(page)
                if pack['content_type'] == 'video':
                    from x_video_browser import fill
                    fill(page, pack, assets, identity, saved, marker)
                    saved['publication_media'] = video_evidence(page, pack, saved)
                else:
                    node = engine.empty_editor(page)
                    node.fill(pack['fields']['composed_text'])
                saved['state'] = 'filled'; write_json(marker, saved)
                value = submit_once(page, pack, identity, saved, marker)
                return dict({k:pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                            state='pending', remote_id=value['remote_id'],
                            remote_url=x_post.status_url(identity, value['remote_id']),
                            observed_account_identity=identity, tracking_scope='native_post_id')
        observed = x_post.readback(anchor, pack, identity, saved['accepted'],
                                   expected_media=saved.get('publication_media'))
        write_json(marker.with_suffix('.x-readback.json'), dict(observed, remote_id=saved['accepted']['remote_id']))
        return dict({k:pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                    **observed, state='published', remote_id=saved['accepted']['remote_id'],
                    observed_account_identity=identity, tracking_scope='native_post_id')
