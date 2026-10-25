"""Passive Channels creator-list observation, not a publication verifier.

No constructed/replayed private requests, uploads, clicks or clipboard access.
The configured public sph ID is NOT the native finderUsername; bind the latter
privately before calling readback. Originality review is a separate fact.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from browser_runtime import background_window
from publish_core import PublishError, digest

ORIGIN = 'https://channels.weixin.qq.com'
MANAGER = ORIGIN + '/platform/post/list'
BASE = '/cgi-bin/mmfinderassistant-bin'
AUTH_PATH = BASE + '/auth/auth_data'
LIST_PATH = BASE + '/post/post_list'


def native_identity(value):
    # Native account names are opaque; do not derive them from a display name,
    # sph share ID, cookie presence or a selected account's position in a list.
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{3,256}', value)
            or value.startswith('sph')):
        raise PublishError('视频号须显式绑定原生 finderUsername，不能使用 sph 公开标识')
    return value


def object_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'export/[A-Za-z0-9_-]{8,256}', value):
        raise PublishError('视频号原生作品 ID 无效；不能用标题或分享链接代替')
    return value


def data(body):
    if (not isinstance(body, dict) or type(body.get('errCode')) is not int
            or body['errCode'] != 0 or not isinstance(body.get('data'), dict)):
        raise PublishError('视频号原生响应未成功或结构未覆盖')
    value = body['data']
    if 'baseResp' in value:
        response = value['baseResp']
        if (not isinstance(response, dict) or type(response.get('errcode')) is not int
                or response['errcode'] != 0):
            raise PublishError('视频号原生业务响应失败')
    return value


def account(body, expected):
    expected = native_identity(expected)
    value = data(body).get('finderUser')
    if not isinstance(value, dict) or value.get('finderUsername') != expected:
        raise PublishError('视频号当前登录账号与原生绑定不符')
    return expected


def states(value):
    """Keep independent dimensions; nothing here means content_verified."""
    status, visible = value.get('status'), value.get('visibleType')
    handle = value.get('handleStatus')
    effective = value.get('effectiveTime', 0)
    if (type(status) is not int or type(visible) is not int
            or visible not in {1, 2, 3} or type(effective) is not int or effective < 0
            or ('handleStatus' in value and type(handle) is not int)):
        raise PublishError('视频号状态、可见范围或定时字段缺失/未覆盖')
    processing = ({1: 'processing', 2: 'processed', 3: 'failed', 4: 'quota_exceeded',
                   5: 'failed_with_wording'}.get(handle, 'unknown')
                  if 'handleStatus' in value else 'not_reported')
    original = value.get('originalInfo')
    originality = dict(declared=None, audit='not_reported', native_audit_flag=None)
    if original is not None:
        if (not isinstance(original, dict) or type(original.get('isDeclared')) is not int
                or original['isDeclared'] not in {0, 1}
                or type(original.get('auditOriginalFlag')) is not int):
            raise PublishError('视频号原创申请与审核字段未覆盖')
        flag = original['auditOriginalFlag']
        originality = dict(declared=bool(original['isDeclared']), native_audit_flag=flag,
                           audit={0: 'pending', 1: 'passed', 2: 'rejected', 3: 'recreation',
                                  4: 'cocreation', 5: 'machine_passed'}.get(flag, 'unknown'))
    # status=1 is the observed native normal state, not proof of public playback.
    candidate = (status == 1 and visible == 1 and effective == 0
                 and processing in {'processed', 'not_reported'})
    return dict(native_status=status, visibility={1: 'public', 2: 'followers', 3: 'self'}[visible],
                processing=processing, effective_time=effective, scheduled=effective > 0,
                originality=originality, public_list_candidate=candidate,
                publication_verified=False)


def item(body, *, expected_native_identity, request_identity, remote_id):
    native_identity(expected_native_identity)
    if request_identity != expected_native_identity:
        raise PublishError('视频号作品列表请求未绑定同一原生账号')
    object_id(remote_id)
    rows = data(body).get('list')
    if not isinstance(rows, list):
        raise PublishError('视频号作品列表缺失')
    matches = [v for v in rows if isinstance(v, dict) and v.get('objectId') == remote_id]
    if len(matches) != 1:
        raise PublishError('当前列表未唯一找到指定视频号作品；不能据此认定不存在')
    value = matches[0]
    try:
        desc = value['desc']; media = desc['media']; title = desc.get('shortTitle', [])
        if (not isinstance(desc['description'], str) or not desc['description']
                or not isinstance(title, list) or len(title) > 1
                or title and (not isinstance(title[0], dict)
                              or not isinstance(title[0].get('shortTitle'), str))
                or type(desc['mediaType']) is not int
                or not isinstance(media, list) or len(media) != 1):
            raise ValueError()
        video = media[0]
        for field in ['videoPlayLen', 'width', 'height']:
            if type(video[field]) is not int or video[field] <= 0:
                raise ValueError()
        if (not isinstance(video['fileSize'], str)
                or not re.fullmatch(r'[1-9][0-9]{0,11}', video['fileSize'])
                or not isinstance(video['md5sum'], str)
                or not re.fullmatch(r'[a-fA-F0-9]{32}', video['md5sum'])):
            raise ValueError()
        thumb = video['thumbUrl']; url = urlsplit(thumb)
        if (url.scheme != 'https' or not url.netloc or url.username or url.password
                or url.fragment or any(c.isspace() for c in thumb)):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PublishError('视频号列表的原文、独立短标题或单媒体字段未覆盖') from exc
    return dict(remote_id=remote_id, native_identity=expected_native_identity,
                description=desc['description'], short_title=title[0]['shortTitle'] if title else '',
                native_media_type=desc['mediaType'],
                media=dict(duration_seconds=video['videoPlayLen'], width=video['width'],
                           height=video['height'], file_size=video['fileSize'],
                           md5=video['md5sum'].lower(), thumb_url=thumb), states=states(value))


def observe_visible(page, value):
    matches = []
    for card in page.locator('.post-feed-item').all():
        title = card.locator('.post-title')
        if title.count() == 1 and title.text_content() == value['description']:
            matches.append(card)
    if len(matches) != 1:
        raise PublishError('视频号可见列表缺少唯一完整文案卡片')
    card = matches[0]; image = card.locator('img.thumb')
    if (not card.is_visible() or image.count() != 1 or not image.is_visible()
            or image.evaluate('n=>({src:n.currentSrc,loaded:n.complete&&n.naturalWidth>0})')
            != dict(src=value['media']['thumb_url'], loaded=True)):
        raise PublishError('视频号可见列表封面与原生指定作品不符')


def readback(anchor, *, cdp_url, expected_native_identity, remote_id):
    """One bounded current-page read, no pagination/absence inference.

    This helper is intentionally not registered as a unified core verifier.
    It cannot prove source bytes, declarations, covers, topics, collection,
    public playback or a new engine's successful submission.
    """
    native_identity(expected_native_identity); object_id(remote_id)
    with background_window(anchor, cdp_url=cdp_url) as page:
        bodies = {AUTH_PATH: [], LIST_PATH: []}; errors = []

        def capture(response):
            u = urlsplit(response.url)
            if (u.scheme != 'https' or u.netloc != 'channels.weixin.qq.com'
                    or u.path not in bodies):
                return
            try:
                request = response.request
                if response.status != 200 or request.method != 'POST' or request.frame.page != page:
                    raise ValueError()
                payload = request.post_data_json
                if not isinstance(payload, dict):
                    raise ValueError()
                body = response.json()
                if u.path == AUTH_PATH:
                    account(body, expected_native_identity)
                    bodies[u.path].append(expected_native_identity)
                else:
                    bodies[u.path].append(item(body, expected_native_identity=expected_native_identity,
                                              request_identity=payload.get('_log_finder_id'),
                                              remote_id=remote_id))
            except Exception:
                errors.append(u.path)

        page.on('response', capture)
        try:
            page.goto(MANAGER, wait_until='domcontentloaded', timeout=25000)
            deadline = time.monotonic() + 20
            while not all(bodies.values()) and not errors and time.monotonic() < deadline:
                if urlsplit(page.url).path == '/login.html':
                    raise PublishError('视频号尚未登录；保留原扫码会话，不自动重开二维码')
                page.wait_for_timeout(100)
            if errors or not all(bodies.values()) or page.url != MANAGER:
                raise PublishError('视频号原生账号与指定作品响应未完成核对')
            if len({digest(v) for v in bodies[LIST_PATH]}) != 1:
                raise PublishError('视频号同一作品的列表响应矛盾')
            value = bodies[LIST_PATH][0]
            page.locator('.post-feed-item').first.wait_for(state='visible', timeout=10000)
            observe_visible(page, value)
            if errors or page.url != MANAGER:
                raise PublishError('视频号核验期间账号、作品或页面发生变化')
            return dict(value, observed_at=time.time(), observation_scope='native_creator_list_and_visible_card',
                        content_verified=False, source_media_verified=False,
                        unverified_fields=['public_identity_binding', 'source_media_bytes', 'independent_covers',
                                           'native_topics', 'collection_membership', 'activity', 'ai_declaration',
                                           'public_playback', 'short_share_link'])
        finally:
            page.remove_listener('response', capture)
