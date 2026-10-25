"""Hash the actual native Douyin preview bytes, without uploading or publishing."""
from __future__ import annotations
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from browser_assets import checked_path
from publish_core import PublishError, digest

# An implementation memory bound, not a claimed platform upload limit.
MAX_BYTES = 256 * 1024 * 1024
READ = r'''async (n,expectedSize) => {
 const before=n.currentSrc,controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),30000);
 try {
  if(!/^https:\/\/video-cn\.douyin\.com\//.test(before))throw Error('unknown source');
  const r=await fetch(before,{cache:'force-cache',credentials:'omit',redirect:'error',signal:controller.signal});
  if(r.status!==200 || r.type==='opaque' || !r.body)throw Error('source unavailable');
  const reader=r.body.getReader(),parts=[];let size=0;
  while(true){
   const {value,done}=await reader.read();if(done)break;
   size+=value.byteLength;
   if(size>expectedSize){await reader.cancel();throw Error('oversized source');}
   parts.push(value);
  }
  if(size!==expectedSize)throw Error('incomplete source');
  const data=new Uint8Array(size);let i=0;
  for(const part of parts){data.set(part,i);i+=part.byteLength;}
  const sha256=[...new Uint8Array(await crypto.subtle.digest('SHA-256',data))]
   .map(n=>n.toString(16).padStart(2,'0')).join('');
  if(n.currentSrc!==before)throw Error('source changed');
  return {src:before,sha256,size,width:n.videoWidth,height:n.videoHeight,
   duration:n.duration,ready:n.readyState};
 }finally{clearTimeout(timer);}
}'''


def prepare(asset):
    size = Path(checked_path(asset)).stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise PublishError('当前抖音原片字节核对支持不超过256MiB的非空视频；未选择上传')
    return size


def observe(page, asset):
    size = prepare(asset)
    node = page.locator('video')
    if node.count() != 1 or not node.is_visible():
        raise PublishError('抖音原片字节核对不能唯一定位可见视频')
    try:
        value = node.evaluate(READ, size)
        parts = urlsplit(value['src'])
        match = re.fullmatch(r'/(?:[^/]+/){2}(tos-cn-v-[A-Za-z0-9-]+/[A-Za-z0-9_-]+)', parts.path)
        if (parts.scheme != 'https' or parts.netloc != 'video-cn.douyin.com' or not match
                or parts.fragment or value.get('sha256') != asset['sha256']
                or type(value.get('size')) is not int or value['size'] != size
                or any(type(value.get(k)) is not int or not 0 < value[k] <= 16384 for k in ('width', 'height'))
                or type(value.get('ready')) is not int or value['ready'] < 1
                or type(value.get('duration')) not in (int, float)
                or not math.isfinite(value['duration']) or value['duration'] <= 0):
            raise ValueError('source facts differ')
        # A local edit during the remote read must not be accepted either.
        checked_path(asset)
        return dict(source_asset_id=asset['id'], source_sha256=asset['sha256'], size=size,
                    preview_key=digest([parts.netloc, match.group(1)]),
                    width=value['width'], height=value['height'], duration=value['duration'])
    except Exception as exc:
        raise PublishError('抖音原生预览字节、来源或媒体信息不能与封存原片核对；不重传') from exc


def verify(page, pack, saved):
    expected = saved.get('source_video')
    if expected is None:
        # Existing editor attempts predate byte receipts. Never retrofit proof or
        # reupload them merely because the stronger evidence is absent.
        return False
    asset = next(a for a in pack['assets'] if a['kind'] == 'video')
    if observe(page, asset) != expected:
        raise PublishError('抖音原片字节回执与原任务不符；保留原稿，不重新上传')
    return True
