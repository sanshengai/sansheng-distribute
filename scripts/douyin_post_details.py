"""Native published-video detail observation, without editing or submitting.

Native VID and cover URIs are observations, not proof of source-file bindings.
Only the personally logged-in AI-declared video shape observed live is covered.
"""
from __future__ import annotations
import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

import douyin_browser as editor
from browser_runtime import click_background
from publish_core import PublishError, digest

DETAIL_PATH = '/web/api/media/item/info/'
TRANSCODE_PATH = '/web/api/media/video/transend/'


def integer(value, minimum=1, maximum=86400000):
    return type(value) is int and minimum <= value <= maximum


def image_uri(value):
    if not isinstance(value, str) or not re.fullmatch(r'tos-cn-i-dy/[A-Za-z0-9_-]+', value):
        raise PublishError('抖音详情封面标识缺失或未覆盖')
    return value


def utf16_slice(text, start, end):
    if not isinstance(text, str) or not integer(start,0) or not integer(end,start+1):
        raise PublishError('抖音原生话题范围无效')
    try:
        data=text.encode('utf-16-le')
        if end*2>len(data):raise ValueError()
        return data[start*2:end*2].decode('utf-16-le')
    except (UnicodeError,ValueError) as exc:
        raise PublishError('抖音原生话题范围越界或切开 Unicode 字符') from exc


def topics(aweme):
    values=aweme.get('text_extra');names=[];previous={'caption':0,'desc':0}
    if not isinstance(values,list):raise PublishError('抖音详情原生话题实体缺失')
    for value in values:
        try:
            name=value['hashtag_name']
            if (type(value['type']) is not int or value['type']!=1 or not isinstance(name,str)
                    or not name or re.search(r'[#\s\u200b]',name) or name in names):raise ValueError()
            for field,begin,end in [('caption','caption_start','caption_end'),('desc','start','end')]:
                start,stop=value[begin],value[end]
                if (not integer(start,0) or start<previous[field]
                        or utf16_slice(aweme[field],start,stop)!='#'+name):raise ValueError()
                previous[field]=stop
            names.append(name)
        except (KeyError,TypeError,ValueError) as exc:
            raise PublishError('抖音详情话题名称、次序或原文范围不符') from exc
    return names


def facts(body, listed, public_identity):
    try:
        if (not isinstance(body,dict) or type(body.get('status_code')) is not int
                or body['status_code']!=0):raise ValueError()
        a=body['aweme'];v=a['video'];status=a['status'];author=a['author']
        # The native payload can contain rounded numeric item_id/group_id.
        # Only string aweme_id is authoritative; never coerce those numbers.
        if (a['aweme_id']!=listed['remote_id'] or not isinstance(a['aweme_id'],str)
                or author['uid']!=listed['native_user_id']
                or type(a['author_user_id']) is not int or str(a['author_user_id'])!=listed['native_user_id']
                or public_identity not in [author['short_id'],author['unique_id']]
                or a['desc']!=listed['description'] or not isinstance(a['item_title'],str)
                or not a['item_title'] or not isinstance(a['caption'],str)
                or type(a['aweme_type']) is not int or a['aweme_type']!=4
                or a['images'] is not None or a['image_infos'] is not None
                or a['is_live_replay'] is not False or a['is_pic_word'] is not False
                or a['forward_id']!='0' or status['aweme_id']!=listed['remote_id']
                or any(status[k] is not False for k in ['in_reviewing','is_delete','is_private','is_prohibited','self_see'])
                or type(status['private_status']) is not int or status['private_status']!=0
                or not integer(a['duration']) or a['duration']!=listed['duration_ms']
                or not integer(v['duration']) or v['duration']!=a['duration']
                or not isinstance(v['vid'],str) or not re.fullmatch(r'v[A-Za-z0-9_-]{15,79}',v['vid'])
                or v['play_addr']['uri']!=v['vid'] or v['download_addr']['uri']!=v['vid']
                or not integer(v['width'],1,16384) or not integer(v['height'],1,16384)
                or image_uri(a['Cover']['uri'])!=listed['cover_uri']
                or image_uri(v['cover']['uri'])!=a['Cover']['uri']):raise ValueError()
        horizontal=image_uri(a['horizontal_cover']['uri'])
        from douyin_schedule import native_timer
        timer = native_timer(a)
        if timer is not None and (timer['state'] != 'online'
                                  or timer['public_at_epoch'] > time.time()):
            raise PublishError('抖音作品仍在排期、排期受限或时间矛盾；不能认定已经发布')
        # Other choices need their own observed native mapping, not guessed labels.
        declaration=json.loads(a['user_declare_info'],object_pairs_hook=lambda pairs:pairs)
        if declaration!=[('choose_value','aigc')]:raise ValueError()
        mix=a['mix_info']
        collection=None
        if mix is not None:
            from douyin_post import decimal
            if (not decimal(mix['mix_id']) or not isinstance(mix['mix_name'],str)
                    or not mix['mix_name'] or mix.get('next_info',{}).get('mix_name')!=mix['mix_name']):raise ValueError()
            collection=dict(id=mix['mix_id'],name=mix['mix_name'])
        names=topics(a)
    except (KeyError,TypeError,ValueError,AttributeError) as exc:
        raise PublishError('抖音详情身份、原文、媒体、声明或合集字段不符/未覆盖') from exc
    return dict(remote_id=a['aweme_id'],native_user_id=author['uid'],title=a['item_title'],
                caption=a['caption'],description=a['desc'],topics=names,
                video=dict(vid=v['vid'],width=v['width'],height=v['height'],duration_ms=v['duration']),
                covers=dict(vertical=a['Cover']['uri'],horizontal=horizontal),
                declaration='内容由AI生成',collection=collection,timer=timer)


def native_image_url(value):
    u=urlsplit(value)
    if (u.scheme!='https' or u.netloc!='p0-creator-media-private.douyin.com'
            or u.username or u.password):raise PublishError('抖音编辑页封面来源不符')
    match=re.fullmatch(r'/(tos-cn-i-dy/[A-Za-z0-9_-]+)~[^/]+',u.path)
    if not match:raise PublishError('抖音编辑页封面路径未覆盖')
    return image_uri(match[1])


def observe(page,value,transcodes):
    from douyin_browser import unique
    if unique(page.locator(editor.TITLE),'已发布作品标题').input_value()!=value['title']:
        raise PublishError('抖音编辑页标题与独立详情不符')
    if editor.native_document(unique(page.locator(editor.DESCRIPTION),'已发布作品简介'))!=dict(text=value['caption'],topics=value['topics']):
        raise PublishError('抖音编辑页完整简介/原生话题与详情不符')
    if editor.declaration_control(page).inner_text()!=value['declaration']:
        raise PublishError('抖音编辑页 AI 声明与详情不符')
    for role,label in [('vertical','竖封面3:4'),('horizontal','横封面4:3')]:
        section=page.locator('[class^="coverControl-"]').filter(has=page.get_by_text(label,exact=True))
        image=unique(unique(section,'已发布作品'+label).locator('img'),'已发布作品'+label+'图片')
        state=image.evaluate('n=>({src:n.currentSrc,loaded:n.complete&&n.naturalWidth>0})')
        if (not image.is_visible() or state['loaded'] is not True
                or native_image_url(state['src'])!=value['covers'][role]):
            raise PublishError('抖音编辑页横竖封面与详情不符')
    mix=page.locator('[class^="mix-display-"] [class^="mix-name-"]')
    if value['collection']:
        label=unique(mix,'已发布作品合集预览')
        if (label.text_content()!='合集·'+value['collection']['name']
                or not label.evaluate('''n=>[n,...n.querySelectorAll('*')].every(x=>{const s=getComputedStyle(x);return s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>0;})''')):
            raise PublishError('抖音可见合集名称与独立详情不符')
    elif mix.count():
        raise PublishError('抖音详情无合集但编辑页出现合集预览')
    video=unique(page.locator('video'),'已发布作品播放器')
    state=video.evaluate('n=>({src:n.currentSrc,ready:n.readyState,duration:n.duration,width:n.videoWidth,height:n.videoHeight})')
    valid=[]
    for receipt in transcodes:
        try:
            body=receipt['body']
            if receipt['video_id']!=value['video']['vid']:raise ValueError()
            u=urlsplit(body['play_url'])
            if (type(body.get('status_code')) is not int or body['status_code']!=0
                    or u.scheme!='https' or not re.fullmatch(r'[A-Za-z0-9-]+\.douyinvod\.com',u.netloc)
                    or u.username or u.password or not isinstance(body['duration'],(int,float))
                    or isinstance(body['duration'],bool) or not math.isfinite(body['duration'])
                    or abs(body['duration']*1000-value['video']['duration_ms'])>50
                    or not re.fullmatch(r'[1-9][0-9]{0,4}',body['width'])
                    or not re.fullmatch(r'[1-9][0-9]{0,4}',body['height'])):raise ValueError()
            valid.append((body['play_url'],int(body['width']),int(body['height'])))
        except (KeyError,TypeError,ValueError) as exc:
            raise PublishError('抖音转码预览响应不完整或来源不符') from exc
    if (not video.is_visible() or state['ready']<1 or not math.isfinite(state['duration'])
            or abs(state['duration']*1000-value['video']['duration_ms'])>50
            or (state['src'],state['width'],state['height']) not in valid):
        raise PublishError('抖音可见播放器与独立转码响应不符')


def read(page,card,listed,public_identity,bodies,wait):
    click_background(page,card.get_by_text('编辑作品',exact=True))
    wait(DETAIL_PATH);wait(TRANSCODE_PATH)
    page.locator(editor.TITLE).wait_for(state='visible',timeout=15000)
    page.wait_for_function('''()=>{const n=document.querySelector('video');return n&&n.readyState>=1&&n.videoWidth>0;}''',timeout=30000)
    u=urlsplit(page.url)
    if (u.scheme!='https' or u.netloc!='creator.douyin.com'
            or u.path!='/creator-micro/content/post/video' or u.fragment
            or parse_qs(u.query,keep_blank_values=True)!=dict(mid=[listed['remote_id']],enter_from=['edit_item'])):
        raise PublishError('抖音已有作品编辑页未绑定同一作品 ID')
    values=[facts(b,listed,public_identity) for b in bodies[DETAIL_PATH]]
    if len({digest(v) for v in values})!=1:raise PublishError('抖音原生作品详情矛盾')
    observe(page,values[0],bodies[TRANSCODE_PATH])
    return values[0]
