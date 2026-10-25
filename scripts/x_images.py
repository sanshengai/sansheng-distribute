"""Native X image attachments. Local blob previews, never cloud upload receipts."""
from __future__ import annotations
import base64
from pathlib import Path
import re
from browser_assets import checked_path
from publish_core import PublishError, digest

MODAL = '[role="dialog"][aria-modal="true"]'
ATTACHMENTS = '[data-testid="attachments"]'
DECODE = '''async src => {
  const image=new Image(); image.src=src; await image.decode();
  return {width:image.naturalWidth,height:image.naturalHeight};
}'''
READ = r'''async root => {
  const containers=root.querySelectorAll('[data-testid="attachments"]');
  const images=Array.from(root.querySelectorAll('[data-testid="attachments"] img'));
  if(containers.length!==1 || root.querySelector('video') || !images.length
      || root.querySelector('[data-testid="attachments"] [role="progressbar"]')
      || Array.from(root.querySelectorAll('input[type="file"]')).some(n=>n.files?.length))
    throw new Error('incomplete native attachments');
  return await Promise.all(images.map(async image=>{
    const src=image.currentSrc;
    if(!/^blob:https:\/\/x\.com\/[0-9a-f-]{36}$/.test(src)
        || !image.complete || !image.naturalWidth || !image.naturalHeight)
      throw new Error('unknown native preview');
    const response=await fetch(src);
    if(!response.ok) throw new Error('preview unavailable');
    const data=await response.arrayBuffer();
    const sha256=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',data)))
      .map(n=>n.toString(16).padStart(2,'0')).join('');
    return {src,sha256,size:data.byteLength,width:image.naturalWidth,height:image.naturalHeight};
  }));
}'''
READY = '''expected => {
  const roots=document.querySelectorAll('[role="dialog"][aria-modal="true"]');
  if(roots.length!==1) return false;
  const root=roots[0], images=root.querySelectorAll('[data-testid="attachments"] img');
  return root.querySelectorAll('[data-testid="attachments"]').length===1
    && images.length===expected && Array.from(images).every(n=>n.complete&&n.naturalWidth>0)
    && !root.querySelector('video,[data-testid="attachments"] [role="progressbar"]')
    && Array.from(root.querySelectorAll('input[type="file"]')).every(n=>!n.files?.length);
}'''


def check(pack):
    assets=pack['assets']
    if (pack['content_type']!='images' or not 1<=len(assets)<=4
            or any(x['kind']!='image' or Path(x['path']).suffix.lower()
                   not in {'.png','.jpg','.jpeg','.webp'} for x in assets)):
        raise PublishError('当前 X 图文支持一至四张 PNG/JPEG/WebP，不混入视频或动画')


def static_image(data,ext):
    if ext in {'.jpg','.jpeg'}:
        return data.startswith(b'\xff\xd8')
    if ext=='.png':
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):return False
        offset=8
        while offset+12<=len(data):
            size=int.from_bytes(data[offset:offset+4],'big');kind=data[offset+4:offset+8]
            end=offset+12+size
            if end>len(data) or kind==b'acTL':return False
            if kind==b'IEND':return size==0 and end==len(data)
            offset=end
        return False
    if ext=='.webp':
        if (data[:4]!=b'RIFF' or data[8:12]!=b'WEBP'
                or int.from_bytes(data[4:8],'little')+8!=len(data)):return False
        offset=12
        while offset+8<=len(data):
            kind=data[offset:offset+4];size=int.from_bytes(data[offset+4:offset+8],'little')
            end=offset+8+size+(size%2)
            if end>len(data) or kind in {b'ANIM',b'ANMF'}:return False
            if kind==b'VP8X' and (size<1 or data[offset+8]&2):return False
            offset=end
        return offset==len(data)
    return False


def prepare(page, assets):
    # Validate ALL snapshots and dimensions before changing the editor.
    for asset in assets:
        path=Path(checked_path(asset));size=path.stat().st_size
        if not 0<size<=5*1024*1024:
            raise PublishError('当前 X 图片执行器接受不超过5MiB的单图')
        data=path.read_bytes();ext=path.suffix.lower()
        if not static_image(data,ext):
            raise PublishError('X 图片格式与扩展名不符或为动画；此路线仅静态图片')
        mime={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}[ext]
        source='data:'+mime+';base64,'+base64.b64encode(data).decode('ascii')
        try:
            value=page.evaluate(DECODE,source)
            if any(type(value.get(k)) is not int or not 0<value[k]<=4096 for k in ('width','height')):
                raise ValueError('unsupported dimensions')
        except Exception as exc:
            raise PublishError('X 图片无法解码或边长超过当前4096像素范围') from exc


def observe(page, assets):
    root=page.locator(MODAL)
    if root.count()!=1:
        raise PublishError('X 图片编辑弹窗不能唯一核对')
    try:values=root.evaluate(READ)
    except Exception as exc:
        raise PublishError('X 原生图片预览尚未完整或来源无法核对') from exc
    if len(values)!=len(assets):
        raise PublishError('X 图片数与封存素材不符')
    result=[]
    for value,asset in zip(values,assets):
        if (not re.fullmatch(r'blob:https://x\.com/[0-9a-f-]{36}',value.get('src',''))
                or value.get('sha256')!=asset['sha256']
                or type(value.get('size')) is not int or not 0<value['size']<=5*1024*1024
                or any(type(value.get(k)) is not int or not 0<value[k]<=4096 for k in ('width','height'))):
            raise PublishError('X 图片顺序、实际字节或来源与封存素材不符')
        result.append(dict(source_asset_id=asset['id'],source_sha256=asset['sha256'],
                           preview_key=digest(value['src']),width=value['width'],height=value['height']))
    return result


def upload(page, assets):
    root=page.locator(MODAL)
    file=root.locator('input[type="file"][data-testid="fileInput"]')
    if root.count()!=1 or file.count()!=1 or file.is_disabled():
        raise PublishError('X 原生图片文件入口不能唯一核对')
    if file.get_attribute('multiple') is None:
        raise PublishError('X 原生图片入口不支持本路线的多图选择')
    accepts=(file.get_attribute('accept') or '').split(',')
    if not {'image/png','image/jpeg','image/webp'}.issubset(accepts):
        raise PublishError('X 原生图片文件入口类型已改变')
    paths=[checked_path(a) for a in assets]
    file.set_input_files(paths,timeout=15000)
    page.wait_for_function(READY,arg=len(assets),timeout=60000,polling=200)
    return observe(page,assets)


def verify(page, pack, saved):
    current=observe(page,pack['assets'])
    previous=saved.get('images')
    # A lost local receipt does not require another upload: exact native blob
    # bytes can independently prove the entire sealed selection in this target.
    if previous is not None and current!=previous:
        raise PublishError('X 原编辑会话图片身份已改变；不重传或认领其他图片')
    return current
