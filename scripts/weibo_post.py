"""Independent native Weibo detail readback; never call private APIs directly."""
from __future__ import annotations
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re
import time
from urllib.parse import parse_qs, urlsplit

from browser_runtime import background_window
from publish_core import PublishError, digest


def identity(result, expected=None):
    try:
        ident, uid = result['idstr'], result['user']['idstr']
        if (not isinstance(ident, str) or not re.fullmatch(r'[1-9][0-9]{0,24}', ident)
                or str(result['id']) != ident or str(result['mid']) != ident
                or not isinstance(uid, str) or not re.fullmatch(r'[1-9][0-9]{4,19}', uid)
                or str(result['user']['id']) != uid
                or not isinstance(result['mblogid'], str)
                or not re.fullmatch(r'[A-Za-z0-9]{5,24}', result['mblogid'])
                or (expected is not None and uid != expected)):
            raise ValueError()
        return dict(remote_id=ident, identity=uid, mblogid=result['mblogid'])
    except (KeyError, TypeError, ValueError) as exc:
        raise PublishError('微博原生作品、作者或短 ID 缺失或矛盾') from exc


def status_url(uid, remote_id):
    if (not isinstance(uid, str) or not re.fullmatch(r'[1-9][0-9]{4,19}', uid)
            or not isinstance(remote_id, str) or not re.fullmatch(r'[1-9][0-9]{0,24}', remote_id)):
        raise PublishError('微博作品或账号身份无效')
    return 'https://weibo.com/detail/' + remote_id


def url_mappings(body):
    values = body.get('url_struct', [])
    if not isinstance(values, list):
        raise PublishError('微博原生链接实体无效')
    result = {}
    for value in values:
        try:
            short, long = value['short_url'], value['long_url']
            u = urlsplit(long); s = urlsplit(short)
            if (not isinstance(short, str) or s.scheme not in {'http', 'https'} or s.netloc != 't.cn'
                    or not re.fullmatch(r'/[A-Za-z0-9]+', s.path) or s.query or s.fragment
                    or u.scheme not in {'http', 'https'} or not u.netloc or u.username or u.password
                    or value.get('ori_url', long) != long
                    or (short in result and result[short] != long)):
                raise ValueError()
            result[short] = long  # Native detail can repeat an identical entity.
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PublishError('微博原生短链或原地址不能唯一核对') from exc
    return result


def expand(text, mappings):
    if not isinstance(text, str):
        raise PublishError('微博完整原文缺失')
    return re.sub(r'https?://t\.cn/[A-Za-z0-9]+',
                  lambda m: mappings.get(m.group(0), m.group(0)), text)


class Text(HTMLParser):
    """Decode native BR/emoji/topic HTML and exact native link destinations."""
    def __init__(self, mappings):
        super().__init__(convert_charrefs=True)
        self.mappings = mappings; self.out = []; self.anchor = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if (tag not in {'br', 'a', 'span', 'div', 'img', 'strong', 'b', 'em', 'i', 'p'}
                or 'hidden' in attrs or attrs.get('aria-hidden') == 'true'
                or any(k.startswith('on') for k in attrs)
                or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style', ''), re.I)):
            raise PublishError('微博正文出现未覆盖的隐藏或动态内容')
        if tag == 'a':
            if self.anchor:
                raise PublishError('微博正文链接嵌套')
            href = attrs.get('href', ''); u = urlsplit(href)
            if u.scheme not in {'', 'http', 'https'} or u.username or u.password:
                raise PublishError('微博正文链接协议或身份不受支持')
            destination = None
            if href in self.mappings:
                destination = self.mappings[href]
            elif href in self.mappings.values():
                destination = href
            elif u.scheme == 'https' and u.netloc == 'weibo.cn' and u.path == '/sinaurl':
                params = parse_qs(u.query, keep_blank_values=True)
                if set(params) != {'u'} or len(params['u']) != 1 or params['u'][0] not in self.mappings.values():
                    raise PublishError('微博跳转链接未绑定原生链接实体')
                destination = params['u'][0]
            elif u.netloc not in {'weibo.com', 'www.weibo.com', 's.weibo.com'}:
                raise PublishError('微博正文外链未绑定原生链接实体')
            self.anchor = destination or True
            if destination:
                self.out.append(destination)
        elif self.anchor is not True and self.anchor:
            return  # Replaced native link label/icon, not authored text.
        elif tag == 'br':
            self.out.append('\n')
        elif tag == 'img':
            if not attrs.get('alt'):
                raise PublishError('微博正文表情缺少原生替代文字')
            self.out.append(attrs['alt'])

    def handle_endtag(self, tag):
        if tag == 'a': self.anchor = False

    def handle_data(self, value):
        if not self.anchor or self.anchor is True:
            self.out.append(value)


def rendered(value, mappings):
    if not isinstance(value, str) or not value:
        raise PublishError('微博正文 HTML 缺失')
    parser = Text(mappings); parser.feed(value); parser.close()
    if parser.anchor:
        raise PublishError('微博正文链接未闭合')
    return ''.join(parser.out)


def facts(body):
    if not isinstance(body, dict) or body.get('ok') != 1 or body.get('error'):
        raise PublishError('微博原生详情查询未成功')
    value = identity(body)
    if (body.get('retweeted_status') or body.get('deleted') or body.get('mix_media_info')
            or body.get('page_info') or body.get('is_paid') not in (None, False, 0)
            or not isinstance(body.get('visible'), dict)
            or type(body['visible'].get('type')) is not int or body['visible']['type'] != 0):
        raise PublishError('微博普通公开图文出现转发、其他媒体或不可核对的可见性')
    text = body.get('text_raw')
    if body.get('isLongText') and (not isinstance(body.get('longText'), dict)
                                 or body['longText'].get('content') != text):
        raise PublishError('微博长文缺少完整详情原文；列表截断文字不能验收')
    maps = url_mappings(body)
    text = expand(text, maps)
    if rendered(body.get('text'), maps) != text:
        raise PublishError('微博原生正文 HTML 与完整原文不一致')
    pics = body.get('pic_ids')
    if (not isinstance(pics, list) or any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9]{8,80}', p) for p in pics)
            or len(set(pics)) != len(pics) or type(body.get('pic_num')) is not int
            or body['pic_num'] != len(pics)):
        raise PublishError('微博图片身份、数量或次序无效')
    return dict(value, text=text, pic_ids=pics, url_mappings=maps)


def check_uploads(pack, uploaded):
    if (pack['content_type'] not in {'text', 'images'} or not isinstance(uploaded, list)
            or [(u.get('id'), u.get('sha256')) for u in uploaded] != [(a['id'], a['sha256']) for a in pack['assets']]
            or any(not isinstance(u.get('pid'), str) or not re.fullmatch(r'[A-Za-z0-9]{8,80}', u['pid']) for u in uploaded)
            or len({u['pid'] for u in uploaded}) != len(uploaded)):
        raise PublishError('微博图片原生 PID 未与封存原件一一绑定')


def verify_facts(value, pack, uid, accepted, uploaded):
    check_uploads(pack, uploaded)
    if any(value.get(k) != accepted.get(k) for k in ('remote_id', 'identity', 'mblogid')):
        raise PublishError('微博详情作品身份与首次受理回执不同')
    if value['identity'] != uid or value['text'] != pack['fields']['composed_text']:
        raise PublishError('微博详情作者、完整文案或链接与封存内容不符')
    if value['pic_ids'] != [u['pid'] for u in uploaded]:
        raise PublishError('微博详情图片身份或次序与原生上传回执不符')
    return value


def readback(anchor, pack, uid, accepted, uploaded, cdp_url):
    """A fresh native detail window, exact public identity, full body and PIDs."""
    url = status_url(uid, accepted.get('remote_id')); bodies = []; errors = []
    with background_window(anchor, cdp_url=cdp_url) as page:
        def response(r):
            u = urlsplit(r.url)
            if u.scheme != 'https' or u.netloc != 'weibo.com' or u.path != '/ajax/statuses/show': return
            try:
                if r.status != 200 or r.request.method != 'GET' or len(bodies) >= 5: raise ValueError()
                bodies.append(r.json())
            except Exception: errors.append('native_detail_unavailable')
        page.on('response', response)
        page.goto(url, wait_until='domcontentloaded', timeout=25000)
        page.locator('article .wbpro-feed-ogText').wait_for(state='visible', timeout=20000)
        if page.url != url or errors or not bodies:
            raise PublishError('微博独立作品查询未就绪；保留原受理身份')
        values = [verify_facts(facts(b), pack, uid, accepted, uploaded) for b in bodies]
        if len({digest(v) for v in values}) != 1:
            raise PublishError('微博独立作品查询结果矛盾')
        observe_visible(page, values[0], pack['fields']['composed_text'])
        return dict(remote_url=url, observed_at=time.time(), native_detail_digest=digest(bodies),
                    content_verified=True, verified_pack_digest=digest(pack),
                    verified_asset_ids=[a['id'] for a in pack['assets']],
                    media_tracking_scope='native_post_pic_ids_bound_to_uploaded_source',
                    remote_media_bytes_verified=False)


def observe_visible(page, value, expected_text):
    """Visible native detail only; does not claim a source file was uploaded."""
    uid = value['identity']
    matches = 0
    permalink = f'https://weibo.com/{uid}/{value["mblogid"]}'
    for article in page.locator('article').all():
        if article.locator('header a[href="'+permalink+'"]').count() != 1 or not article.is_visible(): continue
        node = article.locator('.wbpro-feed-ogText > div')
        if node.count() != 1 or not node.is_visible() or rendered(node.inner_html(), value['url_mappings']) != expected_text:
            raise PublishError('微博作品页的可见完整正文与封存内容不符')
        if not node.evaluate('''root=>[root,...root.querySelectorAll('*')].every(n=>{const s=getComputedStyle(n);return s.display!=='none'&&s.visibility!=='hidden'&&s.visibility!=='collapse'&&Number(s.opacity)>0;})'''):
            raise PublishError('微博作品正文存在不可见内容；不认定完整可见')
        if article.locator('video').count():
            raise PublishError('微博图文出现未交接的视频')
        pictures = article.locator('.picture img').evaluate_all('''ns=>ns.map(n=>({src:n.getAttribute('src'),loaded:n.complete&&n.naturalWidth>0}))''')
        pids = []
        for image in pictures:
            u = urlsplit(image['src'] or '')
            if u.scheme != 'https' or not re.fullmatch(r'(?:[A-Za-z0-9-]+\.)*sinaimg\.cn', u.netloc) or image['loaded'] is not True:
                raise PublishError('微博作品图片未完整加载或来源不符')
            pids.append(PurePosixPath(u.path).stem)
        if pids != value['pic_ids']:
            raise PublishError('微博作品可见图片身份或次序与原生详情不符')
        matches += 1
    if matches != 1:
        raise PublishError('微博作品页缺少唯一可见原作品')
