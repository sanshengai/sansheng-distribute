"""Douyin only: continue an attempt whose video upload and native receipt were recorded
but whose fields were never filled and nothing was sent.

Release conditions (all required): explicit author confirmation file with a fresh
read-only listing (no match, written by ``confirm_unsubmitted.py``); no remote ID;
no send trace in the attempt record; exactly one recorded native commit receipt and
no receipt error; the original owned window is still on the native post editor with
empty title/description and default declaration/collection; the player-byte SHA-256,
size, dimensions, duration and object match the sealed source and that receipt.
``verify`` records the check; ``execute`` re-checks, fills once in the same window
and sends once. Never uploads again.
"""
import hashlib, re, time
from pathlib import Path
from urllib.parse import urlsplit
from publish_core import PublishError, write_json
import browser_preinput as pre

VERIFIED = 'uploaded_resume_verified'


PARTIAL_PHASE = re.compile(r'fields|topic_[0-9]+_(search|readback)')
RESUME_KEYS = ('resume_original_state', 'resume_verified_at', 'resume_confirmation_digest')


def base_state(saved):
    return saved.get('resume_original_state') if saved.get('state') == VERIFIED else saved.get('state')


def partial(saved):
    """Fields begun but stopped inside title/description/topics: never reached
    declaration, collection, cover, schedule or any send step."""
    # 'attempted' + a field phase only arises when a resume execute stopped mid-clear.
    return (base_state(saved) in ('uploaded', 'attempted') and bool(PARTIAL_PHASE.fullmatch(str(saved.get('phase'))))
            and not any(k in saved for k in ('covers', 'schedule')))


COVER_READBACK = re.compile(r'cover_(portrait|landscape)_(complete|readback)')


def cover_pending(saved):
    """Fields, declaration and collection done; the cover dialog completed but its
    readback stopped. Nothing scheduled or sent."""
    return (base_state(saved) in ('uploaded', 'attempted') and bool(COVER_READBACK.fullmatch(str(saved.get('phase'))))
            and 'schedule' not in saved)


UNCLICKED_KEYS = ('submission_attempted_at', 'click_error_type', 'click_error_phase',
                  'native_response_count', 'publication_options')


def unclicked(saved):
    """Filled and checkpointed for sending, but the pre-click check failed before any
    native input: no input checkpoint, no native response, no acceptance or receipt."""
    others = set(pre.SENDING) - {'submission_attempted_at', 'native_response_count'}
    return (base_state(saved) == 'submission_unknown' and saved.get('phase') == 'complete'
            and saved.get('submission_attempted_at') is not None
            and saved.get('click_error_phase') == 'before_native_input'
            and not saved.get('native_response_count')
            and not any(saved.get(k) for k in others)
            and isinstance(saved.get('covers'), dict))


def applies(saved):
    base = (not any(k in saved for k in pre.SENDING) and 'video_source' in saved
            and saved.get('video_upload_errors') == [] and isinstance(saved.get('video_upload_events'), list)
            and len(saved['video_upload_events']) >= 1 and bool(saved.get('target_id')))
    unfilled = base_state(saved) == 'attempted' and saved.get('phase') == 'upload' and saved.get('video') is None
    if unclicked(saved):
        return (saved.get('video_upload_errors') == [] and bool(saved.get('video_upload_events'))
                and bool(saved.get('target_id')) and 'video_source' in saved)
    return base and (unfilled or partial(saved) or cover_pending(saved))


def release_path(marker):
    return Path(marker).with_name(Path(marker).name + '.author-release.json')


def release_chosen(marker, saved):
    """Author chose to release (not resume) this exact uploaded attempt."""
    path = release_path(marker)
    if not path.exists():
        return False
    from publish_core import read_json, digest
    value = read_json(path)
    if not isinstance(value, dict) or value.get('attempt_digest') != digest(original(saved)):
        raise PublishError('作者释放选择与当前尝试记录不符')
    return True


def original(saved):
    value = {k: v for k, v in saved.items() if k not in RESUME_KEYS}
    value['state'] = base_state(saved)
    return value


def restaged(pack, request_path):
    root = Path(request_path).with_suffix('.browser-assets'); out = []
    for asset in pack['assets']:
        path = root / (asset['id'] + Path(asset['absolute_path']).suffix.lower())
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            raise PublishError('抖音续跑的暂存素材缺失或改变；不重新上传')
        out.append(dict(asset, absolute_path=str(path)))
    return out


def page_target(ctx, page):
    session = ctx.new_cdp_session(page)
    try: return session.send('Target.getTargetInfo')['targetInfo']['targetId']
    finally: session.detach()


def owned_page(connection, target_id, saved=None):
    """The original owned window; after a browser restart (session restore gives
    new target IDs) rebind only to the single restored editor whose native preview
    object is the recorded one. Returns (page, target_id)."""
    found, restored = [], []
    for ctx in connection.contexts:
        for page in ctx.pages:
            tid = page_target(ctx, page)
            if tid == target_id: found.append((page, tid))
            elif saved and urlsplit(page.url).path == '/creator-micro/content/post/video':
                restored.append((page, tid))
    if len(found) == 1:
        return found[0]
    key = ((saved or {}).get('video') or {}).get('preview_key')
    if not found and key:
        import douyin_browser as home
        same = []
        for page, tid in restored:
            try:
                if home.video_identity(page)['preview_key'] == key: same.append((page, tid))
            except PublishError:
                pass
        if len(same) == 1:
            return same[0]
    raise PublishError('抖音上传后的原编辑窗口不在或不唯一；不重新上传')


def check_editor(page, pack, saved, assets):
    import douyin_browser as home, douyin_video as video, douyin_upload as upload
    import douyin_focus
    douyin_focus.ensure(page)  # background tab clicks (e.g. preview tab) need held focus emulation
    home.origin(page, home.EDITOR); home.no_warning(page)
    if cover_pending(saved) or unclicked(saved):
        if (home.declaration_control(page).inner_text() == '请选择自主声明'
                or home.collection_value(page) == '请选择合集'):
            raise PublishError('抖音续跑：封面阶段但声明或合集未选；不认定')
        # The editor may still show the cover preview; switch the native preview tab back to video (view only).
        import douyin_cover
        douyin_cover.restore_video(page)
    elif not partial(saved):
        home.assert_empty_fields(page)
    if not (cover_pending(saved) or unclicked(saved)) and (home.declaration_control(page).inner_text() != '请选择自主声明'
            or home.collection_value(page) != '请选择合集'):
        raise PublishError('抖音原编辑页已有声明或合集；不能认定未填写')
    # A long-idle background editor may have unloaded its preview; wait (read-only)
    # for the native player to report data again before binding its identity.
    try:
        page.wait_for_function("()=>{const v=document.querySelector('video');return !!v&&v.readyState>=1&&v.duration>0}", timeout=30000)
    except Exception:
        pass
    observed = dict(video=home.video_identity(page),
                    source_video=video.observe(page, next(a for a in assets if a['kind'] == 'video')))
    observed['cloud_video'] = upload.receipt(saved['video_upload_events'], saved['video_source'], observed['source_video'])
    return observed


def clear_fields(page):
    """Partial resume only: clear this task's own unfinished title/description in the same window."""
    import douyin_browser as home, douyin_focus
    douyin_focus.ensure(page)
    title, description = home.text_fields(page)
    title.fill('')
    # The native editor ignores fill('') beyond one character; select-all + delete
    # through the page keyboard, a few bounded rounds, then require it empty.
    for _ in range(5):
        if not home.description_text(description):
            break
        description.focus(timeout=5000)
        page.keyboard.press('ControlOrMeta+a'); page.keyboard.press('Backspace')
        page.wait_for_timeout(400)
    home.assert_empty_fields(page)


def run(action, request, request_path, pack, bound, marker, profile, saved):
    if any(saved.get(k) != v for k, v in bound.items()) or request['job'].get('remote_id'):
        raise PublishError('抖音续跑：原任务绑定不符或已有作品 ID')
    confirmed = pre.author_confirmation(marker, original(saved), request, bound)
    assets = restaged(pack, request_path)
    import douyin_browser as home
    with home.connect(profile, writing=True) as connection:
        page, page_tid = owned_page(connection, saved['target_id'], saved)
        observed = check_editor(page, pack, saved, assets)
        if action == 'verify':
            if request['job'].get('state') != 'submission_unknown':
                raise PublishError('抖音续跑核对只接受结果未知任务')
            saved.update(resume_original_state=base_state(saved),
                         state=VERIFIED, resume_verified_at=time.time(), resume_confirmation_digest=confirmed)
            write_json(marker, saved)
            return dict({k: pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                        state='not_submitted', absence_verified=True,
                        absence_scope='author_confirmed_uploaded_unfilled_unsent',
                        evidence_path=str(marker), observed_account_identity=bound['identity'])
        if (action != 'execute' or request['job'].get('state') != 'prepared'
                or saved.get('state') != VERIFIED or saved.get('resume_confirmation_digest') != confirmed):
            raise PublishError('抖音续跑须先通过独立核对')
        import douyin_cover as cover
        import douyin_publish_browser as sender
        anchor = sender.fresh_anchor(connection)
        try:
            uid = sender.native_account(anchor, bound['identity'], bound['cdp_url'])
        finally:
            anchor.close()
        if uid != saved.get('native_user_id'):
            raise PublishError('抖音原生账号身份已改变')
        was_partial = partial(saved); at_cover = cover_pending(saved); at_send = unclicked(saved)
        for k in RESUME_KEYS: saved.pop(k, None)
        if page_tid != saved['target_id']:
            saved['rebound_from_target_id'] = saved['target_id']; saved['target_id'] = page_tid
        saved.update(observed); saved['state'] = 'attempted'; write_json(marker, saved)
        if was_partial:
            clear_fields(page)
        if at_send:
            prior = {k: saved.pop(k, None) for k in UNCLICKED_KEYS}
            saved.setdefault('prior_unclicked_attempts', []).append(prior)
            saved['state'] = 'filled'; saved['phase'] = 'complete'; write_json(marker, saved)
            import douyin_focus
            douyin_focus.ensure(page); home.assert_identity(page, bound['identity']); home.no_warning(page)
            home.verify_fields(page, pack, saved)
        cover_sources = None if at_send else cover.prepare(page, assets, cover.requested(pack))
        if at_send:
            pass
        elif at_cover:
            import douyin_focus
            douyin_focus.ensure(page); home.assert_identity(page, bound['identity']); home.no_warning(page)
            def progress(phase):
                saved['phase'] = phase; write_json(marker, saved)
            saved['state'] = 'uploaded'; write_json(marker, saved)
            home.fill_after_fields(page, pack, assets, cover_sources, saved, marker, progress, cover_saved=True)
        else:
            home.fill_fields(page, pack, assets, cover_sources, bound['identity'], saved, marker)
        accepted = sender.submit_once(page, pack, bound['identity'], uid, bound['cdp_url'], saved, marker)
        if not saved.get('receipt_error'):
            try: page.close()
            except Exception: pass
        rid = accepted['remote_id']
        return dict({k: pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                    state='pending', remote_id=rid, remote_url='https://www.douyin.com/video/' + rid,
                    observed_account_identity=bound['identity'], tracking_scope='native_post_id',
                    content_verified=False)
