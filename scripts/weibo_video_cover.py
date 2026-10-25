"""Native landscape/portrait cover, source/crop/upload PID bound; no silent crop."""
from __future__ import annotations
import base64
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit
from browser_assets import checked_path
from browser_runtime import click_background
from publish_core import PublishError
from weibo_video_upload import READY

CROP=r'''async()=>{
 const q=[document.querySelector('#app')?._vnode],seen=new Set(),found=[];let count=0;
 while(q.length){const n=q.shift();if(!n||typeof n!=='object'||seen.has(n))continue;
 if(++count>2000)throw Error('tree limit');seen.add(n);
 if(n.component){const c=n.component,d=c.data||{},i=d.instance;
 if(d.ready===true&&i?.ready===true&&i.imageData&&typeof i.getCroppedCanvas==='function')found.push({c,d,i});
 q.push(c.subTree);}if(Array.isArray(n.children))q.push(...n.children);
 if(Array.isArray(n.dynamicChildren))q.push(...n.dynamicChildren);if(n.ssContent)q.push(n.ssContent);}
 if(found.length!==1)throw Error('active crop not unique');const {c,d,i}=found[0];
 if(!/^blob:https:\/\/weibo\.com\/[a-f0-9-]{36}$/.test(d.src))throw Error('unknown source');
 const hex=async b=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',b))].map(x=>x.toString(16).padStart(2,'0')).join('');
 const response=await fetch(d.src);if(!response.ok)throw Error('source missing');const source=await response.arrayBuffer();
 const canvas=i.getCroppedCanvas({imageSmoothingEnabled:false,imageSmoothingQuality:'high'});
 const blob=await new Promise(resolve=>canvas.toBlob(resolve));const output=await blob.arrayBuffer();
 return {source_sha256:await hex(source),source_size:source.byteLength,aspect_ratio:c.props.aspectRatio,
 crop:i.getData(true),width:canvas.width,height:canvas.height,native_size:output.byteLength,native_sha256:await hex(output)};
}'''


def prepare(page,asset):
    path=Path(checked_path(asset));size=path.stat().st_size
    if not 0<size<=10*1024*1024:raise PublishError('当前微博封面路线仅支持不超过10MiB的源图')
    mime='image/png' if path.suffix.lower()=='.png' else 'image/jpeg'
    src='data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode('ascii')
    try:
        value=page.evaluate('''async src=>{const i=new Image();i.src=src;await i.decode();return {width:i.naturalWidth,height:i.naturalHeight};}''',src)
        w,h=value['width'],value['height']
        if any(type(x) is not int or not 0<x<=4096 for x in (w,h)) or (w*9!=h*16 and w*4!=h*3):raise ValueError('ratio')
    except Exception as exc:raise PublishError('微博视频封面须可解码的16:9或3:4源图，边长不超过4096；不自动裁切') from exc
    return dict(value,source_asset_id=asset['id'],source_sha256=asset['sha256'],source_size=size)


def verify_crop(value,prepared):
    crop=value.get('crop',{})
    if (value.get('source_sha256')!=prepared['source_sha256'] or value.get('source_size')!=prepared['source_size']
            or value.get('width')!=prepared['width'] or value.get('height')!=prepared['height']
            or value.get('aspect_ratio')!=prepared['width']/prepared['height'] or crop.get('x')!=0 or crop.get('y')!=0
            or crop.get('width')!=prepared['width'] or crop.get('height')!=prepared['height']
            or crop.get('rotate')!=0 or crop.get('scaleX')!=1 or crop.get('scaleY')!=1
            or type(value.get('native_size')) is not int or value['native_size']<=0
            or not re.fullmatch('[a-f0-9]{64}',value.get('native_sha256',''))):
        raise PublishError('微博原生封面源图或裁剪结果不符；不点击完成')


def check_video_ratio(prepared, video):
    # Observed native creator selects 16:9 for landscape and 3:4 for portrait.
    # Square has not been observed; reject rather than guess its crop policy.
    w,h=video['width'],video['height']
    if w==h or prepared['width']* (9 if w>h else 4)!=prepared['height']*(16 if w>h else 3):
        raise PublishError('微博封面与视频方向不符：横屏用16:9，竖屏用3:4；交回制作端，不上传或自动裁切')


def incomplete(page,asset,prepared,saved):
    """Recognize only the exact pre-completion crop stage, without accepting it."""
    if (not isinstance(saved,dict) or saved.get('prepared')!=prepared
            or saved.get('requests')!=[] or saved.get('responses')!=[] or saved.get('errors')!=[]):
        raise PublishError('微博未完成封面状态不明；不认领其他编辑会话')
    checked_path(asset)
    value=page.evaluate(CROP)
    if value.get('source_sha256')!=asset['sha256'] or value.get('source_size')!=prepared['source_size']:
        raise PublishError('微博未完成封面的源图已变；保留未知状态')
    return dict(stage='cover_editing_required',native_aspect_ratio=value['aspect_ratio'],
                source_asset_id=asset['id'],source_sha256=asset['sha256'])


def preview(page,pid):
    link=page.get_by_role('link',name='上传封面',exact=True)
    if link.count()!=1 or not link.is_visible():raise PublishError('微博封面字段不能唯一核对')
    node=link.locator('..').locator('..').locator('.woo-picture-main > img')
    if node.count()!=1 or not node.is_visible():raise PublishError('微博封面预览不能唯一核对')
    value=node.evaluate('(n)=>({src:n.currentSrc,complete:n.complete,width:n.naturalWidth,height:n.naturalHeight})')
    u=urlsplit(value.get('src',''))
    if (u.scheme!='https' or not re.fullmatch(r'(?:[A-Za-z0-9-]+\.)*sinaimg\.cn',u.netloc)
            or Path(u.path).stem!=pid or value.get('complete') is not True
            or any(type(value.get(k)) is not int or value[k]<=0 for k in ('width','height'))):
        raise PublishError('微博封面实际 PID 或加载状态不符')
    return dict(pid=pid,width=value['width'],height=value['height'])


def observe(page,asset,prepared,saved):
    if (not isinstance(saved,dict) or saved.get('errors')!=[] or saved.get('prepared')!=prepared
            or prepared['source_asset_id']!=asset['id'] or prepared['source_sha256']!=asset['sha256']):
        raise PublishError('微博封面来源或上传记录缺失')
    verify_crop(saved.get('crop',{}),prepared)
    requests,responses=saved.get('requests',[]),saved.get('responses',[])
    if (len(requests)!=1 or len(responses)!=1 or requests[0].get('size')!=saved['crop']['native_size']
            or requests[0].get('sha256')!=saved['crop']['native_sha256']
            or requests[0].get('content_type')!='application/octet-stream'
            or responses[0].get('status')!=200 or responses[0].get('ret') is not True
            or not re.fullmatch('[A-Za-z0-9]{8,128}',responses[0].get('pid',''))):
        raise PublishError('微博封面原生裁剪输出与唯一上传回执不符；不重传')
    return preview(page,responses[0]['pid'])


def upload(page,asset,prepared,checkpoint):
    saved=dict(prepared=prepared,requests=[],responses=[],errors=[])
    def persist(phase):checkpoint(phase,saved)
    link=page.get_by_role('link',name='上传封面',exact=True)
    if link.count()!=1 or not link.is_visible():raise PublishError('微博原生封面入口不唯一')
    link.scroll_into_view_if_needed(timeout=5000);persist('before_cover_open')
    with page.expect_file_chooser(timeout=10000) as event:click_background(page,link)
    chooser=event.value;state=chooser.element.evaluate('(n)=>({accept:n.accept,count:n.files.length,multiple:n.multiple})')
    if state!={'accept':'.jpg, .jpeg, .bmp, .gif, .png','count':0,'multiple':False}:
        raise PublishError('微博原生封面选择器改变；不选择文件')
    persist('before_cover_selection');chooser.set_files(checked_path(asset),timeout=15000)
    persist('cover_selected')
    page.locator('.cropper-canvas > img:visible').wait_for(state='visible',timeout=10000)
    page.wait_for_function('''()=>[...document.querySelectorAll('.cropper-canvas > img')].some(i=>i.getBoundingClientRect().width>0&&i.complete&&i.naturalWidth>0)''',timeout=10000)
    saved['crop']=page.evaluate(CROP);verify_crop(saved['crop'],prepared)
    def native(url):
        u=urlsplit(url);return u.scheme=='https' and u.netloc=='picupload.weibo.com' and u.path=='/interface/upload.php'
    def request(req):
        if not native(req.url):return
        try:
            body=req.post_data_buffer
            if req.method!='POST' or not body:raise ValueError('missing native bytes')
            saved['requests'].append(dict(size=len(body),sha256=hashlib.sha256(body).hexdigest(),content_type=req.headers.get('content-type')))
            persist('cover_uploading')
        except Exception:
            saved['errors'].append('cover_request_unrecorded')
            try:persist('cover_unknown')
            except Exception:pass
    def response(res):
        if not native(res.url):return
        try:
            b=res.json();saved['responses'].append(dict(status=res.status,ret=b.get('ret'),pid=b.get('pic',{}).get('pid')))
            persist('cover_uploaded')
        except Exception:
            saved['errors'].append('cover_response_unrecorded')
            try:persist('cover_unknown')
            except Exception:pass
    page.on('request',request);page.on('response',response)
    try:
        button=page.locator('button:visible').filter(has_text=re.compile(r'^\s*完成\s*$'))
        button.scroll_into_view_if_needed(timeout=5000);persist('before_cover_complete');click_background(page,button)
        page.wait_for_function(READY,timeout=90000,polling=250)
        observe(page,asset,prepared,saved);persist('cover_verified')
    finally:
        page.remove_listener('request',request);page.remove_listener('response',response)
    return saved
