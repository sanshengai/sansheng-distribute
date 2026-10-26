"""Readonly native composer and passive request contract; no private API calls."""
from __future__ import annotations
import json
from urllib.parse import parse_qs,urlsplit

from publish_core import PublishError,digest
from weibo_video_post import CREATION_TYPES
import weibo_video_cover as cover
import weibo_video_upload as upload

COMPOSER=r'''()=>{
 const q=[document.querySelector('#app')?._vnode],seen=new Set(),found=[];
 while(q.length){if(seen.size>=3000)throw Error('native tree limit');
  const n=q.shift();if(!n||typeof n!=='object'||seen.has(n))continue;seen.add(n);
  if(n.component){const c=n.component,p=c.props||{},d=c.data||{};
   if(typeof p.set?.getSuccessData==='function'){
    const state={};for(const k of ['mblog_statement','visible','schedule_timestamp','schedule_id','toolAction','ispost',
      'content','pic_id','media','vote','position','authority','approval_state','selectedDeclarationRequired',
      'selectedDeclarationOptional','declarationReprintSource','statementConfig','share_id','sgid','shopConfig','publishMarkdown'])state[k]=d[k];
    found.push({state,publish:p.set.publish,autoPublish:p.set.autoPublish,action:p.set.action,
      doneDisabled:p.doneDisabled,isAiClip:p.isAiClip,publishType:p.publishType,statementAuth:p.statementAuth});
   }
   q.push(c.subTree);
  }
  if(Array.isArray(n.children))q.push(...n.children);
  if(Array.isArray(n.dynamicChildren))q.push(...n.dynamicChildren);
  if(n.ssContent)q.push(n.ssContent);
 }
 return {composers:found,statement:window.$CONFIG?.flags?.statement?.mblog_statement_list};
}'''


def statement_type(snapshot,label):
    values=snapshot.get('statement')
    if not isinstance(values,list):raise PublishError('微博当前原生内容声明枚举缺失')
    matches=[v.get('type') for v in values if isinstance(v,dict) and v.get('name')==label]
    if len(matches)!=1 or type(matches[0]) is not int or matches[0] not in {0,1,2,3,4}:
        raise PublishError('微博内容声明标签不能绑定唯一原生类型')
    return matches[0]


def composer_state(snapshot,pack):
    """A stale channelData snapshot is deliberately not returned or trusted."""
    try:
        cs=snapshot['composers']
        if not isinstance(cs,list) or len(cs)!=1:raise ValueError()
        c=cs[0];s=c['state'];statement=statement_type(snapshot,pack['platform_metadata']['declaration'])
        expected=dict(mblog_statement=statement,visible=0,schedule_timestamp=0,schedule_id='',toolAction='channel',
            ispost=False,content=pack['fields']['composed_text'],pic_id=[],media={},vote={},position=None,
            authority=0,approval_state=[],selectedDeclarationRequired='',selectedDeclarationOptional=[],
            declarationReprintSource='',statementConfig=None,share_id='',sgid='',shopConfig=None,publishMarkdown=None)
        if (not isinstance(s,dict) or set(s)!=set(expected)
                or any(s[k]!=v or type(s[k]) is not type(v) for k,v in expected.items())
                or c.get('publish') is not True or c.get('autoPublish') is not False
                or c.get('action')!='channel' or c.get('doneDisabled') is not False
                or c.get('isAiClip') is not False or c.get('publishType')!='' or c.get('statementAuth') is not True):
            raise ValueError()
        return dict(statement=statement,state_digest=digest(snapshot))
    except (KeyError,ValueError,TypeError,AttributeError) as exc:
        raise PublishError('微博视频并非已完成上传的公开立即发布状态，或包含未映射选项') from exc


def cover_pid(page):
    node=page.get_by_role('link',name='上传封面',exact=True)
    if node.count()!=1 or not node.is_visible():raise PublishError('微博发送前封面入口不唯一')
    images=node.locator('..').locator('..').locator('.woo-picture-main > img')
    if images.count()!=1 or not images.is_visible():raise PublishError('微博发送前缺少唯一可见封面')
    from weibo_video_post import image_pid
    pid=image_pid(images.evaluate('(n)=>n.currentSrc'))
    cover.preview(page,pid)
    return pid


def default_options(page):
    """Only the observed category/relation-off and clipping-on UI shape."""
    values=page.locator('label.woo-switch-main:visible').evaluate_all('''ns=>ns.map(n=>{const i=n.querySelector('input[type=checkbox]');return {checked:i?.checked,on:i?.getAttribute('true-value'),off:i?.getAttribute('false-value')};})''')
    if (not isinstance(values,list) or len(values)!=3
            or sum(v==dict(checked=False,on='true',off='false') for v in values)!=2
            or sum(v==dict(checked=True,on='1',off='0') for v in values)!=1):
        raise PublishError('微博视频分类、关联或剪辑设置不在已核对范围；不改变作者选项')
    return values


def unique_json(text):
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError('duplicate native JSON key')
            result[key]=value
        return result
    return json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite JSON')))


def native_input(request,pack,saved,snapshot):
    """Validate what the native UI actually sent, without constructing/replaying it.

    Unknown options stop acceptance, including another cover, categories,
    playlists, subtitles, paid/automatic/scheduled posts and structured claims.
    """
    try:
        u=urlsplit(request.url);raw=request.post_data
        if (u.scheme!='https' or u.netloc!='weibo.com' or u.path!='/ajax/statuses/update'
                or request.method!='POST' or not isinstance(raw,str) or not 0<len(raw)<=2*1024*1024
                or request.headers.get('content-type','').split(';',1)[0].strip()!='application/x-www-form-urlencoded'):
            raise ValueError()
        pairs=parse_qs(raw,keep_blank_values=True,strict_parsing=True,max_num_fields=30)
        required={'content','visible','share_id','media','vote'};statement=snapshot['statement']
        if type(statement) is not int or statement not in {0,1,2,3,4} or saved.get('pack_digest')!=digest(pack):raise ValueError()
        allowed=required|({'mblog_statement'} if statement else set())
        # The native creator may add an opaque anti-abuse fingerprint. Observe
        # its presence only; never persist, synthesize or reuse its value.
        if 'fp' in pairs:
            if len(pairs['fp'])!=1 or not 0<len(pairs['fp'][0])<=16384:raise ValueError()
            allowed=allowed|{'fp'}
        if (set(pairs)!=allowed or any(len(v)!=1 for v in pairs.values())
                or pairs['content']!=[pack['fields']['composed_text']] or pairs['visible']!=['0']
                or pairs['share_id']!=[''] or pairs['vote']!=['']
                or (statement and pairs['mblog_statement']!=[str(statement)])):raise ValueError()
        media=unique_json(pairs['media'][0]);assets=pack['assets']
        asset=next(a for a in assets if a['kind']=='video');native=upload.saved_source(asset,saved)
        allowed_media={'type','media_id','titles','covers','free_duration','resource','homemade','contribution','follower_watch_entire'}
        if (not isinstance(media,dict) or set(media)-allowed_media or media.get('type')!='video'
                or media.get('media_id')!=native['media_id']
                or media.get('titles')!=[dict(title=pack['fields']['title'],default='true')]
                or media.get('free_duration')!={'start':0,'end':30}
                or media.get('resource')!={'video_down':1,'allow_clip':1}
                or ('follower_watch_entire' in media and media['follower_watch_entire']!={'enable':0})):
            raise ValueError()
        if (any(type(media['free_duration'][k]) is not int for k in ('start','end'))
                or any(type(media['resource'][k]) is not int for k in ('video_down','allow_clip'))
                or ('follower_watch_entire' in media and type(media['follower_watch_entire']['enable']) is not int)):
            raise ValueError()
        creations=[media[k] for k in ('homemade','contribution') if k in media]
        if (len(creations)!=1 or creations[0]!={'channel_ids':[''],'type':CREATION_TYPES[pack['platform_metadata']['creation_type']]}
                or type(creations[0].get('type')) is not int):raise ValueError()
        covers=media.get('covers')
        # Observed portrait creator includes one empty landscape-slot marker,
        # not a second uploaded picture. Accept only this exact empty marker.
        if (isinstance(covers,list) and len(covers)==2 and covers[1]=={'type':1,'source':1}
                and all(type(covers[1][k]) is int for k in ('type','source'))
                and native['width']*16==native['height']*9
                and isinstance(covers[0],dict) and type(covers[0].get('width')) is int
                and type(covers[0].get('height')) is int and covers[0]['width']*4==covers[0]['height']*3):
            covers=covers[:1]
        if (not isinstance(covers,list) or len(covers)!=1 or not isinstance(covers[0],dict)
                or set(covers[0])-{'pid','source','width','height'}
                or covers[0].get('pid')!=snapshot['cover_pid']
                or type(covers[0].get('source')) is not int or covers[0]['source'] not in {1,11}
                or any(type(covers[0][k]) is not int or covers[0][k]<=0 for k in ('width','height') if k in covers[0])):raise ValueError()
        if pack['cover_asset_id']:
            image=next(a for a in assets if a['id']==pack['cover_asset_id'])
            proof=cover.receipt(image,saved['cover']['prepared'],saved['cover'])
            if proof['pid']!=snapshot['cover_pid']:raise ValueError()
        return dict(media_id=native['media_id'],cover_pid=snapshot['cover_pid'],statement=statement,
                    request_digest=digest(pairs),native_media_digest=digest(media),pack_digest=digest(pack))
    except (KeyError,ValueError,TypeError,AttributeError,StopIteration) as exc:
        raise PublishError('微博原生视频发送载荷与封存文案、源媒体或明确设置不符；不重发') from exc
