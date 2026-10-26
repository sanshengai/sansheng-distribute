"""Native X creation receipts and independent ordinary-post readback.

Observe responses produced by the visible native UI; never replay GraphQL or
construct signed/private API requests. A missing receipt remains unknown.
"""
from __future__ import annotations
import html
import re
import time
from urllib.parse import parse_qs, urlsplit

from browser_runtime import background_tab, background_window, render_background_page
from publish_core import PublishError, digest


def status_url(identity, remote_id):
    if (not isinstance(identity, str) or not re.fullmatch(r'@[A-Za-z0-9_]{1,15}', identity)
            or not isinstance(remote_id, str) or not re.fullmatch(r'[1-9][0-9]{0,24}', remote_id)):
        raise PublishError('X 作品身份或账号无效')
    return 'https://x.com/' + identity[1:] + '/status/' + remote_id


def _expand(text, entities):
    if not isinstance(text, str) or not isinstance(entities, dict):
        raise PublishError('X 原生完整文字或实体缺失')
    urls = entities.get('urls', [])
    if not isinstance(urls, list):
        raise PublishError('X 原生链接实体无效')
    encoded = text.encode('utf-16-le'); replacements = []
    for entity in urls:
        try:
            start, end = entity['indices']
            original, expanded = entity['url'], entity['expanded_url']
            parts = urlsplit(expanded)
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(encoded)//2
                    or not isinstance(original, str) or parts.scheme not in {'http', 'https'}
                    or not parts.netloc or parts.username or parts.password
                    or encoded[start*2:end*2].decode('utf-16-le') != original):
                raise ValueError()
            replacements.append((start, end, expanded))
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise PublishError('X 原生链接位置或展开地址不能核对') from exc
    replacements.sort()
    if any(a[1] > b[0] for a, b in zip(replacements, replacements[1:])):
        raise PublishError('X 原生链接位置重叠')
    for start, end, expanded in reversed(replacements):
        encoded = encoded[:start*2] + expanded.encode('utf-16-le') + encoded[end*2:]
    return html.unescape(encoded.decode('utf-16-le'))


def acceptance_identity(result, identity=None, *, require_typename=True):
    """Retain an actual own-post ID even if later content checks cannot finish."""
    if (not isinstance(result, dict) or result.get('__typename') not in ({'Tweet'} if require_typename else {None,'Tweet'})):
        raise PublishError('X 返回的不是可核对的普通作品')
    legacy = result.get('legacy', {})
    user = result.get('core', {}).get('user_results', {}).get('result', {})
    remote_id = result.get('rest_id')
    handle = user.get('core', {}).get('screen_name') or user.get('legacy', {}).get('screen_name')
    if (not isinstance(legacy, dict) or remote_id != legacy.get('id_str')
            or not isinstance(handle, str) or not isinstance(user.get('rest_id'), str)
            or not re.fullmatch(r'[1-9][0-9]*', user['rest_id'])
            or legacy.get('user_id_str') != user['rest_id']):
        raise PublishError('X 原生作品与作者身份缺失或矛盾')
    status_url('@' + handle, remote_id)
    value = dict(remote_id=remote_id, identity='@' + handle.lower(), author_id=user['rest_id'])
    if identity is not None and value['identity'] != identity:
        raise PublishError('X 受理作品属于其他账号；不绑定该身份')
    return value


def facts(result):
    """Extract one native Tweet, retaining media and privacy as explicit facts."""
    value = acceptance_identity(result)
    legacy = result['legacy']; user = result['core']['user_results']['result']
    text, entities = legacy.get('full_text'), legacy.get('entities')
    note = result.get('note_tweet', {}).get('note_tweet_results', {}).get('result')
    if note is not None:
        if not isinstance(note, dict):
            raise PublishError('X 扩展文字结果无效')
        text, entities = note.get('text'), note.get('entity_set')
    media = legacy.get('extended_entities', {}).get('media', [])
    entity_media = legacy.get('entities', {}).get('media', [])
    if not isinstance(media, list) or not isinstance(entity_media, list):
        raise PublishError('X 原生媒体列表无效')
    attachments = []; photo_urls = []
    for item in media:
        if not isinstance(item, dict):
            raise PublishError('X 原生附件身份无效')
        original = item.get('original_info', {})
        native = dict(media_id=item.get('id_str'), media_key=item.get('media_key'),
            type=item.get('type'), width=original.get('width'), height=original.get('height'))
        if item.get('type') == 'photo':
            if (item.get('video_info') or item.get('ext_media_availability', {}).get('status')
                    not in {None, 'Available'}):
                raise PublishError('X 原生图片不可用或出现未交接视频')
            photo_source(item.get('media_url_https'))
            photo_urls.append(item['media_url_https'])
        else:
            native.update(duration_millis=item.get('video_info', {}).get('duration_millis'),
                          availability=item.get('ext_media_availability', {}).get('status'))
        attachments.append(native)
    # Ordinary media tweets append one transport URL. Remove only the exact
    # native entity at the UTF-16 tail, never an authored URL or arbitrary suffix.
    chosen_media = entities.get('media', []) if isinstance(entities, dict) else []
    if chosen_media:
        if (not isinstance(chosen_media, list) or not 1 <= len(media) <= 4
                or len(chosen_media) not in {1, len(media)}):
            raise PublishError('X 附件正文实体尚不支持或不能唯一核对')
        item = chosen_media[0]
        try:
            url = item['url']; start, end = item['indices']; encoded = text.encode('utf-16-le')
            if (type(start) is not int or type(end) is not int or start <= 0
                    or end != len(encoded)//2 or not re.fullmatch(r'https://t\.co/[A-Za-z0-9]+', url)
                    or encoded[start*2:end*2].decode('utf-16-le') != url
                    or encoded[(start-1)*2:start*2] != b' \x00'
                    or any(chosen.get('id_str') != original.get('id_str')
                           or chosen.get('media_key') != original.get('media_key')
                           or chosen.get('url') != url or chosen.get('indices') != [start, end]
                           for chosen, original in zip(chosen_media, media))):
                raise ValueError()
            text = encoded[:(start-1)*2].decode('utf-16-le')
        except (KeyError, TypeError, ValueError, UnicodeError, AttributeError) as exc:
            raise PublishError('X 附件自动链接的位置或媒体身份不符') from exc
    protected = user.get('privacy', {}).get('protected', user.get('legacy', {}).get('protected'))
    return dict(value, text=_expand(text, entities),
                url_mappings=[dict(url=e['url'], expanded_url=e['expanded_url']) for e in entities.get('urls', [])],
                media_count=max(len(media), len(entity_media)),
                media=attachments,
                photo_urls=photo_urls,
                protected=protected, reply_to=legacy.get('in_reply_to_status_id_str'),
                quoted=legacy.get('is_quote_status'),
                reposted=bool(legacy.get('retweeted_status_result')),
                limited=bool(result.get('limited_actions') or result.get('tweet_interstitial')))


def verify_facts(value, pack, identity, *, remote_id=None, author_id=None, expected_media=None):
    if (value['identity'] != identity or (remote_id is not None and value['remote_id'] != remote_id)
            or (author_id is not None and value['author_id'] != author_id)):
        raise PublishError('X 原生作品或账号与发送回执不符')
    if value['text'] != pack['fields']['composed_text']:
        raise PublishError('X 已受理作品的完整文字或链接与封存内容不符')
    if (value['reply_to'] or value['quoted'] is not False
            or value['reposted'] or value['limited'] or value['protected'] is not False):
        raise PublishError('X 普通公开文字帖的媒体、回复、引用或可见性不能核对')
    if pack.get('content_type', 'text') == 'video':
        check_publication_media(pack, expected_media)
        actual=value.get('media');expected=expected_media['native']
        matched=isinstance(actual,list) and len(actual)==1 and isinstance(actual[0],dict)
        if matched:
            observed=dict(actual[0]);duration=observed.pop('duration_millis',None)
            reference=dict(expected);wanted=reference.pop('duration_millis',None)
            # MP4 native metadata rounds fractional milliseconds; X truncates.
            matched=(observed==reference and type(duration) is int and type(wanted) is int
                     and duration>0 and wanted>0 and abs(duration-wanted)<=1)
        if value['media_count'] != 1 or not matched:
            raise PublishError('X 已发布视频身份、尺寸、时长或可用状态与原片回执不符')
    elif pack.get('content_type') == 'images':
        check_publication_media(pack, expected_media)
        if (value['media_count'] != len(expected_media)
                or value.get('media') != [r['native'] for r in expected_media]
                or len(value.get('photo_urls', [])) != len(expected_media)):
            raise PublishError('X 已发布图片身份、尺寸或次序与原图上传回执不符')
    elif value['media_count'] or expected_media is not None:
        raise PublishError('X 普通公开文字帖出现未交接媒体')
    return value


def check_publication_media(pack, expected):
    """Bind a processed native video to this immutable asset, not just a count."""
    if pack.get('content_type') == 'images':
        try:
            assets = pack['assets']
            if not isinstance(expected, list) or not 1 <= len(expected) == len(assets) <= 4:
                raise ValueError()
            for asset, row in zip(assets, expected):
                native = row['native']
                if (row['source_asset_id'] != asset['id'] or row['source_sha256'] != asset['sha256']
                        or not isinstance(native['media_id'], str)
                        or not re.fullmatch(r'[0-9]{1,32}', native['media_id'])
                        or native['media_key'] != '3_' + native['media_id'] or native['type'] != 'photo'
                        or any(type(native[k]) is not int or not 0 < native[k] <= 4096
                               for k in ('width', 'height'))): raise ValueError()
            if len({r['native']['media_id'] for r in expected}) != len(assets): raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PublishError('X 图文发布缺少与封存原图绑定的媒体证据') from exc
        return
    try:
        assets = pack['assets']; native = expected['native']
        if (pack['content_type'] != 'video' or len(assets) != 1
                or expected['source_asset_id'] != assets[0]['id']
                or expected['source_sha256'] != assets[0]['sha256']
                or not re.fullmatch(r'[0-9]{1,32}', native['media_id'])
                or not isinstance(native['media_key'], str) or not native['media_key']
                or native['type'] != 'video' or native['availability'] != 'Available'
                or any(type(native[k]) is not int or native[k] <= 0
                       for k in ('width', 'height', 'duration_millis'))):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PublishError('X 视频发布缺少与封存原片绑定的媒体证据') from exc


def photo_source(url):
    """Native photo CDN identity, allowing only format/size presentation queries."""
    try:
        u = urlsplit(url); query = parse_qs(u.query, keep_blank_values=True)
        match = re.fullmatch(r'/media/([A-Za-z0-9_-]+)(?:\.(jpg|jpeg|png|webp))?', u.path)
        if (u.scheme != 'https' or u.netloc != 'pbs.twimg.com' or u.fragment or not match
                or set(query) - {'format', 'name'} or any(len(v) != 1 for v in query.values())):
            raise ValueError()
        fmt = match[2] or query.get('format', [None])[0]
        if (fmt not in {'jpg', 'jpeg', 'png', 'webp'}
                or ('format' in query and query['format'][0] != fmt)
                or ('name' in query and not re.fullmatch(r'[a-zA-Z0-9_]{1,32}', query['name'][0]))):
            raise ValueError()
        return match[1], fmt
    except (TypeError, ValueError, AttributeError) as exc:
        raise PublishError('X 可见图片或原生图片地址无法核对') from exc


class Capture:
    """Persist the actual native acceptance ID before click completion returns."""
    def __init__(self, pack, identity, save):
        self.pack, self.identity, self.save = pack, identity, save
        self.accepted = None; self.error = None; self.count = 0; self.diagnostic = None

    def response(self, response):
        parts = urlsplit(response.url)
        if (parts.scheme != 'https' or parts.netloc != 'x.com'
                or not re.fullmatch(r'/i/api/graphql/[^/]+/CreateTweet', parts.path)):
            return
        self.count += 1
        try:
            if self.count != 1 or response.request.method != 'POST' or response.status != 200:
                raise PublishError('X 原生投稿响应数量、方法或状态异常')
            body = response.json()
            if not isinstance(body, dict) or body.get('errors'):
                raise PublishError('X 原生投稿返回错误；不重复点击')
            result = body['data']['create_tweet']['tweet_results']['result']
            user=result.get('core',{}).get('user_results',{}).get('result',{})
            self.diagnostic=dict(result_type=result.get('__typename'), result_keys=list(result),
                legacy_keys=list(result.get('legacy',{})), user_keys=list(user))
            # CreateTweet omits __typename in the observed native response;
            # TweetDetail still requires it. IDs and author bindings stay exact.
            value = acceptance_identity(result, self.identity, require_typename=False)
            accepted = dict(value, observed_at=time.time(), response_digest=digest(body))
            self.save(accepted)
            self.accepted = accepted
        except Exception as exc:
            self.diagnostic=dict(self.diagnostic or {},error=str(exc) if isinstance(exc,PublishError) else type(exc).__name__)
            self.error = 'X 原生受理回执未能完整核对或保存；保持未知，不重复点击'


def detail_result(body, remote_id):
    if not isinstance(body, dict) or body.get('errors'):
        raise PublishError('X 原生作品查询未完成')
    matches = {}
    def walk(node):
        if isinstance(node, dict):
            if node.get('__typename') == 'Tweet' and node.get('rest_id') == remote_id:
                matches[digest(node)] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    instructions = body.get('data', {}).get('threaded_conversation_with_injections_v2', {}).get('instructions')
    if not isinstance(instructions, list):
        raise PublishError('X 原生作品查询结构改变')
    walk(instructions)
    if len(matches) != 1:
        raise PublishError('X 独立查询缺少唯一的原作品身份')
    return facts(next(iter(matches.values())))


def readback(anchor, pack, identity, accepted, *, expected_media=None, cdp_url=None):
    remote_id = accepted.get('remote_id'); url = status_url(identity, remote_id)
    bodies = []; errors = []
    surface = (background_window(anchor, cdp_url=cdp_url)
               if pack.get('content_type') == 'video' else background_tab(anchor))
    with surface as page:
        def observe(response):
            parts = urlsplit(response.url)
            if (parts.scheme == 'https' and parts.netloc == 'x.com'
                    and re.fullmatch(r'/i/api/graphql/[^/]+/TweetDetail', parts.path)):
                try:
                    if response.status != 200 or len(bodies) >= 8:
                        raise ValueError()
                    bodies.append(response.json())
                except Exception:
                    errors.append('native_detail_unavailable')
        page.on('response', observe)
        page.goto(url, wait_until='domcontentloaded', timeout=25000)
        render_background_page(page)
        page.locator('[data-testid="tweet"]').first.wait_for(state='attached', timeout=20000)
        if page.url != url or errors or not bodies:
            raise PublishError('X 原作品页或独立原生查询未就绪；只保留已受理身份')
        values = []
        for body in bodies:
            value = detail_result(body, remote_id)
            values.append(verify_facts(value, pack, identity, remote_id=remote_id,
                                       author_id=accepted.get('author_id'), expected_media=expected_media))
        if len({digest(v) for v in values}) != 1:
            raise PublishError('X 独立作品查询结果不一致')
        href = '/' + identity[1:] + '/status/' + remote_id
        articles = page.locator('[data-testid="tweet"]')
        visible_matches = 0
        for article in articles.all():
            # A timestamp permalink belongs to this article; quoted links alone
            # do not establish which tweet is being inspected.
            links = article.locator('a[href]').evaluate_all(
                'ns=>ns.filter(a=>a.querySelector("time")).map(a=>a.getAttribute("href"))')
            if sum(isinstance(link, str) and link.lower() == href for link in links) != 1:
                continue
            if not article.is_visible():
                continue
            text = article.locator('[data-testid="tweetText"]')
            if text.count() != 1 or not text.inner_text().strip():
                raise PublishError('X 原作品正文未真实渲染')
            rendered = text.evaluate(RENDERED, values[0]['url_mappings'])
            if rendered != pack['fields']['composed_text']:
                raise PublishError('X 原作品可见完整文案或原生链接与封存内容不符')
            if pack.get('content_type', 'text') == 'video':
                players = article.locator('[data-testid="videoPlayer"]')
                players.first.wait_for(state='visible', timeout=15000)
                if (players.count() != 1 or not players.is_visible()
                        or article.locator('[data-testid="tweetPhoto"]:not(:has([data-testid="videoPlayer"]))').count()):
                    raise PublishError('X 原视频页没有唯一可见播放器或出现额外图片')
            elif pack.get('content_type') == 'images':
                photos = article.locator('[data-testid="tweetPhoto"]')
                if (photos.count() != len(expected_media)
                        or article.locator('video,[data-testid="videoPlayer"]').count()):
                    raise PublishError('X 原图文页图片数量不符或出现额外视频')
                actual = []
                for photo in photos.all():
                    if not photo.is_visible(): raise PublishError('X 原图文页存在不可见图片')
                    images = photo.locator('img').evaluate_all(
                        'ns=>ns.map(n=>({src:n.currentSrc,loaded:n.complete&&n.naturalWidth>0}))')
                    if len(images) != 1 or images[0].get('loaded') is not True:
                        raise PublishError('X 原图文页图片未完整加载或不能唯一核对')
                    actual.append(photo_source(images[0]['src']))
                if actual != [photo_source(u) for u in values[0]['photo_urls']]:
                    raise PublishError('X 原图文页可见图片来源或次序与原生详情不符')
            elif article.locator('[data-testid="tweetPhoto"],video,[data-testid="videoPlayer"]').count():
                raise PublishError('X 文字帖原生作品页出现未交接媒体')
            visible_matches += 1
        if visible_matches != 1:
            raise PublishError('X 作品页没有唯一可见的原作品正文')
        observation = dict(remote_url=url, observed_at=time.time(), native_detail_digest=digest(bodies),
                    content_verified=True, verified_pack_digest=digest(pack), author_id=values[0]['author_id'])
        if expected_media is not None:
            observation.update(verified_asset_ids=[a['id'] for a in pack['assets']],
                               media_tracking_scope='native_post_media_id_bound_to_processed_source',
                               remote_media_bytes_verified=False)
        return observation


RENDERED = r'''(root, mappings) => {
  const known=new Map();
  for(const m of mappings){
    for(const key of [m.url,m.expanded_url]){
      if(known.has(key)&&known.get(key)!==m.expanded_url)throw Error('ambiguous link');
      known.set(key,m.expanded_url);
    }
  }
  const visit=node=>{
    if(node.nodeType===Node.TEXT_NODE)return node.data;
    if(node.nodeType!==Node.ELEMENT_NODE)return '';
    if(node.tagName==='BR')return '\n';
    if(node.tagName==='IMG'){
      if(!node.alt)throw Error('missing emoji');
      return node.alt;
    }
    if(node.tagName==='A'){
      const href=node.getAttribute('href');
      if(known.has(href))return known.get(href);
      if(/^https?:/.test(href||'')&&new URL(href).hostname!=='x.com')throw Error('unmatched link');
    }
    return Array.from(node.childNodes).map(visit).join('');
  };
  return visit(root);
}'''
