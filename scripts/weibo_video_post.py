"""Native public video/detail and author edit facts; no private API requests.

Parsing these observations is a readback foundation, not publication evidence.
The sender must separately bind native upload receipts and immutable sources.
"""
from __future__ import annotations
import copy
import math
from pathlib import PurePosixPath
import re
import time
from urllib.parse import parse_qs, urlsplit

from browser_runtime import background_window
from publish_core import PublishError, digest
import weibo_post as post

CREATION_TYPES = {'原创': 0, '二创': 2, '转载': 1}


def video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,24}', value):
        raise PublishError('微博视频缺少精确字符串媒体 ID')
    return value


def image_pid(url):
    if not isinstance(url, str):raise PublishError('微博视频封面地址缺失')
    u=urlsplit(url);pid=PurePosixPath(u.path).stem
    if (u.scheme!='https' or not re.fullmatch(r'(?:[A-Za-z0-9-]+\.)*sinaimg\.cn',u.netloc)
            or u.query or u.fragment or not re.fullmatch(r'[A-Za-z0-9]{8,128}',pid)):
        raise PublishError('微博视频封面来源或 PID 无法核对')
    return pid


def mappings(body, vid, rid):
    """Accept only the native attachment's exact app/HTTP identity mapping."""
    values=body.get('url_struct',[])
    if not isinstance(values,list):raise PublishError('微博视频链接实体无效')
    short=body['page_info'].get('short_url')
    if not isinstance(short,str) or not re.fullmatch(r'https?://t\.cn/[A-Za-z0-9]+',short):
        raise PublishError('微博视频原生播放短链缺失')
    ordinary=[];attachment=[]
    for v in values:
        if not isinstance(v,dict):raise PublishError('微博视频链接实体无效')
        if v.get('short_url')!=short:
            ordinary.append(v);continue
        try:
            long=v['long_url'];u=urlsplit(long);q=parse_qs(u.query,keep_blank_values=True)
            original=urlsplit(v['ori_url']);oq=parse_qs(original.query,keep_blank_values=True)
            if (u.scheme!='https' or u.netloc!='video.weibo.com' or u.path!='/show'
                    or u.fragment or q!={'fid':['1034:'+vid]}
                    or original.scheme!='sinaweibo' or original.netloc!='video' or original.path!='/vvs'
                    or original.fragment or oq!={'mid':[rid],'object_id':['1034:'+vid],
                                              'url_type':['39'],'object_type':['video'],'pos':['1']}
                    or type(v.get('url_type')) is not int or v['url_type']!=39):raise ValueError()
            attachment.append((long,v['ori_url']))
        except (ValueError,TypeError,KeyError,AttributeError) as exc:
            raise PublishError('微博播放短链与原生作品或视频身份不符') from exc
    if not attachment or len(set(attachment))!=1:
        raise PublishError('微博视频播放链接缺失或存在矛盾')
    result=post.url_mappings({'url_struct':ordinary})
    if short in result:raise PublishError('微博视频播放短链与普通外链冲突')
    result[short]=attachment[0][0]
    return result,attachment[0][0]


def facts(body):
    """Native /ajax/statuses/show body; exact complete text and public video."""
    if not isinstance(body,dict) or body.get('ok')!=1 or body.get('error'):
        raise PublishError('微博原生视频详情查询未成功')
    who=post.identity(body);uid=who['identity'];rid=who['remote_id']
    if (body.get('retweeted_status') or body.get('deleted') or body.get('mix_media_info')
            or body.get('is_paid') not in (None,False,0) or type(body.get('pic_num')) is not int
            or body['pic_num']!=0 or body.get('pic_ids') not in (None,[])
            or not isinstance(body.get('visible'),dict)
            or type(body['visible'].get('type')) is not int or body['visible']['type']!=0):
        raise PublishError('微博单视频出现其他媒体、转发或不能核对的公开设置')
    info=body.get('page_info');media=info.get('media_info') if isinstance(info,dict) else None
    if not isinstance(media,dict):raise PublishError('微博公开作品缺少原生视频详情')
    vid=video_id(media.get('media_id'));author=media.get('author_info')
    if (info.get('type')!='11' or info.get('object_type')!='video'
            or info.get('object_id')!='1034:'+vid or info.get('page_id')!='230444'+vid
            or info.get('author_id')!=uid or info.get('authorid')!=uid
            or media.get('author_mid')!=rid or media.get('media_type')!='video'
            or not isinstance(author,dict) or author.get('idstr')!=uid or str(author.get('id'))!=uid):
        raise PublishError('微博公开作品、作者与视频身份互相矛盾')
    title=media.get('video_title');duration=media.get('duration')
    if (not isinstance(title,str) or not title.strip() or type(duration) not in (int,float)
            or not math.isfinite(duration) or duration<=0):
        raise PublishError('微博视频标题或时长缺失')
    cover=image_pid(info.get('page_pic'))
    raw=body.get('text_raw')
    if (not isinstance(raw,str) or not raw or (body.get('isLongText') and
        (not isinstance(body.get('longText'),dict) or body['longText'].get('content')!=raw))):
        raise PublishError('微博视频缺少完整原文，不能以列表截断文案验收')
    urls,transport=mappings(body,vid,rid);text=post.expand(raw,urls)
    if post.rendered(body.get('text'),urls)!=text:
        raise PublishError('微博视频完整原文与正文 HTML 不一致')
    # Never trim authored whitespace or an arbitrary URL. Only one exact native
    # media entity at the final position can be treated as a transport suffix.
    suffix=' '+transport
    authored=None
    # Native video creator appends exactly one space and three zero-width
    # markers after its transport link. Do not trim authored whitespace.
    for ending in (suffix,suffix+' '+'\u200b'*3):
        if text.endswith(ending):authored=text[:-len(ending)];break
    topics=[];native_topics=body.get('topic_struct',[])
    if not isinstance(native_topics,list):raise PublishError('微博原生话题列表格式无效')
    for topic in native_topics:
        topic_title=topic.get('topic_title') if isinstance(topic,dict) else None
        u=urlsplit(topic.get('topic_url','')) if isinstance(topic,dict) else None
        if (not isinstance(topic_title,str) or not topic_title or u.scheme!='sinaweibo' or u.netloc!='searchall'
                or parse_qs(u.query).get('q')!=['#'+topic_title+'#'] or topic_title in topics):
            raise PublishError('微博原生话题身份或顺序不能核对')
        topics.append(topic_title)
    declarations=body.get('content_auth_list')
    statement=None
    if declarations is not None:
        if not isinstance(declarations,list):raise PublishError('微博视频公开内容声明格式无效')
        values=[v['mblog_statement'] for v in declarations if isinstance(v,dict) and 'mblog_statement' in v]
        if values:
            if any(type(v) is not int or v not in {0,1,2,3,4} for v in values) or len(set(values))!=1:
                raise PublishError('微博视频公开内容声明缺失或矛盾')
            statement=values[0]
    return dict(who,media_id=vid,title=title,duration=duration,cover_pid=cover,
                text=text,authored_text=authored,url_mappings=urls,statement=statement,topics=topics)


def authored_matches(public,fields):
    expected=fields['composed_text']
    if expected in (public['text'],public['authored_text']):return True
    names=[tag.strip('#') for tag in fields.get('tags',[])]
    separated=' '.join('#'+name+'#' for name in names)
    # Only the generated final topic group may lose its separators. Native
    # topic entities must establish every name and its original order.
    return bool(names and public.get('topics')==names and expected.endswith(separated)
                and public['authored_text']==expected[:-len(separated)]+separated.replace('# #','##'))


def public_duration_matches(fractional,seconds):
    # Public/native edit responses expose different precision. Exact source
    # milliseconds are checked independently; only observed integer encodings
    # (truncation or nearest second) are accepted here.
    return (type(seconds) is int and seconds in {math.floor(fractional),math.floor(fractional+0.5)})


def edit_duration_matches(source,edit):
    milliseconds=source*1000;expected=math.floor(milliseconds);actual=round(edit*1000)
    if abs(edit*1000-actual)>1e-7:return False
    # At an exact millisecond boundary the native edit service can truncate
    # one floating-point ULP below it (104.7 -> 104.699, observed live).
    allowed={expected,expected-1} if milliseconds.is_integer() else {expected}
    return actual in allowed


def edit_facts(body, public):
    """Native readonly getVideoEditInfo; binds media and original public post."""
    try:
        if not isinstance(body,dict) or body.get('ok')!=1 or body.get('error'):raise ValueError()
        v=body['data']['video_info']
        if (v['oid']!='1034:'+public['media_id'] or v['mid_str']!=public['remote_id']
                or str(v['mid'])!=public['remote_id'] or v['video_type']!='normal' or v['biz_type']!='video'
                or type(v['video_visibility']) is not int or v['video_visibility']!=0
                or v.get('publishing') is not False or v.get('editable') not in (1,True)):
            raise ValueError()
        titles=v['titles'];covers=v['covers']
        if (not isinstance(titles,list) or len(titles)!=1 or titles[0].get('default') is not True
                or titles[0].get('title')!=public['title'] or not isinstance(covers,list) or len(covers)!=1
                or covers[0].get('type')!='default' or image_pid(covers[0].get('url'))!=public['cover_pid']):
            raise ValueError()
        creation=[v[k] for k in ('homemade_info','contribution_info') if v.get(k)]
        if (len(creation)!=1 or type(creation[0].get('type')) is not int
                or creation[0]['type'] not in CREATION_TYPES.values()
                or any(type(v.get(k)) is not int or v[k]<=0 for k in ('width','height'))
                or type(v.get('duration')) not in (int,float) or not math.isfinite(v['duration'])
                or not public_duration_matches(v['duration'],public['duration']) or not isinstance(v.get('current_playlists'),list)):
            raise ValueError()
        return dict(remote_id=public['remote_id'],media_id=public['media_id'],title=public['title'],
                    cover_pid=public['cover_pid'],creation_type=next(k for k,n in CREATION_TYPES.items() if n==creation[0]['type']),
                    width=v['width'],height=v['height'],duration=v['duration'],
                    playlists=copy.deepcopy(v['current_playlists']))
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        raise PublishError('微博视频编辑读回与公开作品身份、标题、封面或元数据不符') from exc


def observe_visible(page, value, native_body):
    """Verify an existing native player's loaded URL; no play or app callbacks."""
    permalink=f'https://weibo.com/{value["identity"]}/{value["mblogid"]}'
    matches=[]
    media=native_body['page_info']['media_info']
    sources={media[k] for k in ('stream_url','stream_url_hd','mp4_sd_url','mp4_hd_url',
             'h265_mp4_hd','h265_mp4_ld','inch_4_mp4_hd','inch_5_mp4_hd',
             'inch_5_5_mp4_hd','mp4_720p_mp4','hevc_mp4_720p')
             if isinstance(media.get(k),str) and media[k]}
    playback=media.get('playback_list',[])
    if not isinstance(playback,list):raise PublishError('微博原生视频播放列表无效')
    for item in playback:
        if not isinstance(item,dict) or not isinstance(item.get('play_info'),dict):
            raise PublishError('微博原生视频播放条目无效')
        source=item['play_info'].get('url')
        if not isinstance(source,str) or not source:raise PublishError('微博原生视频播放条目缺少地址')
        sources.add(source)
    for a in page.locator('article').all():
        if not a.is_visible() or a.locator('header a[href="'+permalink+'"]').count()!=1:continue
        text=a.locator('.wbpro-feed-ogText > div')
        if (text.count()!=1 or not text.is_visible()
                or post.rendered(text.inner_html(),value['url_mappings'])!=value['text']):
            raise PublishError('微博视频公开页面的完整文案与原生详情不符')
        if not text.evaluate('''root=>[root,...root.querySelectorAll('*')].every(n=>{const s=getComputedStyle(n);return s.display!=='none'&&s.visibility!=='hidden'&&s.visibility!=='collapse'&&Number(s.opacity)>0;})'''):
            raise PublishError('微博视频正文含不可见内容')
        players=a.locator('video').evaluate_all('''ns=>ns.map(n=>{const r=n.getBoundingClientRect(),s=getComputedStyle(n);return {src:n.currentSrc,ready:n.readyState,width:n.videoWidth,height:n.videoHeight,visible:r.width>0&&r.height>0&&s.visibility==='visible'&&Number(s.opacity)>0};})''')
        if (len(players)!=1 or players[0].get('visible') is not True
                or players[0].get('src') not in sources or type(players[0].get('ready')) is not int
                or players[0]['ready']<1 or any(type(players[0].get(k)) is not int or players[0][k]<=0 for k in ('width','height'))):
            raise PublishError('微博公开视频播放器未加载同一原生媒体；不冒称播放或字节验收')
        u=urlsplit(players[0]['src'])
        if (u.scheme!='https' or not re.fullmatch(r'(?:[A-Za-z0-9-]+\.)*weibocdn\.com',u.netloc)
                or u.username or u.password or u.fragment):
            raise PublishError('微博可见视频源不是原生 HTTPS 媒体来源')
        matches.append(dict(width=players[0]['width'],height=players[0]['height'],
                            native_player_source_digest=digest(players[0]['src'])))
    if len(matches)!=1:raise PublishError('微博视频公开页面缺少唯一可见的原作品')
    return matches[0]


def observe_existing(anchor, uid, remote_id, cdp_url):
    """Fresh native public detail plus author edit GETs; never change fields.

    Return observations only, never content_verified/published/source claims.
    """
    url=post.status_url(uid,remote_id);raw=[];errors=[]
    with background_window(anchor,cdp_url=cdp_url) as page:
        def response(r):
            u=urlsplit(r.url)
            if u.scheme!='https' or u.netloc!='weibo.com' or u.path!='/ajax/statuses/show':return
            try:
                if r.status!=200 or r.request.method!='GET' or len(raw)>=5:raise ValueError()
                raw.append(r.json())
            except Exception:errors.append('native_video_detail_unavailable')
        page.on('response',response)
        page.goto(url,wait_until='domcontentloaded',timeout=25000)
        page.locator('article .wbpro-feed-ogText').wait_for(state='visible',timeout=20000)
        deadline=time.monotonic()+10
        while not raw and not errors and time.monotonic()<deadline:page.wait_for_timeout(100)
        if errors or not raw:raise PublishError('微博原视频独立详情未就绪')
        values=[facts(b) for b in raw]
        if (len({digest(v) for v in values})!=1
                or values[0]['remote_id']!=remote_id or values[0]['identity']!=uid):
            raise PublishError('微博视频独立详情的作品或作者不一致')
        public=values[0]
        canonical=f'https://weibo.com/{uid}/{public["mblogid"]}'
        if page.url not in {url,canonical}:
            raise PublishError('微博视频页面跳转未绑定原作品的数字及短 ID')
        deadline=time.monotonic()+10
        while True:
            try:
                visible=observe_visible(page,public,raw[0]);break
            except PublishError:
                if time.monotonic()>=deadline:raise
                page.wait_for_timeout(100)
    edit_url='https://weibo.com/upload/channel?entry=edit&oid=1034:'+public['media_id']
    edits=[];errors=[]
    with background_window(anchor,cdp_url=cdp_url) as page:
        def response(r):
            u=urlsplit(r.url)
            if u.scheme!='https' or u.netloc!='weibo.com' or u.path!='/ajax/multimedia/getVideoEditInfo':return
            try:
                q=parse_qs(u.query,keep_blank_values=True)
                if (r.status!=200 or r.request.method!='GET' or len(edits)>=5
                        or q.get('media_id')!=['1034:'+public['media_id']]):raise ValueError()
                edits.append(r.json())
            except Exception:errors.append('native_video_edit_unavailable')
        page.on('response',response)
        page.goto(edit_url,wait_until='domcontentloaded',timeout=25000)
        page.locator('input[placeholder="填写标题（0～30个字）"]').wait_for(state='visible',timeout=20000)
        deadline=time.monotonic()+10
        while not edits and not errors and time.monotonic()<deadline:page.wait_for_timeout(100)
        if page.url!=edit_url or errors or not edits:raise PublishError('微博原视频编辑读回未就绪')
        values=[edit_facts(b,public) for b in edits]
        if len({digest(v) for v in values})!=1:raise PublishError('微博视频原生编辑详情矛盾')
        title=page.locator('input[placeholder="填写标题（0～30个字）"]')
        if title.count()!=1 or not title.is_visible() or title.input_value()!=public['title']:
            raise PublishError('微博视频可见编辑标题与公开作品不同')
    return dict(public=public,edit=values[0],visible=visible,observed_at=time.time(),
                native_detail_digest=digest(raw),native_edit_digest=digest(edits),
                source_binding_verified=False,remote_media_bytes_verified=False)


def bind_sources(pack, uid, accepted, saved, observation):
    """Bind post/readback identity to prior upload proofs, never mark published.

    A caller still needs actual native submission-input and declaration checks.
    Do not reconstruct a missing upload proof from the public video or cover.
    """
    import weibo_video_upload as upload
    import weibo_video_cover as cover
    try:
        if (pack['platform']!='weibo' or pack['content_type']!='video'
                or saved.get('pack_digest')!=digest(pack) or saved.get('identity')!=uid
                or not isinstance(accepted,dict) or accepted.get('identity')!=uid):raise ValueError()
        post.status_url(uid,accepted.get('remote_id'))
        public,edit=observation['public'],observation['edit']
        if (public['identity']!=uid or public['remote_id']!=accepted['remote_id']
                or public['mblogid']!=accepted.get('mblogid')
                or edit['remote_id']!=accepted['remote_id'] or edit['media_id']!=public['media_id']
                or public['title']!=pack['fields']['title'] or edit['title']!=public['title']
                or not authored_matches(public,pack['fields'])
                or edit['creation_type']!=pack['platform_metadata']['creation_type']):raise ValueError()
        assets=pack['assets'];videos=[a for a in assets if a['kind']=='video'];images=[a for a in assets if a['kind']=='image']
        if len(videos)!=1 or len(videos)+len(images)!=len(assets):raise ValueError()
        native=upload.saved_source(videos[0],saved)
        if (native['media_id']!=public['media_id']
                or any(edit[k]!=native[k] for k in ('width','height'))
                or not edit_duration_matches(float(native['duration']),edit['duration'])
                or not public_duration_matches(native['duration'],public['duration'])):raise ValueError()
        bindings=[dict(asset_id=videos[0]['id'],source_sha256=videos[0]['sha256'],native_media_id=native['media_id'])]
        if pack['cover_asset_id']:
            if len(images)!=1 or images[0]['id']!=pack['cover_asset_id']:raise ValueError()
            saved_cover=saved.get('cover');prepared=saved_cover.get('prepared') if isinstance(saved_cover,dict) else None
            if not isinstance(prepared,dict):raise ValueError()
            proof=cover.receipt(images[0],prepared,saved_cover)
            cover.check_video_ratio(prepared,native)
            if public['cover_pid']!=proof['pid'] or edit['cover_pid']!=proof['pid']:raise ValueError()
            bindings.append(dict(asset_id=images[0]['id'],source_sha256=images[0]['sha256'],native_picture_id=proof['pid']))
        elif images:
            raise ValueError()
        return dict(source_binding_verified=True,remote_media_bytes_verified=False,asset_bindings=bindings,
                    verified_pack_digest=digest(pack),remote_id=public['remote_id'],native_media_id=native['media_id'])
    except (KeyError,ValueError,TypeError,AttributeError) as exc:
        raise PublishError('微博视频独立作品与原封存文案、视频或封面来源不符') from exc
