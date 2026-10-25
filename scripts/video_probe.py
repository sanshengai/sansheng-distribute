"""Native local MP4 metadata in an owned blank page, before visiting a platform."""
from __future__ import annotations
import math
from pathlib import Path
from browser_assets import checked_path
from publish_core import PublishError

READ = '''async()=>{
 const f=document.querySelector('input')?.files?.[0],v=document.querySelector('video');
 if(!f||!v)throw Error('missing local source');
 const url=URL.createObjectURL(f);let timer;
 try {
  const loaded=new Promise((resolve,reject)=>{
   v.onloadedmetadata=resolve;v.onerror=()=>reject(Error('decode failed'));
   timer=setTimeout(()=>reject(Error('metadata timeout')),20000);
  });
  v.src=url;await loaded;
  return {width:v.videoWidth,height:v.videoHeight,duration:v.duration,size:f.size};
 } finally {clearTimeout(timer);v.removeAttribute('src');v.load();URL.revokeObjectURL(url);}
}'''


def inspect(page, asset):
    if page.url != 'about:blank':
        raise PublishError('视频元数据检查只允许本任务新建的空白页；不改平台页面')
    source=Path(checked_path(asset))
    page.set_content('<!doctype html><input type="file" accept="video/mp4"><video preload="metadata"></video>')
    page.locator('input[type=file]').set_input_files(str(source),timeout=120000)
    try:
        value=page.evaluate(READ)
        if (not isinstance(value,dict) or value.get('size')!=source.stat().st_size
                or any(type(value.get(k)) is not int or not 0<value[k]<=16384 for k in ('width','height'))
                or type(value.get('duration')) not in (int,float)
                or not math.isfinite(value['duration']) or value['duration']<=0):
            raise ValueError('invalid native metadata')
        checked_path(asset)
    except Exception as exc:
        raise PublishError('本地 MP4 元数据无法核对；未访问上传页面') from exc
    return value
