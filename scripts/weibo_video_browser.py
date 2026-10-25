"""Owned unfocused native Weibo video prefill and independent read-only resume."""
from __future__ import annotations
import json
import os
from pathlib import Path
from browser_assets import checked_path,stage
from browser_runtime import background_window,connect,select_page
from platform_pack import build_pack
from publish_core import PublishError,digest,read_json,write_json
import weibo_browser as home
import weibo_video_upload as video
import weibo_video_fields as fields
import weibo_video_cover as cover
import video_probe


def assets_for_resume(pack,request_path):
    root=Path(request_path).with_suffix('.browser-assets')
    return [dict(a,absolute_path=str(root/(a['id']+Path(a['path']).suffix.lower()))) for a in pack['assets']]


def verify(page,pack,assets,saved):
    if not page.locator('input[type=file]').evaluate_all('(ns)=>ns.every(n=>!n.files.length)'):
        raise PublishError('微博视频存在新增待处理文件；不报告预填完成')
    item=next(a for a in assets if a['kind']=='video')
    native=video.observe_source(page,item,saved)
    if saved.get('native_video') is not None and saved['native_video']!=native:
        raise PublishError('微博原视频编辑会话身份改变；不认领其他稿件')
    actual=fields.observe(page,pack)
    if pack['cover_asset_id']:
        image=next(a for a in assets if a['id']==pack['cover_asset_id'])
        prepared=cover.prepare(page,image)
        cover.observe(page,image,prepared,saved.get('cover'))
    return native,actual


def dispatch(request_path,action,profile,lock_fd):
    if action not in {'execute','verify'}:raise PublishError('不支持的微博视频阶段')
    try:os.fstat(lock_fd)
    except OSError as exc:raise PublishError('微博视频须继承核心账号锁') from exc
    request=read_json(request_path);manifest=read_json(request['manifest_path'])
    if digest(manifest)!=request['manifest_digest'] or request['target'] not in manifest.get('targets',[]):
        raise PublishError('微博视频请求与封存清单不符')
    target=request['target'];identity,endpoint,browser=home.settings(profile,target)
    if action=='execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('微博账号受限，只准备本地包')
    pack=build_pack(manifest,target['id'],profile,check_files=action=='execute');fields.check(pack)
    bound=dict(pack_digest=digest(pack),identity=identity,cdp_url=endpoint,browser_profile=browser)
    marker=home.marker_path(request_path)
    if action=='execute' and marker.exists():raise PublishError('微博视频已有尝试；只读核对，不重复上传')
    if action=='verify':
        saved=read_json(marker)
        if any(saved.get(k)!=v for k,v in bound.items()) or not saved.get('target_id'):
            raise PublishError('微博原视频编辑会话绑定不符；不认领其他窗口')
    from browser_engine import target_id
    with connect(profile,writing=action=='execute') as connection:
        if action=='execute':
            assets=stage(pack,request_path);item=next(a for a in assets if a['kind']=='video')
            source=Path(checked_path(item))
            with source.open('rb') as stream:header=stream.read(12)
            if len(header)!=12 or header[4:8]!=b'ftyp' or not 12<source.stat().st_size<=15*1024**3:
                raise PublishError('微博视频不是受支持的 MP4；未上传')
            anchor=select_page(connection,'weibo',path='/');home.assert_identity(anchor,identity)
            with background_window(anchor,cdp_url=endpoint,retain=True) as page:
                saved=dict(bound,target_id=target_id(page),state='owned_window_opened',
                    tracking_scope='chrome_session_only',source_sha256=item['sha256'],
                    source_size=source.stat().st_size,upload_events=[],upload_errors=[])
                fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as out:json.dump(saved,out);out.flush();os.fsync(out.fileno())
                def progress(phase,extra=None):
                    saved.update(extra or {});saved['state']=phase;write_json(marker,saved)
                local_video=None;prepared=None;image=None
                if pack['cover_asset_id']:
                    # Local blank-page decode precedes every native platform upload.
                    local_video=video_probe.inspect(page,item)
                    image=next(a for a in assets if a['id']==pack['cover_asset_id'])
                    prepared=cover.prepare(page,image)
                    cover.check_video_ratio(prepared,local_video)
                    progress('local_media_verified',{'local_video':local_video})
                page.goto(video.CREATOR,wait_until='domcontentloaded',timeout=25000)
                page.locator(video.BUTTON).wait_for(state='visible',timeout=20000)
                video.assert_identity(page,identity);home.assert_no_warning(page)
                if (page.evaluate(video.READY) or not page.locator('input[type=text],textarea').evaluate_all('(ns)=>ns.every(n=>!n.value)')
                        or not page.locator('input[type=file]').evaluate_all('(ns)=>ns.every(n=>!n.files.length)')
                        or not page.locator('video').evaluate_all('(ns)=>ns.every(n=>!n.currentSrc&&!n.getAttribute("src")&&n.readyState===0)')):
                    raise PublishError('微博新视频窗口已有内容或媒体；不覆盖')
                def checkpoint(events,errors):
                    saved.update(upload_events=events,upload_errors=errors);write_json(marker,saved)
                capture=video.Capture(checkpoint);page.on('response',capture.response)
                try:
                    video.assert_identity(page,identity);home.assert_no_warning(page)
                    video.select_once(page,item,progress)
                    page.wait_for_function(video.READY,timeout=120000,polling=250)
                    video.assert_identity(page,identity);home.assert_no_warning(page)
                    native=video.observe_source(page,item,saved)
                    if local_video and any(native[k]!=local_video[k] for k in ('width','height','duration')):
                        raise PublishError('微博原生视频元数据与本地检查不符；不继续填写')
                    progress('video_verified',{'native_video':native})
                    fields.fill(page,pack,progress)
                    if image is not None:
                        def cover_progress(phase,value):progress(phase,{'cover':value})
                        cover.upload(page,image,prepared,cover_progress)
                    verify(page,pack,assets,saved);progress('filled')
                finally:page.remove_listener('response',capture.response)
        else:
            page=select_page(connection,'weibo',target_id=saved['target_id'],path='/upload/channel')
            video.assert_identity(page,identity);home.assert_no_warning(page)
            assets=assets_for_resume(pack,request_path)
            if saved.get('state')=='cover_selected' and pack['cover_asset_id']:
                if not page.locator('input[type=file]').evaluate_all('(ns)=>ns.every(n=>!n.files.length)'):
                    raise PublishError('微博未完成封面存在新增待处理文件；保留原稿')
                image=next(a for a in assets if a['id']==pack['cover_asset_id'])
                prepared=cover.prepare(page,image)
                partial=cover.incomplete(page,image,prepared,saved.get('cover'))
                item=next(a for a in assets if a['kind']=='video')
                native=video.observe_source(page,item,saved,cover_editing=True)
                if saved.get('native_video')!=native:raise PublishError('微博未完成封面的视频身份已变')
                fields.observe(page,pack,cover_editing=True)
                result={k:pack[k] for k in ('manifest_digest','target_id','platform','account')}
                result.update(state='pending',remote_id='browser-editor:'+saved['target_id'],remote_url=page.url,
                    observed_account_identity=identity,tracking_scope='chrome_session_only',
                    platform_draft_saved=False,final_click_available=False,content_verified=False,
                    author_action='核对原编辑器与封面方向；需换素材时交回制作端重新封存，不重传已完成目标',**partial)
                return result
            verify(page,pack,assets,saved)
        video.assert_identity(page,identity);home.assert_no_warning(page)
        result={k:pack[k] for k in ('manifest_digest','target_id','platform','account')}
        result.update(state='pending' if action=='execute' else 'prefill_ready',
            remote_id='browser-editor:'+saved['target_id'],remote_url=page.url,
            observed_account_identity=identity,tracking_scope='chrome_session_only',
            media_tracking_scope='native_current_file_and_cloud_receipt',
            platform_draft_saved=False,final_click_available=False)
        if action=='verify':result.update(content_verified=True,verified_pack_digest=digest(pack),
                                         verified_asset_ids=[a['id'] for a in pack['assets']])
        return result
