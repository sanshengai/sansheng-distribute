"""Native X video observation and cloud receipts; never sends a post."""
from __future__ import annotations
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit
from browser_assets import checked_path
from publish_core import PublishError, digest

MODAL='[role="dialog"][aria-modal="true"]'
READY='''() => {
 const roots=document.querySelectorAll('[role="dialog"][aria-modal="true"]');
 if(roots.length!==1)return false;
 const r=roots[0],vs=r.querySelectorAll('video'),bs=r.querySelectorAll('[data-testid="tweetButton"]');
 return r.querySelectorAll('[data-testid="attachments"]').length===1 && vs.length===1
 && vs[0].readyState>=1 && vs[0].videoWidth>0 && vs[0].videoHeight>0
 && Number.isFinite(vs[0].duration) && vs[0].duration>0 && bs.length===1
 && !bs[0].disabled && bs[0].getAttribute('aria-disabled')!=='true';
}'''
READ='''async r => {
 const vs=r.querySelectorAll('video'),bs=r.querySelectorAll('[data-testid="tweetButton"]');
 if(r.querySelectorAll('[data-testid="attachments"]').length!==1 || vs.length!==1
 || r.querySelector('[data-testid="attachments"] img') || bs.length!==1
 || bs[0].disabled || bs[0].getAttribute('aria-disabled')==='true')throw Error('not ready');
 const v=vs[0],src=v.currentSrc;
 if(!/^blob:https:\\/\\/x\\.com\\/[0-9a-f-]{36}$/.test(src))throw Error('unknown source');
 const response=await fetch(src);if(!response.ok)throw Error('source unavailable');
 const data=await response.arrayBuffer();
 const sha256=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',data)))
 .map(x=>x.toString(16).padStart(2,'0')).join('');
 return {src,sha256,size:data.byteLength,width:v.videoWidth,height:v.videoHeight,
 duration:v.duration,ready:v.readyState};
}'''


def check(pack):
    assets=pack['assets']
    if (pack['content_type']!='video' or len(assets)!=1
            or assets[0]['kind']!='video' or Path(assets[0]['path']).suffix.lower()!='.mp4'):
        raise PublishError('当前 X 视频路线只接收一条 MP4，不混入图片或其他素材')


def prepare(asset):
    path=Path(checked_path(asset))
    with path.open('rb') as stream:header=stream.read(12)
    if path.stat().st_size<=12 or header[4:8]!=b'ftyp':
        raise PublishError('X 视频文件不是可识别的 MP4；未选择上传')
    return path.stat().st_size


def number(value):
    return type(value) in (int,float) and math.isfinite(value)


def observe(page, asset, size):
    root=page.locator(MODAL)
    if root.count()!=1:raise PublishError('X 视频编辑弹窗不能唯一核对')
    try:value=root.evaluate(READ)
    except Exception as exc:raise PublishError('X 原生视频预览尚未就绪或来源无法核对') from exc
    if (not re.fullmatch(r'blob:https://x\.com/[0-9a-f-]{36}',value.get('src',''))
            or value.get('sha256')!=asset['sha256'] or type(value.get('size')) is not int
            or value['size']!=size or type(size) is not int or size<=0
            or any(type(value.get(k)) is not int or value[k]<=0 for k in ('width','height'))
            or type(value.get('ready')) is not int or value['ready']<1
            or not number(value.get('duration')) or value['duration']<=0):
        raise PublishError('X 视频实际字节、来源、尺寸或时长与任务不符')
    return dict(source_asset_id=asset['id'],source_sha256=asset['sha256'],
                preview_key=digest(value['src']),size=size,width=value['width'],height=value['height'],
                duration=value['duration'])


class Capture:
    """Record only bounded native upload response fields, persisted on each event."""
    def __init__(self,save):self.events=[];self.errors=[];self.save=save
    def response(self,response):
        parts=urlsplit(response.url)
        if (parts.scheme!='https' or parts.netloc!='upload.x.com'
                or parts.path!='/i/media/upload2.json'):return
        commands=parse_qs(parts.query).get('command',[])
        try:
            if len(commands)!=1 or commands[0] not in {'INIT','APPENDMULTI','FINALIZE','STATUS'}:
                raise ValueError('unknown command')
            if len(self.events)>=256:raise ValueError('too many upload responses')
            event={'command':commands[0],'status':response.status,'observed_at':time.time()}
            if commands[0]!='APPENDMULTI':
                body=response.json()
                if not isinstance(body,dict):raise ValueError('invalid response')
                event['body']={k:body[k] for k in ('media_id_string','media_key','size',
                    'expires_after_secs','processing_info','video','errors') if k in body}
            self.events.append(event)
            self.save(self.events,self.errors)
        except Exception:
            self.errors.append('native_upload_response_unrecorded')
            # Persisting a receipt failed: keep a sticky in-memory error as well.
            try:self.save(self.events,self.errors)
            except Exception:pass


def cloud_receipt(events, size, *, now=None):
    now=time.time() if now is None else now
    try:
        if not isinstance(events,list) or not 4<=len(events)<=256:raise ValueError()
        commands=[e['command'] for e in events]
        if (commands[0]!='INIT' or commands.count('INIT')!=1 or commands.count('FINALIZE')!=1
                or commands[-1]!='STATUS' or 'APPENDMULTI' not in commands
                or any(c not in {'INIT','APPENDMULTI','FINALIZE','STATUS'} for c in commands)):
            raise ValueError()
        final=commands.index('FINALIZE')
        if final<2 or any(c!='APPENDMULTI' for c in commands[1:final]) or any(c!='STATUS' for c in commands[final+1:]):
            raise ValueError()
        ident=events[0]['body']['media_id_string'];key=events[0]['body']['media_key']
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9]{1,32}',ident) or not isinstance(key,str) or not key:
            raise ValueError()
        deadlines=[];previous=0
        for event in events:
            command=event['command'];stamp=event['observed_at']
            if not number(stamp) or stamp<previous or stamp>now+5:raise ValueError()
            previous=stamp
            if type(event['status']) is not int or event['status']!={'INIT':202,'APPENDMULTI':204,'FINALIZE':200,'STATUS':200}[command]:
                raise ValueError()
            if command=='APPENDMULTI':continue
            body=event['body'];expiry=body.get('expires_after_secs')
            if (body.get('media_id_string')!=ident or body.get('media_key')!=key or body.get('errors')):
                raise ValueError()
            # Processing STATUS responses omit size and expiry. INIT, FINALIZE,
            # and the terminal succeeded STATUS must still bind them completely.
            intermediate = command=='STATUS' and body.get('processing_info',{}).get('state') in {'pending','in_progress'}
            if expiry is not None:
                if not number(expiry) or expiry<=0:raise ValueError()
                deadlines.append(stamp+expiry)
            elif not intermediate:raise ValueError()
            if command in {'FINALIZE','STATUS'}:
                if 'size' in body or not intermediate:
                    if type(body.get('size')) is not int or body['size']!=size:raise ValueError()
                if body.get('processing_info',{}).get('state') not in {'pending','in_progress','succeeded'}:raise ValueError()
        body=events[-1]['body']
        if (body.get('processing_info',{}).get('state')!='succeeded'
                or body.get('processing_info',{}).get('progress_percent')!=100
                or body.get('video',{}).get('video_type')!='video/mp4' or min(deadlines)<=now):raise ValueError()
        return dict(media_id=ident,media_key=key,size=size,expires_at=min(deadlines),processing='succeeded')
    except (KeyError,TypeError,ValueError,IndexError,AttributeError) as exc:
        raise PublishError('X 原生云端视频回执不完整、身份/大小不符或已过期；不重传') from exc


def verify(page,pack,saved):
    if saved.get('upload_errors'):raise PublishError('X 视频响应记录失败；保持未知，不重传')
    receipt=cloud_receipt(saved.get('upload_events'),saved.get('source_size'))
    if saved.get('cloud_video') is not None and receipt!=saved['cloud_video']:
        raise PublishError('X 视频云端身份与原任务回执不符')
    current=observe(page,pack['assets'][0],receipt['size'])
    if saved.get('native_video') is not None and current!=saved['native_video']:
        raise PublishError('X 原编辑会话的视频身份已改变；不替换原稿')
    return current,receipt
