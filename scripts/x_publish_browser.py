"""Opt-in ordinary X text/images/video submission; one native click, same-ID recovery."""
from __future__ import annotations
import json
import os
import time

import browser_engine as engine
from browser_runtime import background_window, click_background
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json
import x_post


def checkpoint(saved, marker, **fields):
    value = dict(saved, **fields)
    write_json(marker, value)
    saved.update(value)


def video_evidence(page, pack, saved):
    import x_video
    native, cloud = x_video.verify(page, pack, saved)
    value = dict(source_asset_id=native['source_asset_id'], source_sha256=native['source_sha256'],
                 native=dict(media_id=cloud['media_id'], media_key=cloud['media_key'], type='video',
                    width=native['width'], height=native['height'],
                    duration_millis=round(native['duration']*1000), availability='Available'))
    x_post.check_publication_media(pack, value)
    return value


def image_evidence(page, pack, assets, saved):
    import x_images
    from x_image_upload import receipts
    if saved.get('image_upload_errors'):
        raise PublishError('X 图片上传回执记录失败；不发送或重传')
    previews = x_images.verify(page, pack, saved)
    rows = receipts(saved.get('image_upload_events'), assets, previews)
    value = [dict(source_asset_id=r['source_asset_id'], source_sha256=r['source_sha256'],
                  native=r['native']) for r in rows]
    x_post.check_publication_media(pack, value)
    return value


def fill_images(page, pack, assets, identity, saved, marker):
    import x_images
    from x_image_upload import Capture, wait_complete
    engine.assert_identity(page, identity); engine.assert_no_warning(page); engine.empty_editor(page)
    x_images.prepare(page, assets)
    def checkpoint(events, errors):
        value = dict(saved, image_upload_events=events, image_upload_errors=errors)
        write_json(marker, value); saved.update(value)
    capture = Capture(checkpoint); page.on('response', capture.response)
    try:
        engine.assert_identity(page, identity); engine.assert_no_warning(page); engine.empty_editor(page)
        value = dict(saved, state='before_native_selection')
        write_json(marker, value); saved.update(value)
        saved['images'] = x_images.upload(page, assets)
        wait_complete(page, capture, len(assets))
        saved['publication_media'] = image_evidence(page, pack, assets, saved)
        node = engine.editor(page)
        if node.inner_text().strip(' \t\r\n\u200b\ufeff'):
            raise PublishError('X 图片上传后已有其他文字；保留原稿，不覆盖')
        node.fill(pack['fields']['composed_text'])
        if node.inner_text() != pack['fields']['composed_text']:
            raise PublishError('X 实际图文文案与封存包不符；不发送')
        saved['state'] = 'filled'; write_json(marker, saved)
    finally:
        page.remove_listener('response', capture.response)


def submit_once(page, pack, identity, saved, marker, *, assets=None):
    if saved.get('submission_attempted_at') is not None or saved.get('accepted'):
        raise PublishError('X 已有发送尝试；只能独立查询，不再次点击')

    def ready():
        engine.assert_identity(page, identity); engine.assert_no_warning(page)
        if pack['content_type'] == 'video':
            if saved.get('publication_media') != video_evidence(page, pack, saved):
                raise PublishError('X 发送前原片云端或预览证据已改变；不发送')
        elif pack['content_type'] == 'images':
            if saved.get('publication_media') != image_evidence(page, pack, assets, saved):
                raise PublishError('X 发送前原图云端或预览证据已改变；不发送')
        else:
            engine.assert_no_media(page)
        if engine.editor(page).inner_text() != pack['fields']['composed_text']:
            raise PublishError('X 发送前文案已改变；保留原稿，不发送')

    dismiss_topic_suggestions(page)
    ready()
    button = page.locator('[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]')
    if button.count() != 1 or not button.is_visible() or button.is_disabled():
        raise PublishError('X 发送按钮不唯一、不可见或禁用；不发送')
    checkpoint(saved, marker, submission_attempted_at=time.time(), state='submission_unknown')

    def accepted(value):
        checkpoint(saved, marker, accepted=value, state='accepted')

    capture = x_post.Capture(pack, identity, accepted)
    page.on('response', capture.response)
    def before_input():
        ready()
        # A checkpoint means input may happen next, never that X accepted it.
        checkpoint(saved, marker, native_input_checkpointed_at=time.time())
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
        saved['native_response_diagnostic'] = capture.diagnostic
        if capture.error:
            saved['receipt_error'] = capture.error
        write_json(marker, saved)
    if not saved.get('accepted'):
        raise PublishError('X 发送结果未知且无可核对作品 ID；保留首次尝试，不重复发送')
    return saved['accepted']


def before_native_send(saved):
    if any(k in saved for k in ('native_input_checkpointed_at','accepted','receipt_error')):
        return False
    if 'submission_attempted_at' not in saved:return True
    return (saved.get('submission_protocol')==2 and saved.get('click_error_phase')=='before_native_input'
            and saved.get('native_response_count')==0 and saved.get('click_error_type')=='PublishError')


def dismiss_topic_suggestions(page):
    popup=page.locator('[role="listbox"][id^="typeaheadDropdownWrapped-"]')
    if popup.count()==1:
        page.keyboard.press('Escape')
        if popup.count():raise PublishError('X 话题建议仍遮挡发送按钮；不点击发送')


def close_accepted_editor(page,saved,marker):
    """Cleanup must not erase a durable accepted identity or mask its readback."""
    try:
        page.close()
        checkpoint(saved,marker,owned_composer_closed_after_acceptance=True)
    except Exception as exc:
        checkpoint(saved,marker,owned_composer_cleanup_error=type(exc).__name__)


def recover_uploaded_unsent(action, request, pack, bound, marker, profile):
    """Continue the same uploaded video only after an independent unsent proof."""
    if pack['content_type'] != 'video' or not marker.exists(): return None
    saved = read_json(marker)
    if saved.get('state') not in {'before_native_selection','cloud_processed','filled','unsent_video_verified','submission_unknown'}:
        return None
    if not before_native_send(saved):return None
    if not saved.get('upload_events'): return None
    if any(saved.get(k)!=v for k,v in bound.items()) or request['job'].get('remote_id'):
        raise PublishError('X 已上传未发送任务的绑定已改变')
    with engine.connect(profile,writing=action=='execute') as connection:
        page=engine.select_page(connection,'x',path='/compose/post',target_id=saved['target_id'])
        engine.assert_identity(page,bound['identity']);engine.assert_no_warning(page)
        import x_video
        native,cloud=x_video.verify(page,pack,saved)
        node=engine.editor(page);text=node.inner_text()
        blank=not text.strip(' \t\r\n\u200b\ufeff')
        if not blank and text!=pack['fields']['composed_text']:
            raise PublishError('X 原稿文案已被修改；保留原稿，不继续')
        if action=='verify':
            checkpoint(saved,marker,state='unsent_video_verified',native_video=native,cloud_video=cloud)
            return dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},
                state='not_submitted',absence_verified=True,absence_scope='original_video_no_native_send_attempt',
                evidence_path=str(marker),observed_account_identity=bound['identity'])
        if action!='execute' or saved['state']!='unsent_video_verified' or request['job']['state']!='prepared':
            raise PublishError('X 已上传视频须先独立核对未发送状态')
        if 'submission_attempted_at' in saved:
            old={k:saved.pop(k) for k in ('submission_attempted_at','click_error_type','click_error_phase','click_error','native_response_count') if k in saved}
            saved.setdefault('pre_native_input_failures',[]).append(old)
            checkpoint(saved,marker,state='unsent_video_verified')
        if blank:node.fill(pack['fields']['composed_text'])
        dismiss_topic_suggestions(page)
        saved['publication_media']=video_evidence(page,pack,saved)
        checkpoint(saved,marker,state='filled')
        accepted=submit_once(page,pack,bound['identity'],saved,marker)
        close_accepted_editor(page,saved,marker)
        return dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},
            state='pending',remote_id=accepted['remote_id'],
            remote_url=x_post.status_url(bound['identity'],accepted['remote_id']),
            observed_account_identity=bound['identity'],tracking_scope='native_post_id')


def recover_candidate(request, pack, bound, marker, profile):
    saved=read_json(marker);candidate=request.get('recovery_remote_id')
    if (request.get('recovery_platform')!='x' or request['job']['state']!='submission_unknown'
            or request['job'].get('remote_id') or any(saved.get(k)!=v for k,v in bound.items())
            or saved.get('submission_protocol')!=2 or not saved.get('native_input_checkpointed_at')
            or saved.get('native_response_count')!=1 or saved.get('accepted')):
        raise PublishError('X 候选恢复缺少原任务、首次发送与原片身份；不认领')
    x_post.status_url(bound['identity'],candidate)
    x_post.check_publication_media(pack,saved.get('publication_media'))
    with engine.connect(profile) as connection:
        anchor=engine.select_page(connection,'x',target_id=saved['target_id'])
        engine.assert_identity(anchor,bound['identity'],path='/home')
        observed=x_post.readback(anchor,pack,bound['identity'],{'remote_id':candidate},
                                expected_media=saved['publication_media'],cdp_url=bound['cdp_url'])
        accepted=dict(remote_id=candidate,identity=bound['identity'],author_id=observed['author_id'],
                      observed_at=time.time(),recovered_from_independent_detail=True)
        checkpoint(saved,marker,accepted=accepted,state='accepted',
                   original_receipt_error=saved.get('receipt_error'),receipt_error=None)
        close_accepted_editor(anchor,saved,marker)
    return dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},
                **observed,state='published',remote_id=candidate,
                observed_account_identity=bound['identity'],tracking_scope='native_post_id',recovery_verified=True)


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
    if pack['endpoint'] != 'publish' or pack['content_type'] not in {'text', 'images', 'video'}:
        raise PublishError('X 发送候选仅承接普通 text/images/video:publish')
    bound = dict(pack_digest=digest(pack), identity=identity, cdp_url=endpoint, browser_profile=browser)
    marker = engine.marker_path(request_path)
    if action=='verify' and request.get('recovery_remote_id') is not None:
        return recover_candidate(request,pack,bound,marker,profile)
    from browser_preinput import recover
    recovered = recover(action, request, pack, bound, marker, profile)
    if recovered is not None: return recovered
    recovered = recover_uploaded_unsent(action, request, pack, bound, marker, profile)
    if recovered is not None: return recovered
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
        if pack['content_type'] in {'video', 'images'}:
            x_post.check_publication_media(pack, saved.get('publication_media'))
    with engine.connect(profile, writing=action == 'execute') as connection:
        anchor = engine.select_page(connection, 'x', path='/home')
        if action == 'execute':
            if pack['content_type'] in {'video', 'images'}:
                from browser_assets import stage
                assets = stage(pack, request_path)
                if pack['content_type'] == 'video':
                    import x_video
                    size = x_video.prepare(assets[0])
            with background_window(anchor, cdp_url=endpoint, retain=True) as page:
                saved = dict(bound, target_id=engine.target_id(page), state='owned_window_opened', submission_protocol=2)
                if pack['content_type'] == 'video':
                    saved.update(source_size=size, upload_events=[], upload_errors=[])
                elif pack['content_type'] == 'images':
                    saved.update(image_upload_events=[], image_upload_errors=[])
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
                elif pack['content_type'] == 'images':
                    fill_images(page, pack, assets, identity, saved, marker)
                else:
                    node = engine.empty_editor(page)
                    node.fill(pack['fields']['composed_text'])
                saved['state'] = 'filled'; write_json(marker, saved)
                value = submit_once(page, pack, identity, saved, marker,
                                    assets=assets if pack['content_type'] == 'images' else None)
                close_accepted_editor(page,saved,marker)
                return dict({k:pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                            state='pending', remote_id=value['remote_id'],
                            remote_url=x_post.status_url(identity, value['remote_id']),
                            observed_account_identity=identity, tracking_scope='native_post_id')
        observed = x_post.readback(anchor, pack, identity, saved['accepted'],
                                   expected_media=saved.get('publication_media'),cdp_url=endpoint)
        write_json(marker.with_suffix('.x-readback.json'), dict(observed, remote_id=saved['accepted']['remote_id']))
        return dict({k:pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                    **observed, state='published', remote_id=saved['accepted']['remote_id'],
                    observed_account_identity=identity, tracking_scope='native_post_id')
