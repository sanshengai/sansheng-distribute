"""Read-only native creator-list observation; not a publication verifier.

The list omits source video IDs, declarations and full cover/collection fields.
Never use this observation alone to mark a core job content_verified.
"""
from __future__ import annotations
import re
import time
from urllib.parse import parse_qs, urlsplit

from browser_runtime import background_window, click_background
from publish_core import PublishError, digest

HOME = 'https://creator.douyin.com/creator-micro/home'
MANAGER = 'https://creator.douyin.com/creator-micro/content/manage'
ACCOUNT_PATH = '/aweme/v1/creator/pc/user/info/'
LIST_PATH = '/web/api/creator/item/list'
CARD = '[class^="video-card-content-"]'
TITLE = '[class^="info-title-text-"]'
COVER = '[class^="video-card-cover-"]'


def decimal(value):
    return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,24}', value) is not None


def account(body):
    if (not isinstance(body, dict) or type(body.get('status_code')) is not int
            or body['status_code'] != 0 or not decimal(body.get('uid'))
            or body.get('is_confer_login') is not False
            or body.get('is_log_as_person') is not True
            or body.get('is_registered') is not True):
        raise PublishError('抖音个人账号原生身份不明确；不推断授权或机构账号身份')
    return body['uid']


def item(body, uid, remote_id, expected_description=None):
    if not decimal(uid) or not decimal(remote_id):
        raise PublishError('抖音作者或作品 ID 无效')
    if (not isinstance(body, dict) or type(body.get('status_code')) is not int
            or body['status_code'] != 0 or not isinstance(body.get('items'), list)):
        raise PublishError('抖音作品列表原生响应无效')
    rows = [v for v in body['items'] if isinstance(v, dict) and v.get('id') == remote_id]
    if len(rows) != 1:
        raise PublishError('当前列表未唯一找到指定作品；不能据此认定作品不存在')
    value = rows[0]
    try:
        duration = value['video_info']['duration']
        uri = value['cover']['uri']
        description = value['description']
        if (value['user_id'] != uid or type(value['type']) is not int or value['type'] != 3
                or value.get('collaborative') is not False
                or type(value['review']['status']) is not int or value['review']['status'] != 2
                or value['visibility'] != dict(close_friend=0, part_see=0, status=1)
                or any(type(v) is not int for v in value['visibility'].values())
                or not decimal(duration) or int(duration) > 86400000
                or not isinstance(uri, str) or not re.fullmatch(r'tos-cn-i-dy/[A-Za-z0-9_-]+', uri)
                or not isinstance(description, str) or not description
                or expected_description is not None and description != expected_description):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise PublishError('抖音作品作者、文案、视频或公开审核状态不符') from exc
    return dict(remote_id=remote_id, native_user_id=uid, description=description,
                duration_ms=int(duration), cover_uri=uri, native_review_status=2,
                native_visibility_status=1)


def cover_uri(css):
    match = re.fullmatch(r'url\("(https://[^"\s]+)"\)', css or '')
    if not match:
        raise PublishError('抖音作品可见封面不是唯一原生图片')
    u = urlsplit(match[1])
    if (u.username or u.password or not re.fullmatch(r'[A-Za-z0-9-]+\.douyinpic\.com', u.netloc)):
        raise PublishError('抖音作品可见封面来源不符')
    match = re.fullmatch(r'/(tos-cn-i-dy/[A-Za-z0-9_-]+)~[^/]+', u.path)
    if not match:
        raise PublishError('抖音作品可见封面标识未覆盖')
    return match[1]


def observe_visible(page, value):
    matches = []
    for card in page.locator(CARD).all():
        title = card.locator(TITLE)
        if title.count() == 1 and title.text_content() == value['description']:
            matches.append(card)
    if len(matches) != 1:
        raise PublishError('抖音作品列表缺少唯一完整文案卡片')
    card = matches[0]; cover = card.locator(COVER)
    if (not card.is_visible() or cover.count() != 1 or not cover.is_visible()
            or card.get_by_text('已发布', exact=True).count() != 1
            or not card.get_by_text('已发布', exact=True).is_visible()
            or cover_uri(cover.evaluate('n=>getComputedStyle(n).backgroundImage')) != value['cover_uri']):
        raise PublishError('抖音可见卡片的封面或发布状态与原生响应不符')
    return card


def readback(anchor, *, cdp_url, public_identity, remote_id, expected_description=None, include_details=False):
    """Observe one currently listed work in an owned no-focus window.

    No pagination guesses, no direct private API calls, no editor mutation.
    A missing item on this page is inconclusive, never an absence proof.
    """
    if type(include_details) is not bool:
        raise PublishError('抖音详情观察选项须为布尔值')
    if (not isinstance(public_identity, str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', public_identity) or not decimal(remote_id)):
        raise PublishError('抖音只读查询身份无效')
    with background_window(anchor, cdp_url=cdp_url) as page:
        bodies = {ACCOUNT_PATH: [], LIST_PATH: []}; errors = []
        if include_details:
            from douyin_post_details import DETAIL_PATH, TRANSCODE_PATH
            bodies.update({DETAIL_PATH: [], TRANSCODE_PATH: []})
        def capture(response):
            u = urlsplit(response.url)
            if (u.scheme != 'https' or u.netloc != 'creator.douyin.com'
                    or u.path not in bodies or response.request.method != 'GET'):
                return
            try:
                if response.status != 200:
                    raise ValueError()
                body = response.json()
                if include_details and u.path == TRANSCODE_PATH:
                    ids = parse_qs(u.query).get('video_id', [])
                    body = dict(video_id=ids[0] if len(ids)==1 else None, body=body)
                bodies[u.path].append(body)
            except Exception:
                errors.append(u.path)
        page.on('response', capture)
        def wait(path):
            deadline = time.monotonic() + 15
            while not bodies[path] and not errors and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            if errors or not bodies[path]:
                raise PublishError('抖音只读原生响应尚未就绪')
        page.goto(HOME, wait_until='domcontentloaded', timeout=25000)
        label = page.get_by_text(re.compile(r'^抖音号[：:]?' + re.escape(public_identity) + '$'))
        label.wait_for(state='visible', timeout=15000)
        if page.url != HOME or label.count() != 1:
            raise PublishError('抖音只读账号页面不唯一')
        wait(ACCOUNT_PATH)
        ids = {account(b) for b in bodies[ACCOUNT_PATH]}
        if len(ids) != 1:
            raise PublishError('抖音原生作者身份矛盾')
        uid = next(iter(ids))
        # Use the observed native navigation. A direct manager reload can stay
        # in its loading shell without producing the native work-list request.
        click_background(page, page.get_by_text('内容管理', exact=True))
        wait(LIST_PATH)
        page.locator(CARD).first.wait_for(state='visible', timeout=15000)
        if page.url != MANAGER or errors:
            raise PublishError('抖音只读作品页发生跳转或响应错误')
        values = [item(b, uid, remote_id, expected_description) for b in bodies[LIST_PATH]]
        if len({digest(v) for v in values}) != 1:
            raise PublishError('抖音指定作品的独立原生响应矛盾')
        value = values[0]; card = observe_visible(page, value)
        result = dict(value, observed_at=time.time(), observed_account_identity=public_identity,
                    observation_scope='native_creator_list_and_visible_card',
                    content_verified=False, source_media_verified=False,
                    unverified_fields=['source_video_identity', 'horizontal_cover', 'vertical_cover',
                                       'declaration', 'collection_membership', 'activity', 'schedule'])
        if include_details:
            from douyin_post_details import read
            result['details'] = read(page,card,value,public_identity,bodies,wait)
            result['observation_scope'] = 'native_creator_list_editor_details_and_visible_fields'
            result['unverified_fields'] = ['source_video_binding','source_cover_bindings','activity','schedule']
        return result
