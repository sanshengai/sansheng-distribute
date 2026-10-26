"""Compare platform rewrites without equating different images or articles."""
import html
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode
from publish_core import PublishError, read_json, file_hash


def image_identity(attrs):
    raw = attrs.get('src') or attrs.get('data-src') or ''
    u = urlsplit(raw)
    # The account API changes /0 to /640 and src to data-src. The opaque image
    # directory remains the identity; other hosts and paths stay byte-strict.
    if u.scheme == 'https' and u.hostname == 'mmbiz.qpic.cn' and not u.port and not u.username:
        if re.fullmatch(r'/(?:sz_)?mmbiz_[A-Za-z0-9]+/[A-Za-z0-9_-]+/(?:0|640|1080)', u.path):
            return 'https://mmbiz.qpic.cn' + u.path.rsplit('/', 1)[0]
    return raw


def article_identity(raw):
    u = urlsplit(html.unescape(raw))
    if u.scheme != 'https' or u.hostname != 'mp.weixin.qq.com' or u.port or u.username or u.path != '/s':
        return raw
    q = parse_qs(u.query)
    keys = ('__biz','mid','idx','sn')
    if any(len(q.get(k,[])) != 1 or not q[k][0] for k in keys):
        return raw
    return 'https://mp.weixin.qq.com/s?' + urlencode([(k,q[k][0]) for k in keys])


def load_link_identities(request_path, manifest_digest, remote_id):
    path = Path(request_path).with_suffix('.wechat-links.json')
    if not path.exists():
        return {}
    data = read_json(path)
    if data.get('manifest_digest') != manifest_digest or data.get('remote_id') != remote_id:
        raise PublishError('文章链接身份观察对应另一任务或草稿')
    result = {}
    for row in data.get('observations',[]):
        source = row.get('source_url','')
        if not re.fullmatch(r'https://mp\.weixin\.qq\.com/s/[A-Za-z0-9_-]+', source):
            raise PublishError('链接身份观察必须来自微信文章短链接')
        if row.get('response_url') != source or row.get('status') != 200:
            raise PublishError('链接观察未取得原官方文章，可能需要验证')
        proof = Path(row.get('path',''))
        if not proof.is_absolute() or not proof.is_file() or file_hash(proof) != row.get('sha256'):
            raise PublishError('链接身份原始证据缺失或漂移')
        text = proof.read_text(encoding='utf-8')
        values = {}
        for key in ('biz','mid','idx','sn','msg_link'):
            # Only literal top-level declarations, never expressions or guesses.
            matches = re.findall(r'\bvar\s+'+key+r'\s*=\s*[\"\x27]([^\"\x27]+)[\"\x27]', text)
            valid = [html.unescape(x) for x in matches if not any(c in x for c in '\n\r')]
            if not valid or len(set(valid)) != 1:
                raise PublishError('官方文章身份字段不唯一或缺失')
            values[key] = valid[0]
        if values['msg_link'] != source or not values['mid'].isdigit() or not values['idx'].isdigit() or not re.fullmatch(r'[a-f0-9]{32}',values['sn']):
            raise PublishError('官方文章身份与原链接不符')
        identity = article_identity('https://mp.weixin.qq.com/s?' + urlencode([('__biz',values['biz']),('mid',values['mid']),('idx',values['idx']),('sn',values['sn'])]))
        if source in result and result[source] != identity:
            raise PublishError('文章短链接观察互相冲突')
        result[source] = identity
    return result


def editor_visible_text(markup):
    """Ignore source indentation at block boundaries, preserve inline word spaces."""
    class Text(HTMLParser):
        blocks = {'address','article','aside','blockquote','br','caption','dd','div','dl','dt',
                  'figcaption','figure','footer','form','h1','h2','h3','h4','h5','h6','header',
                  'hr','li','main','nav','ol','p','pre','section','table','tbody','td','tfoot',
                  'th','thead','tr','ul'}
        def __init__(self):
            super().__init__(); self.parts=[]; self.hidden=0
        def handle_starttag(self,tag,attrs):
            if tag in {'script','style','noscript'}: self.hidden+=1
            elif not self.hidden and tag in self.blocks: self.parts.append(' ')
        def handle_endtag(self,tag):
            if tag in {'script','style','noscript'} and self.hidden: self.hidden-=1
            elif not self.hidden and tag in self.blocks: self.parts.append(' ')
        def handle_data(self,data):
            if not self.hidden: self.parts.append(data)
    parser=Text();parser.feed(markup);parser.close()
    return re.sub(r'\s+',' ',''.join(parser.parts)).strip()
