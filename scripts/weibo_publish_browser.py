"""Opt-in ordinary Weibo text/images submission; durable one-click attempt."""
from __future__ import annotations
import json
import os
import time
from urllib.parse import urlsplit

from browser_assets import stage
from browser_runtime import background_window, click_background
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json
import weibo_browser as home
import weibo_post as post


def checkpoint(saved, marker, **changes):
    """Do not advance in-memory acceptance before the original marker is durable."""
    value = dict(saved, **changes)
    write_json(marker, value)
    saved.update(value)


class Capture:
    """Observe the native UI response; never replay/construct a private request."""
    def __init__(self, uid, save):
        self.uid, self.save = uid, save
        self.accepted = None; self.error = None; self.count = 0

    def response(self, response):
        u = urlsplit(response.url)
        if u.scheme != 'https' or u.netloc != 'weibo.com' or u.path != '/ajax/statuses/update': return
        self.count += 1
        try:
            if self.count != 1 or response.request.method != 'POST' or response.status != 200:
                raise PublishError('微博原生发送响应数量、方法或状态异常')
            body = response.json()
            if (not isinstance(body, dict) or body.get('ok') != 1 or body.get('error')
                    or not isinstance(body.get('data'), dict) or body['data'].get('verify')):
                raise PublishError('微博原生发送未受理或需要本人验证')
            value = post.identity(body['data'], self.uid)
            accepted = dict(value, observed_at=time.time(), response_digest=digest(body))
            self.save(accepted)  # Actual post identity precedes click completion.
            self.accepted = accepted
        except Exception:
            self.error = '微博原生受理身份未能完整核对或保存；不重复发送'


def ready(page, pack, uid, saved):
    home.assert_identity(page, uid); home.assert_no_warning(page)
    card, node = home.editor(page)
    if node.input_value() != pack['fields']['composed_text']:
        raise PublishError('微博发送前完整文案已改变；保留原稿，不发送')
    post.check_uploads(pack, saved.get('uploaded'))
    home.assert_media(card, saved['uploaded'])
    public = card.locator('[title="公开"]')
    if public.count() != 1 or not public.is_visible() or public.inner_text() != '公开':
        raise PublishError('微博发送前不能核对公开可见性；不改变作者设置')
    return card


def submit_once(page, pack, uid, saved, marker):
    if saved.get('submission_attempted_at') is not None or saved.get('accepted'):
        raise PublishError('微博已有发送尝试；只查询原作品，不再次点击')
    card = ready(page, pack, uid, saved)
    button = card.get_by_role('button', name='发送', exact=True)
    if button.count() != 1 or not button.is_visible() or button.is_disabled():
        raise PublishError('微博发送按钮不唯一、不可见或禁用')
    checkpoint(saved, marker, submission_attempted_at=time.time(), state='submission_unknown')

    def accepted(value):
        checkpoint(saved, marker, accepted=value, state='accepted')

    capture = Capture(uid, accepted); page.on('response', capture.response)
    def before_input():
        ready(page, pack, uid, saved)
        checkpoint(saved, marker, native_input_checkpointed_at=time.time())
    try:
        ready(page, pack, uid, saved)
        try:
            click_background(page, button, before_input=before_input)
        except Exception as exc:
            saved['click_error_type'] = type(exc).__name__
            saved['click_error_phase'] = ('native_input_uncertain' if saved.get('native_input_checkpointed_at')
                                         else 'before_native_input')
            if isinstance(exc, PublishError): saved['click_error'] = str(exc)[:300]
            if saved['click_error_phase'] == 'before_native_input':
                raise PublishError('微博原生点击前检查未完成；保留同一稿，不自动重试') from exc
            saved['click_reply_unknown'] = True
        deadline = time.monotonic() + 20
        while capture.accepted is None and capture.error is None and time.monotonic() < deadline:
            page.wait_for_timeout(100)
    finally:
        page.remove_listener('response', capture.response)
        saved['native_response_count'] = capture.count
        if capture.error: saved['receipt_error'] = capture.error
        write_json(marker, saved)
    if not saved.get('accepted'):
        raise PublishError('微博发送结果未知且没有可核对作品 ID；不重复发送')
    return saved['accepted']


def dispatch(request_path, action, profile, lock_fd):
    if action not in {'execute', 'verify'}: raise PublishError('不支持的微博发送阶段')
    try: os.fstat(lock_fd)
    except OSError as exc: raise PublishError('微博发送须继承核心账号锁') from exc
    request = read_json(request_path); manifest = read_json(request['manifest_path'])
    if digest(manifest) != request['manifest_digest'] or request['target'] not in manifest.get('targets', []):
        raise PublishError('微博发送请求与封存清单不符')
    target = request['target']; uid, endpoint, browser = home.settings(profile, target)
    if action == 'execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('微博账号受限，只准备本地包')
    pack = build_pack(manifest, target['id'], profile, check_files=action == 'execute')
    home.check_pack(pack, publishing=True)
    if pack['content_type'] not in {'text', 'images'} or pack['endpoint'] != 'publish':
        raise PublishError('微博发送候选仅承接普通 text/images:publish')
    bound = dict(pack_digest=digest(pack), identity=uid, cdp_url=endpoint, browser_profile=browser)
    marker = home.marker_path(request_path)
    if action == 'execute' and marker.exists():
        raise PublishError('微博任务已有发送尝试；只查询，不重填或重传')
    if action == 'verify':
        saved = read_json(marker)
        if any(saved.get(k) != v for k, v in bound.items()) or not saved.get('anchor_target_id'):
            raise PublishError('微博原发送绑定已改变；不认领其他作品')
        if not saved.get('submission_attempted_at') or not saved.get('accepted') or saved.get('receipt_error'):
            raise PublishError('微博缺少完整原生受理身份；保持未知，不重新发送')
        accepted = saved['accepted']; post.status_url(uid, accepted.get('remote_id'))
        if (accepted.get('identity') != uid
                or request['job'].get('remote_id') not in (None, accepted['remote_id'])):
            raise PublishError('微博原作品或作者身份与核心账本矛盾')
        post.check_uploads(pack, saved.get('uploaded'))
    from browser_engine import target_id
    with home.connect(profile, writing=action == 'execute') as connection:
        anchor = home.select_page(connection, 'weibo', path='/',
                                  **(dict(target_id=saved['anchor_target_id']) if action == 'verify' else {}))
        home.assert_identity(anchor, uid)
        if action == 'execute':
            assets = stage(pack, request_path) if pack['assets'] else []
            with background_window(anchor, cdp_url=endpoint, retain=True) as page:
                saved = dict(bound, target_id=target_id(page), anchor_target_id=target_id(anchor),
                             state='owned_window_opened', submission_protocol=2, uploaded=[])
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as out:
                    json.dump(saved, out); out.flush(); os.fsync(out.fileno())
                page.goto('https://weibo.com/', wait_until='domcontentloaded', timeout=25000)
                page.locator('#homeWrap > .woo-panel-main textarea').wait_for(state='visible', timeout=20000)
                home.fill(page, pack, assets, uid, saved, marker)
                saved['state'] = 'filled'; write_json(marker, saved)
                accepted = submit_once(page, pack, uid, saved, marker)
                if not saved.get('receipt_error'):
                    try:
                        page.close()  # Only our owned composer, after its actual ID is durable.
                        saved['owned_composer_closed_after_acceptance'] = True
                    except Exception as exc:
                        saved['owned_composer_cleanup_error'] = type(exc).__name__
                    write_json(marker, saved)
                return dict({k:pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                            state='pending', remote_id=accepted['remote_id'],
                            remote_url=post.status_url(uid, accepted['remote_id']),
                            observed_account_identity=uid, tracking_scope='native_post_id')
        observed = post.readback(anchor, pack, uid, accepted, saved['uploaded'], endpoint)
        write_json(marker.with_suffix('.weibo-readback.json'), dict(observed, remote_id=accepted['remote_id']))
        return dict({k:pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                    **observed, state='published', remote_id=accepted['remote_id'],
                    observed_account_identity=uid, tracking_scope='native_post_id')
