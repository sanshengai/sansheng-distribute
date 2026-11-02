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


# X may also stop right after opening its owned window (composer never rendered).
X_EARLY = 'owned_window_opened'


def eligible(saved, platform):
    phase = saved.get('preinput_original_state', saved.get('state'))
    allowed = {STAGES.get(platform)} | ({X_EARLY} if platform == 'x' else set())
    if phase not in allowed or any(k in saved for k in SENDING):
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


AUTHOR_CONFIRMABLE = {'douyin', 'x'}
CONFIRMATION_FIELDS = {'schema_version', 'job_key', 'attempt_digest', 'identity', 'author_statement',
                       'confirmed_at', 'listing_check'}
LISTING_FIELDS = {'observed_at', 'account_identity', 'query', 'matches', 'evidence_path'}


def confirmation_path(marker):
    return marker.with_name(marker.name + '.author-unsubmitted.json')


def author_confirmation(marker, saved, request, bound):
    """Validate an explicit author statement that a pre-upload attempt never left the machine.

    Only for an attempt already proven eligible (no file selected, no upload
    events, no send traces), whose owned window has disappeared. The file is
    written by ``confirm_unsubmitted.py`` and bound to the exact attempt record.
    """
    from datetime import datetime
    path = confirmation_path(marker)
    if not path.exists():
        raise PublishError('首次上传前原窗口已丢失；需作者书面确认未提交（confirm_unsubmitted.py），否则不能认定未提交')
    value = read_json(path)
    original = {k: v for k, v in saved.items() if k not in {
        'state', 'preinput_original_state', 'preinput_verified_at', 'preinput_scope',
        'author_confirmation_digest', 'preinput_original_digest'}}
    original['state'] = saved.get('preinput_original_state', saved.get('state'))
    listing = value.get('listing_check') if isinstance(value, dict) else None
    try:
        confirmed = datetime.fromisoformat(value['confirmed_at']); observed = datetime.fromisoformat(listing['observed_at'])
        ok_time = confirmed.tzinfo is not None and observed.tzinfo is not None
    except (KeyError, TypeError, ValueError):
        ok_time = False
    if (not isinstance(value, dict) or set(value) != CONFIRMATION_FIELDS or value['schema_version'] != 1
            or value['job_key'] != request['job'].get('key') or value['attempt_digest'] != digest(original)
            or value['identity'] != bound['identity'] or request['job'].get('remote_id')
            or not isinstance(value['author_statement'], str) or len(value['author_statement'].strip()) < 4
            or not isinstance(listing, dict) or set(listing) != LISTING_FIELDS
            or listing['account_identity'] != bound['identity'] or listing['matches'] != []
            or not isinstance(listing['query'], str) or not listing['query'].strip()
            or not isinstance(listing['evidence_path'], str) or not Path(listing['evidence_path']).is_file()
            or not ok_time):
        raise PublishError('作者未提交确认不完整或与原尝试不符；保持未知')
    return digest(value)


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


def archived_without_new_attempt(action, request, pack, bound, marker):
    """A verified pre-input attempt was archived, then the new run stopped before
    writing its own attempt record. New records are written before any navigation,
    so no record means no new platform input; only verify may report this."""
    if action != 'verify' or request['job'].get('remote_id') or request['job'].get('state') != 'submission_unknown':
        return None
    archives = [p for p in list(marker.parent.glob(marker.name + '.preinput-*'))
                + list(marker.parent.glob(marker.name + '.released-*')) if p.is_file()]
    archives.sort(key=lambda p: p.stat().st_mtime)
    if not archives:
        return None
    last = read_json(archives[-1])
    import douyin_unsent_release as unsent
    verified = ((last.get('state') == 'preinput_absence_verified' and eligible(last, pack['platform']))
                or (last.get('state') == 'unsent_release_verified' and unsent.eligible(last, pack['platform'])))
    if not verified or any(last.get(k) != v for k, v in bound.items()):
        return None
    return dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},
                state='not_submitted', absence_verified=True,
                absence_scope='archived_preinput_attempt_no_new_record',
                evidence_path=str(archives[-1]), observed_account_identity=bound['identity'])


def recover(action, request, pack, bound, marker, profile):
    if not marker.exists():
        found = archived_without_new_attempt(action, request, pack, bound, marker)
        if found is not None or any(marker.parent.glob(marker.name + '.preinput-*')) \
                or any(marker.parent.glob(marker.name + '.released-*')):
            return found
        from no_attempt_recovery import recover as no_attempt
        return no_attempt(action, request, pack, bound, marker)
    saved = read_json(marker); platform = pack['platform']
    if not eligible(saved, platform):
        import douyin_unsent_release as unsent
        if unsent.eligible(saved, platform) and (confirmation_path(marker).exists()
                                                 or saved.get('state') == 'unsent_release_verified'):
            return unsent.release(action, request, pack, bound, marker, profile, saved)
        return None
    if any(saved.get(k) != v for k,v in bound.items()) or request['job'].get('remote_id'):
        raise PublishError('首次上传前原任务绑定不符')
    with connect(profile) as browser:
        if platform in {'weibo','douyin'}:
            from browser_runtime import select_page
            own_anchor = False
            try:
                anchor = select_page(browser,platform,target_id=saved.get('anchor_target_id'))
            except PublishError:
                if platform != 'douyin': raise
                from douyin_publish_browser import fresh_anchor
                anchor = fresh_anchor(browser); own_anchor = True
            if platform == 'weibo':
                from weibo_browser import assert_identity
            else:
                from douyin_browser import assert_identity
            try: assert_identity(anchor,bound['identity'])
            finally:
                if own_anchor: anchor.close()
        pages = []
        for ctx in browser.contexts:
            for page in ctx.pages:
                session = ctx.new_cdp_session(page)
                try: tid = session.send('Target.getTargetInfo')['targetInfo']['targetId']
                finally: session.detach()
                if tid == saved['target_id']: pages.append(page)
        confirmed = None
        early_x = platform == 'x' and saved.get('preinput_original_state', saved.get('state')) == X_EARLY
        if len(pages) == 1:
            if early_x:
                from x_compose_ready import realize
                try: realize(pages[0])
                except Exception: pass
            prove(pages[0], saved, platform)
            if early_x and not (saved.get('preinput_self_check') or {}).get('ok'):
                # Older attempts lack the executor's own read-only self-check;
                # then an explicit author confirmation bound to this attempt is required.
                confirmed = author_confirmation(marker, saved, request, bound)
            if action == 'execute' and saved.get('state') == 'preinput_absence_verified' and platform == 'douyin':
                # Proven empty again; close only this task's own window so the
                # fresh run sees a unique anchor page.
                pages[0].close()
        elif (not pages and platform in {'douyin', 'x'} and saved.get('state') == 'preinput_absence_verified'
              and not saved.get('preinput_scope')):
            # Already proven against the live window before it closed; that
            # verified record was persisted before any later step.
            pass
        elif not pages and platform in AUTHOR_CONFIRMABLE:
            # The owned window is gone; only an explicit author statement
            # bound to this exact attempt can stand in for the page proof.
            confirmed = author_confirmation(marker, saved, request, bound)
        else:
            raise PublishError('首次上传前原窗口已丢失；不能认定未提交')
    if action == 'verify':
        original = digest(saved)
        saved.update(preinput_original_state=saved.get('preinput_original_state',saved['state']),
                     state='preinput_absence_verified', preinput_verified_at=time.time(),
                     **({'preinput_scope':'author_confirmed_owned_window_lost',
                         'author_confirmation_digest':confirmed,'preinput_original_digest':original}
                        if confirmed else {}))
        write_json(marker,saved)
        return dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},
                    state='not_submitted', absence_verified=True,
                    absence_scope='author_confirmed_before_first_platform_upload' if confirmed
                    else 'owned_editor_before_first_platform_upload',
                    evidence_path=str(marker), observed_account_identity=bound['identity'])
    if confirmed is not None and saved.get('state') == 'preinput_absence_verified' and (
            saved.get('preinput_scope') != 'author_confirmed_owned_window_lost'
            or saved.get('author_confirmation_digest') != confirmed):
        raise PublishError('作者未提交确认与已核对记录不符；保持未知')
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
