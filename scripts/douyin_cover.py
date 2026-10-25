"""Native Douyin cover prefill; separate portrait/landscape sources and receipts."""
from __future__ import annotations
import base64
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit
from browser_assets import checked_path
from publish_core import PublishError, digest

ROLES = {'portrait': ('竖封面3:4', '竖封面预览（3:4）', 3, 4),
         'landscape': ('横封面4:3', '横封面预览（4:3）', 4, 3)}
CONTROL = '[class^="coverControl-"]'
PROMPT = '设置横封面获更多流量'
ENCODE = '''async value => {
  const image=new Image();image.src=value;
  await image.decode();
  const width=image.naturalWidth,height=image.naturalHeight;
  if(!width||!height||width>4096||height>4096) throw new Error('unsupported cover dimensions');
  const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
  canvas.getContext('2d').drawImage(image,0,0);
  return {width,height,jpeg:canvas.toDataURL('image/jpeg')};
}'''


def requested(pack):
    value = pack['platform_metadata'].get('cover_assets', {})
    if not isinstance(value, dict) or set(value)-set(ROLES):
        raise PublishError('抖音 cover_assets 只接受 portrait / landscape 素材ID')
    result = dict(value)
    if pack['cover_asset_id']:
        if result.get('portrait', pack['cover_asset_id']) != pack['cover_asset_id']:
            raise PublishError('抖音单封面与 portrait 封面冲突')
        result['portrait'] = pack['cover_asset_id']
    assets = {x['id']: x for x in pack['assets']}
    for role, ident in result.items():
        if (not isinstance(ident, str) or not ident or ident not in assets
                or assets[ident]['kind'] != 'image'
                or Path(assets[ident]['path']).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}):
            raise PublishError('抖音封面须明确引用目标中的 PNG/JPEG/WebP 图片')
    if len(set(result.values())) != len(result):
        raise PublishError('横竖封面须分别提供对应比例的素材；不自动裁切')
    return result


def unique(node, label, *, visible=True):
    if node.count() != 1 or (visible and not node.is_visible()):
        raise PublishError('抖音'+label+'不能唯一核对')
    return node


def control(page, role):
    return unique(page.locator(CONTROL).filter(
        has=page.get_by_text(ROLES[role][0], exact=True)), '封面字段')


def modal(page, role):
    return page.locator('[role="dialog"]').filter(
        has=page.get_by_text(ROLES[role][1], exact=True)).filter(visible=True)


def encode(page, asset, role):
    path = Path(checked_path(asset))
    if path.stat().st_size > 20*1024*1024:
        raise PublishError('当前抖音封面引擎只接受不超过20MiB的源图')
    mime = {'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.webp':'image/webp'}[path.suffix.lower()]
    src = 'data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode('ascii')
    try:
        result = page.evaluate(ENCODE, src)
        w, h = result['width'], result['height']
        ratio = ROLES[role][2:]
        if (type(w) is not int or type(h) is not int or not 0<w<=4096 or not 0<h<=4096
                or w*ratio[1] != h*ratio[0] or not result['jpeg'].startswith('data:image/jpeg;base64,')):
            raise ValueError('wrong image ratio or encoding')
        native = base64.b64decode(result['jpeg'].split(',', 1)[1], validate=True)
        if not native: raise ValueError('empty JPEG')
    except Exception as exc:
        raise PublishError('抖音封面解码或比例不符；竖版须3:4、横版须4:3，边长至多4096，不自动裁切') from exc
    return dict(result, source_asset_id=asset['id'], source_sha256=asset['sha256'],
                native_sha256=hashlib.sha256(native).hexdigest())


def preview(page, role):
    image = unique(control(page, role).locator('img'), '已保存封面')
    value = image.evaluate('(n)=>({src:n.currentSrc,width:n.naturalWidth,height:n.naturalHeight,complete:n.complete})')
    parts = urlsplit(value.get('src', ''))
    match = re.fullmatch(r'/(tos-cn-i-[a-z0-9-]+/[A-Za-z0-9_-]+)~[A-Za-z0-9_.-]+', parts.path)
    if (parts.scheme != 'https' or not re.fullmatch(r'p\d+-creator-media-private\.douyin\.com', parts.netloc)
            or not match or not value.get('complete')
            or type(value.get('width')) is not int or type(value.get('height')) is not int
            or value['width']<=0 or value['height']<=0):
        raise PublishError('抖音已保存封面的来源或图片尚不能核对')
    return dict(object_key=digest([parts.netloc,match.group(1)]), width=value['width'], height=value['height'])


def prepare(page, assets, roles):
    index={x['id']:x for x in assets}
    # Decode ALL sources before any video or cover upload begins.
    return {role:encode(page,index[ident],role) for role,ident in roles.items()}


def restore_video(page):
    if page.locator('video').count()==0:
        unique(page.get_by_text('预览视频',exact=True),'视频预览标签').click(timeout=5000)
        page.wait_for_function('''()=>{const vs=document.querySelectorAll('video');
          return vs.length===1&&vs[0].readyState>=1;}''',timeout=10000)


def upload(page, assets, prepared, *, progress, receipt):
    if not prepared:return
    index={x['id']:x for x in assets}
    originals={}
    # Both fields are task-owned empty/default cover selectors, never replace an
    # author's already selected custom cover. Check every requested role first.
    for role in prepared:
        field=control(page,role)
        unique(field.get_by_text('选择封面',exact=True),'空封面选择入口')
        originals[role]=field.locator('img').evaluate_all('(ns)=>ns.map(n=>n.currentSrc)')
    open_landscape=False
    for role in ('portrait','landscape'):
        if role not in prepared:continue
        source=prepared[role]
        if not (role=='landscape' and open_landscape):
            progress('cover_'+role+'_open')
            unique(control(page,role).get_by_text('选择封面',exact=True),'选择封面').click(timeout=5000)
        dialog=modal(page,role);dialog.wait_for(state='visible',timeout=10000)
        unique(dialog,'封面编辑器')
        uploader=dialog.locator('.semi-upload').filter(has_text='点击上传文件或拖拽文件到这里')
        unique(uploader,'原生封面上传器',visible=False)
        file=unique(uploader.locator('input.semi-upload-hidden-input'),'封面文件输入',visible=False)
        if not file.evaluate('(n)=>!n.files||!n.files.length'):
            raise PublishError('抖音封面已有待上传文件；不覆盖')
        progress('cover_'+role+'_upload')
        file.set_input_files(checked_path(index[source['source_asset_id']]),timeout=15000)
        # The native uploader converts the source to default browser JPEG. Bind
        # this exact conversion, not the source PNG bytes or a visual guess.
        page.wait_for_function('''expected=>{
          const ds=Array.from(document.querySelectorAll('[role="dialog"]')).filter(n=>n.getBoundingClientRect().width>0);
          const images=ds.flatMap(n=>Array.from(n.querySelectorAll('[class^="notImg-"] img')));
          return images.length===1&&images[0].complete&&images[0].src===expected;
        }''',arg=source['jpeg'],timeout=20000)
        image=unique(dialog.locator('[class^="notImg-"] img'),'原生封面源图',visible=False)
        actual=image.evaluate('(n)=>({src:n.src,width:n.naturalWidth,height:n.naturalHeight})')
        if actual!={'src':source['jpeg'],'width':source['width'],'height':source['height']}:
            raise PublishError('抖音封面实际源图与封存素材转换不符')
        if role=='portrait' and 'landscape' in prepared:
            # Use the native next step inside this same editor. Completing the
            # portrait first can auto-populate BOTH fields and remove the other
            # empty selector; the optional marketing prompt is not repeatable.
            progress('cover_landscape_transition')
            unique(dialog.get_by_role('button',name='设置横封面',exact=True),'切换横封面').click(timeout=5000)
            dialog.wait_for(state='hidden',timeout=10000)
            open_landscape=True
            continue
        progress('cover_'+role+'_complete')
        unique(dialog.get_by_role('button',name='完成',exact=True),'封面完成按钮').click(timeout=5000)
        if role=='portrait':
            page.wait_for_function('''()=>{
              const ds=Array.from(document.querySelectorAll('[role="dialog"]')).filter(n=>n.getBoundingClientRect().width>0);
              return ds.some(n=>n.innerText.includes('设置横封面获更多流量'))
                ||!ds.some(n=>n.innerText.includes('竖封面预览（3:4）'));
            }''',timeout=15000)
            prompt=page.locator('[role="dialog"]').filter(has=page.get_by_text(PROMPT,exact=True)).filter(visible=True)
            if prompt.count():
                progress('cover_landscape_prompt')
                unique(prompt,'横封面提示')
                next_step='设置横封面' if 'landscape' in prepared else '暂不设置'
                unique(prompt.get_by_role('button',name=next_step,exact=True),'横封面提示选项').click(timeout=5000)
                prompt.wait_for(state='hidden',timeout=5000)
                open_landscape='landscape' in prepared
            if not open_landscape:dialog.wait_for(state='hidden',timeout=15000)
        else:dialog.wait_for(state='hidden',timeout=15000)
    for role,source in prepared.items():
        progress('cover_'+role+'_readback')
        page.wait_for_function('''label=>{
          const ns=Array.from(document.querySelectorAll('[class^="coverControl-"]')).filter(n=>n.innerText.includes(label));
          const im=ns.length===1?ns[0].querySelector('img'):null;
          return im&&im.complete&&im.naturalWidth>0;
        }''',arg=ROLES[role][0],timeout=15000)
        actual=preview(page,role)
        raw=control(page,role).locator('img').get_attribute('src')
        if raw in originals[role] or abs(actual['width']-source['width'])>1 or abs(actual['height']-source['height'])>1:
            raise PublishError('抖音封面保存结果未改变或发生额外裁切；不报告完成')
        receipt(role,dict(actual,source_asset_id=source['source_asset_id'],source_sha256=source['source_sha256'],
                          native_sha256=source['native_sha256']))
    restore_video(page)


def verify(page, pack, saved):
    roles=requested(pack);receipts=saved.get('covers',{})
    if not isinstance(receipts,dict) or set(roles)!=set(receipts):
        raise PublishError('抖音横竖封面回执不完整；只查询，不补传')
    assets={a['id']:a for a in pack['assets']}
    for role,ident in roles.items():
        record=receipts[role]
        if (record.get('source_asset_id')!=ident or record.get('source_sha256')!=assets[ident]['sha256']
                or not re.fullmatch(r'[0-9a-f]{64}',record.get('native_sha256',''))
                or preview(page,role)!={k:record.get(k) for k in ('object_key','width','height')}):
            raise PublishError('抖音已保存封面与原任务不符；不重传或改封面')
    if roles:restore_video(page)
