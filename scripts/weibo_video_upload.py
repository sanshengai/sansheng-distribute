"""Native Weibo uploader primitives. Not a complete prefill/publication adapter.

The creator's existing DOM file input belongs to a different composer. Its
successful cloud transfer does not attach the file to the video creator.
"""
from __future__ import annotations
import json
import hashlib
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit
from browser_assets import checked_path
from browser_runtime import click_background
from publish_core import PublishError

CREATOR = 'https://weibo.com/upload/channel'
BUTTON = 'button[id^="video_button_upload_"]'
READY = '''()=>[...document.querySelectorAll('span')].filter(n=>
 n.textContent==='上传完成'&&n.getBoundingClientRect().width>0&&
 n.getBoundingClientRect().height>0).length===1'''
CHOOSER = '''n=>({id:n.id,connected:n.isConnected,multiple:n.multiple,
 accept:n.accept,count:n.files?.length||0})'''
SOURCE = '''async n=>{
 if(n.files?.length!==1)throw Error('missing source');
 const f=n.files[0],chunks=[];
 for(let start=0;start<f.size;start+=8388608){
   const hash=await crypto.subtle.digest('SHA-256',await f.slice(start,start+8388608).arrayBuffer());
   chunks.push([...new Uint8Array(hash)].map(x=>x.toString(16).padStart(2,'0')).join(''));
 }
 return {size:f.size,chunks};
}'''
NATIVE_SOURCE = '''async()=>{
 const queue=[document.querySelector('#app')?._vnode],seen=new Set(),found=[];
 let count=0;
 while(queue.length){
   if(count>=2000)throw Error('native tree limit');
   const n=queue.shift();if(!n||typeof n!=='object'||seen.has(n))continue;
   seen.add(n);count++;
   if(n.component){
     const c=n.component,d=c.data||{},u=d.wbUploader;
     if(u&&/^video_button_upload_[0-9]+$/.test(d.id||''))found.push({data:d,uploader:u});
     queue.push(c.subTree);
   }
   if(Array.isArray(n.children))queue.push(...n.children);
   if(Array.isArray(n.dynamicChildren))queue.push(...n.dynamicChildren);
   if(n.ssContent)queue.push(n.ssContent);
 }
 if(found.length!==1)throw Error('uploader not unique');
 const {data:d,uploader:u}=found[0],f=u.currFile;
 if(u.version!=='1.2.3'||d.status!=='success'||d.uploadProgress!==100||
    Object.prototype.toString.call(f)!=='[object File]'||!f.initRes?.media_id)
   throw Error('uploader not ready');
 const chunks=[];
 for(let start=0;start<f.size;start+=8388608){
   const hash=await crypto.subtle.digest('SHA-256',await f.slice(start,start+8388608).arrayBuffer());
   chunks.push([...new Uint8Array(hash)].map(x=>x.toString(16).padStart(2,'0')).join(''));
 }
 return {media_id:f.initRes.media_id,size:f.size,chunks,sdk_version:u.version,
         width:u.videoInfo?.width,height:u.videoInfo?.height,duration:u.videoInfo?.duration};
}'''


def observe_source(page, asset, saved, *, cover_editing=False):
    """Read the native SDK's current File and media ID, without invoking it.

    The DOM success label alone also exists in hidden templates and is
    insufficient. No component methods, upload functions or app callbacks run.
    """
    if (saved.get('upload_errors') != [] or saved.get('source_sha256') != asset['sha256']
            or type(saved.get('source_size')) is not int or saved['source_size'] <= 0):
        raise PublishError('微博视频源绑定或上传记录不完整')
    cloud = cloud_receipt(saved.get('upload_events'), saved['source_size'])
    try:
        if cover_editing:
            if saved.get('state')!='cover_selected':raise ValueError('not the pre-completion cover stage')
            if not page.evaluate('''()=>[...document.querySelectorAll('.cropper-canvas > img')].filter(n=>
              n.getBoundingClientRect().width>0&&n.complete&&n.naturalWidth>0).length===1'''):
                raise ValueError('native cover editor not visible')
        elif not page.evaluate(READY):raise ValueError('completion not visible')
        native = page.evaluate(NATIVE_SOURCE)
    except Exception as exc:
        raise PublishError('微博原生上传器状态无法完整只读核对；不重传') from exc
    source = Path(checked_path(asset)); chunks = []; original = hashlib.sha256()
    with source.open('rb') as stream:
        while data := stream.read(8388608):
            original.update(data); chunks.append(hashlib.sha256(data).hexdigest())
    if (not isinstance(native, dict) or original.hexdigest() != asset['sha256'] or native.get('chunks') != chunks
            or type(native.get('size')) is not int or native['size'] != saved['source_size']
            or native.get('media_id') != cloud['media_id'] or native.get('sdk_version') != '1.2.3'
            or any(type(native.get(k)) is not int or native[k] <= 0 for k in ('width','height'))
            or type(native.get('duration')) not in (int,float) or not math.isfinite(native['duration'])
            or native['duration'] <= 0):
        raise PublishError('微博当前视频文件、媒体 ID 或元数据已变；不报告原稿完成')
    return dict(cloud,source_sha256=asset['sha256'],source_asset_id=asset['id'],
                width=native['width'],height=native['height'],duration=native['duration'],
                tracking_scope='native_current_file_and_cloud_receipt')


def assert_identity(page, expected):
    if (page.url != CREATOR or not isinstance(expected, str)
            or not re.fullmatch(r'[1-9][0-9]{4,19}', expected)):
        raise PublishError('微博视频入口或绑定 UID 不符')
    actual = page.evaluate('()=>[window.$CONFIG?.uid,window.$CONFIG?.user?.idstr].map(String)')
    if actual != [expected, expected]:
        raise PublishError('微博视频创建器原生账号身份不符；不上传')


def uploader_button(page):
    """Wait for the SDK's second click binding, not merely button visibility.

    The observed Vue button initially has its framework click listener. The
    native uploader binds another listener after its delayed mounted hook.
    No page event or application function is invoked by this observation.
    """
    node = page.locator(BUTTON)
    if node.count() != 1 or not node.is_visible() or node.is_disabled():
        raise PublishError('微博原生视频上传按钮不能唯一核对')
    ident = node.get_attribute('id')
    if not isinstance(ident, str) or not re.fullmatch(r'video_button_upload_[0-9]+', ident):
        raise PublishError('微博视频上传目标身份改变')
    session = page.context.new_cdp_session(page)
    try:
        obj = session.send('Runtime.evaluate', {
            'expression': 'document.getElementById('+json.dumps(ident)+')'})['result']
        if not obj.get('objectId'):
            raise PublishError('微博视频上传目标缺失')
        start = time.monotonic()
        while time.monotonic()-start < 12:
            listeners = session.send('DOMDebugger.getEventListeners', {'objectId': obj['objectId']})['listeners']
            if sum(x.get('type') == 'click' for x in listeners) >= 2:
                return node
            page.wait_for_timeout(150)
        raise PublishError('微博视频上传器未就绪；不使用其他文件框代替')
    finally:
        session.detach()


def select_once(page, asset, checkpoint):
    """Caller owns the empty editor/attempt and captures responses beforehand.

    checkpoint must durably save before_file_selection before a file is handed
    to Chrome. Any exception is an attempted action, never permission to retry.
    """
    source = Path(checked_path(asset))
    size = source.stat().st_size
    if not 0 < size <= 15*1024**3 or source.suffix.lower() != '.mp4':
        raise PublishError('微博受验视频路线只接受非空 MP4，最大 15 GiB')
    button = uploader_button(page)
    with page.expect_file_chooser(timeout=10000) as event:
        click_background(page, button)
    chooser = event.value
    state = chooser.element.evaluate(CHOOSER)
    if (state.get('id') != '_ef' or state.get('connected') is not False
            or state.get('multiple') is not False or state.get('count') != 0
            or 'video/mp4' not in (state.get('accept') or '').split(',')):
        raise PublishError('微博真正的视频选文件入口不符；不选择其他文件框')
    checkpoint('before_file_selection', {'source_size': size})
    # Playwright may transfer the file bytes over CDP before assigning the
    # detached input. A 15-second click budget is not a video transfer budget.
    chooser.set_files(str(source), timeout=120000)
    proof = chooser.element.evaluate(SOURCE)
    chunks = []; original = hashlib.sha256()
    with source.open('rb') as stream:
        while data := stream.read(8388608):
            original.update(data)
            chunks.append(hashlib.sha256(data).hexdigest())
    if (original.hexdigest() != asset['sha256'] or proof.get('chunks') != chunks
            or type(proof.get('size')) is not int or proof['size'] != size):
        raise PublishError('微博原生选择器文件与封存原件不符；不重传')
    proof = {'source_size': size, 'source_sha256': asset['sha256'], 'source_chunk_count': len(chunks)}
    checkpoint('selected_once', proof)
    return proof


def _one(values, key):
    value = values[key]
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        raise ValueError('ambiguous native field')
    return value[0]


class Capture:
    """Only native INIT/chunks/CHECK receipts; no API or secret reproduction."""
    def __init__(self, save):
        self.events = []; self.errors = []; self.save = save

    def response(self, response):
        u = urlsplit(response.url)
        origins = {'/2/fileplatform/init.json': 'fileplatform.api.weibo.com',
                   '/2/fileplatform/upload.json': 'up.video.weibocdn.com',
                   '/2/fileplatform/check.json': 'fileplatform.api.weibo.com'}
        if u.scheme != 'https' or u.netloc != origins.get(u.path):
            return
        try:
            if len(self.events) >= 4096:
                raise ValueError('too many responses')
            q = parse_qs(u.query, keep_blank_values=True)
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError('invalid response')
            event = {'path': u.path, 'status': response.status, 'observed_at': time.time(),
                     'body': {k: body[k] for k in ('media_id', 'upload_id', 'result') if k in body}}
            event['fields'] = {k: _one(q, k) for k in ('media_id','size','index','count','start_loc','type') if k in q}
            if u.path.endswith('/check.json'):
                form = parse_qs(response.request.post_data or '', keep_blank_values=True)
                event['fields'] = {k: _one(form, k) for k in ('media_id','upload_id','size','count','action','client') if k in form}
            self.events.append(event)
            self.save(self.events, self.errors)
        except Exception:
            self.errors.append('native_upload_response_unrecorded')
            try: self.save(self.events, self.errors)
            except Exception: pass


def cloud_receipt(events, size, *, now=None):
    """Require exact gap-free chunk coverage; cloud upload is not a draft/post."""
    now = time.time() if now is None else now
    try:
        if type(size) is not int or size <= 0 or not isinstance(events, list) or not 3 <= len(events) <= 4096:
            raise ValueError()
        init, finish = events[0], events[-1]
        if init['path'] != '/2/fileplatform/init.json' or finish['path'] != '/2/fileplatform/check.json':
            raise ValueError()
        media = init['body']['media_id']; upload = init['body']['upload_id']
        if (not isinstance(media, str) or not re.fullmatch(r'[1-9][0-9]{4,31}', media)
                or not isinstance(upload, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', upload)
                or init['fields'].get('type') != 'video' or init['fields'].get('size') != str(size)):
            raise ValueError()
        previous = 0
        for e in events:
            stamp = e['observed_at']
            if (e['status'] != 200 or type(e['status']) is not int or type(stamp) not in (int,float)
                    or not math.isfinite(stamp) or stamp < previous or stamp > now+5):
                raise ValueError()
            previous = stamp
        if (finish['body'].get('result') is not True or finish['body'].get('media_id') != media
                or finish['body'].get('upload_id') != upload):
            raise ValueError()
        f = finish['fields']; count = int(f['count'])
        if (f.get('media_id') != media or f.get('upload_id') != upload or f.get('size') != str(size)
                or f.get('action') != 'finish' or f.get('client') != 'web' or count != len(events)-2):
            raise ValueError()
        chunks = {}
        for e in events[1:-1]:
            q = e['fields']; i = int(q['index']); length = int(q['size']); start = int(q['start_loc'])
            if (e['path'] != '/2/fileplatform/upload.json' or e['body'].get('result') is not True
                    or q.get('media_id') != media or q.get('type') != 'video'
                    or q.get('count') != str(count) or i in chunks or not 0 <= i < count
                    or length <= 0 or start < 0):
                raise ValueError()
            chunks[i] = (start, length)
        offset = 0
        for i in range(count):
            start, length = chunks[i]
            if start != offset: raise ValueError()
            offset += length
        if offset != size: raise ValueError()
        return {'media_id': media, 'size': size, 'chunk_count': count,
                'tracking_scope': 'native_cloud_file_accepted_only'}
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        raise PublishError('微博云端视频回执缺失、覆盖不完整或文件身份不符；不报告完成或重传') from exc
