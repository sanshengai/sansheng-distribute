"""Reconcile observed native components with producer slots; never invent player markup."""
import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs
from publish_core import PublishError, marker


# These are recognition candidates, not claims that any tag/ID can be constructed.
# A new rendering requires a real captured sample and an added parser fixture.
NATIVE={'mpvoice','mpaudio','mp-common-mpaudio','mpvideo','mp-common-videosnap'}
EMBED=NATIVE|{'iframe','audio','video','embed','object'}
VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
IDENTITY={'voice_encode_fileid','data-voice_encode_fileid','voice_id','media_id','data-media_id','vid','data-vid','data-mpvid'}


def official_video_iframe(attrs):
    """Recognize the 2026-10-24 official draft/get sample, not arbitrary iframes."""
    attrs=dict(attrs)
    url=urlsplit(attrs.get('data-src',''))
    query=parse_qs(url.query)
    vid=attrs.get('data-mpvid','')
    return (url.scheme=='https' and url.netloc=='mp.weixin.qq.com'
            and url.path=='/mp/readtemplate' and not url.fragment
            and query.get('t')==['pages/video_player_tmpl']
            and query.get('action')==['mpvideo'] and query.get('vid')==[vid]
            and bool(re.fullmatch(r'wxv_\d+',vid)) and attrs.get('data-vidtype')=='2'
            and 'video_iframe' in attrs.get('class','').split() and not attrs.get('src'))


class Components(HTMLParser):
    def __init__(self,content):
        super().__init__(convert_charrefs=False)
        self.content=content; self.offsets=[0]; self.spans=[]; self.active=None; self.depth=0
        for line in content.splitlines(keepends=True): self.offsets.append(self.offsets[-1]+len(line))
        self.feed(content); self.close()
        if self.active: raise PublishError('原生播放器结构未闭合')

    def source_offset(self):
        line,col=self.getpos(); return self.offsets[line-1]+col

    def handle_starttag(self,tag,attrs):
        known_video=tag=='iframe' and official_video_iframe(attrs)
        if tag in EMBED and tag not in NATIVE and not known_video:
            raise PublishError(f'未验收的媒体组件：{tag}；需要实际样本适配')
        if self.active:
            if tag in NATIVE or known_video: raise PublishError('嵌套原生播放器不受支持')
            if tag not in VOID: self.depth+=1
        elif tag in NATIVE or known_video:
            self.active=(self.source_offset(),tag); self.depth=1

    def handle_endtag(self,tag):
        if not self.active: return
        if tag not in VOID: self.depth-=1
        if self.depth==0:
            if tag!=self.active[1]: raise PublishError('原生播放器闭合标签不匹配')
            end=self.content.find('>',self.source_offset())+1
            self.spans.append((self.active[0],end,self.active[1])); self.active=None

    def handle_startendtag(self,tag,attrs):
        known_video=tag=='iframe' and official_video_iframe(attrs)
        if tag in EMBED and tag not in NATIVE and not known_video: raise PublishError('未验收的媒体组件')
        if tag in NATIVE or known_video:
            if self.active: raise PublishError('嵌套原生播放器不受支持')
            self.spans.append((self.source_offset(),self.source_offset()+len(self.get_starttag_text()),tag))


def component_info(content):
    class Attrs(HTMLParser):
        def handle_starttag(self,tag,attrs):
            if not hasattr(self,'attrs'): self.attrs=dict(attrs)
    result=[]
    for start,end,tag in Components(content).spans:
        component=content[start:end]; parsed=Attrs(); parsed.feed(component)
        ids={k:v for k,v in getattr(parsed,'attrs',{}).items() if k in IDENTITY and v}
        result.append(dict(tag=tag,component_sha256=hashlib.sha256(component.encode()).hexdigest(),identities=ids))
    return result


def reconcile(content,slots,assets,receipt,*,manifest_digest,remote_id):
    if receipt.get('manifest_digest')!=manifest_digest or receipt.get('remote_id')!=remote_id:
        raise PublishError('音视频回执与文章版本/草稿不符')
    rows=receipt.get('components',[])
    if not isinstance(rows,list) or len(rows)>len(slots):
        raise PublishError('原生媒体回执不完整')
    by_slot={row.get('slot_id'):row for row in rows}
    if len(by_slot)!=len(rows) or not set(by_slot).issubset({s['id'] for s in slots}):
        raise PublishError('媒体槽重复或错配')
    replacements={}
    infos={c['component_sha256']:c for c in component_info(content)}
    remaining=[]
    for slot in slots:
        if slot['id'] not in by_slot:
            remaining.append(dict(slot_id=slot['id'],action='insert'))
            continue
        row=by_slot[slot['id']]; asset=assets[slot['asset_id']]
        if row.get('asset_sha256')!=asset['sha256'] or row.get('kind')!=asset['kind']:
            raise PublishError('播放器未绑定正确的源素材')
        if not row.get('remote_media_id') or not row.get('upload_evidence'):
            raise PublishError('缺少实际上传/选择素材和播放器身份的关联证据')
        if any(row.get(k) is not True for k in ('saved','reopened','loaded','played')):
            remaining.append(dict(slot_id=slot['id'],action='verify_existing_player'))
        fingerprint=row.get('component_sha256')
        if not fingerprint or fingerprint in replacements:
            raise PublishError('播放器指纹缺失或重复；相同组件需单独适配，不猜对应位置')
        if fingerprint not in infos:
            raise PublishError('播放器指纹与当前读回不符；先核对媒体身份、源素材和全文，再刷新观察回执')
        info=infos[fingerprint]
        kind='audio' if info.get('tag') in {'mpvoice','mpaudio','mp-common-mpaudio'} else 'video'
        if kind!=asset['kind']:
            raise PublishError('实际播放器类型与源素材不符')
        attribute=row.get('remote_id_attribute')
        if (kind=='audio' and attribute in {'vid','data-vid','data-mpvid'}) or (kind=='video' and attribute in {'voice_encode_fileid','data-voice_encode_fileid','voice_id'}):
            raise PublishError('播放器身份属性与媒体类型不符')
        if info.get('identities',{}).get(row.get('remote_id_attribute'))!=row['remote_media_id']:
            raise PublishError('播放器身份与官方读回属性不符')
        replacements[fingerprint]=marker(slot['id'])
    spans=Components(content).spans
    if len(spans)!=len(rows): raise PublishError('正文播放器数量与已记录媒体槽不符；先只读补齐实际映射')
    parts=[]; cursor=0; seen=set()
    for start,end,tag in spans:
        fingerprint=hashlib.sha256(content[start:end].encode()).hexdigest()
        if fingerprint not in replacements or fingerprint in seen:
            raise PublishError('正文播放器与实际观察回执不符')
        seen.add(fingerprint)
        parts.extend((content[cursor:start],replacements[fingerprint])); cursor=end
    parts.append(content[cursor:])
    return ''.join(parts),remaining
