"""Owned, unfocused native X video editor; bounded prefill and read-only recovery."""
from __future__ import annotations
import json
import os
from browser_assets import stage,checked_path
from browser_runtime import background_window
from platform_pack import build_pack
from publish_core import PublishError,digest,read_json,write_json
import browser_engine as engine
import x_video


def fill(page, pack, assets, identity, saved, marker):
    """Select once, persist native upload receipts, then fill the sealed copy."""
    engine.assert_identity(page,identity);engine.assert_no_warning(page);engine.empty_editor(page)
    file=page.locator(x_video.MODAL+' input[type="file"][data-testid="fileInput"]')
    accepts=(file.get_attribute('accept') or '').split(',') if file.count()==1 else []
    if file.count()!=1 or file.is_disabled() or 'video/mp4' not in accepts:
        raise PublishError('X 原生视频文件入口不能唯一核对或类型改变')
    def checkpoint(events,errors):
        saved.update(upload_events=events,upload_errors=errors);write_json(marker,saved)
    capture=x_video.Capture(checkpoint)
    page.on('response',capture.response)
    try:
        # Native video selection may reset text; do it before filling the copy.
        engine.assert_identity(page,identity);engine.assert_no_warning(page);engine.empty_editor(page)
        saved['state']='before_native_selection';write_json(marker,saved)
        file.set_input_files(checked_path(assets[0]),timeout=15000)
        page.wait_for_function(x_video.READY,timeout=120000,polling=250)
        engine.assert_identity(page,identity);engine.assert_no_warning(page)
        native,cloud=x_video.verify(page,pack,saved)
        saved.update(native_video=native,cloud_video=cloud,state='cloud_processed');write_json(marker,saved)
        node=engine.editor(page)
        if node.inner_text().strip(' \t\r\n\u200b\ufeff'):
            raise PublishError('X 视频上传后已有其他文字；保留原稿，不覆盖')
        node.fill(pack['fields']['composed_text'])
        if node.inner_text()!=pack['fields']['composed_text']:
            raise PublishError('X 实际视频文案与封存包不符；先只读检查原稿')
        saved['state']='filled';write_json(marker,saved)
    finally:page.remove_listener('response',capture.response)


def dispatch(request_path,action,profile,lock_fd):
    if action not in {'execute','verify'}:raise PublishError('不支持的浏览器阶段')
    try:os.fstat(lock_fd)
    except OSError as exc:raise PublishError('浏览器执行必须继承核心账号锁') from exc
    request=read_json(request_path);manifest=read_json(request['manifest_path'])
    if digest(manifest)!=request['manifest_digest'] or request['target'] not in manifest.get('targets',[]):
        raise PublishError('浏览器请求与封存清单不符')
    target=request['target'];identity,endpoint,browser=engine.settings(profile,target)
    if action=='execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('账号受限，只准备本地包')
    pack=build_pack(manifest,target['id'],profile,check_files=action=='execute');engine.check_pack(pack)
    x_video.check(pack)
    bound=dict(pack_digest=digest(pack),identity=identity,cdp_url=endpoint,browser_profile=browser)
    marker=engine.marker_path(request_path)
    if action=='execute' and marker.exists():raise PublishError('该视频编辑任务已尝试；先只读核对，不重复上传')
    if action=='verify':
        saved=read_json(marker)
        if any(saved.get(k)!=v for k,v in bound.items()) or not saved.get('target_id'):
            raise PublishError('原视频编辑会话绑定改变或缺失；不认领其他草稿')
    with engine.connect(profile,writing=action=='execute') as connection:
        if action=='execute':
            assets=stage(pack,request_path);size=x_video.prepare(assets[0])
            anchor=engine.select_page(connection,'x',path='/home')
            with background_window(anchor,cdp_url=endpoint,retain=True) as page:
                saved=dict(bound,target_id=engine.target_id(page),state='owned_window_opened',
                           tracking_scope='chrome_session_only',source_size=size,upload_events=[],upload_errors=[])
                fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as out:
                    json.dump(saved,out);out.flush();os.fsync(out.fileno())
                page.goto('https://x.com/compose/post',wait_until='domcontentloaded',timeout=25000)
                page.locator(x_video.MODAL+' [data-testid="tweetTextarea_0"]').wait_for(state='visible',timeout=20000)
                fill(page,pack,assets,identity,saved,marker)
        else:
            page=engine.select_page(connection,'x',path='/compose/post',target_id=saved['target_id'])
            engine.assert_identity(page,identity);engine.assert_no_warning(page)
            x_video.verify(page,pack,saved)
            if engine.editor(page).inner_text()!=pack['fields']['composed_text']:
                raise PublishError('X 原视频编辑会话文案已变；不报告预填完成')
        result={k:pack[k] for k in ('manifest_digest','target_id','platform','account')}
        result.update(state='pending' if action=='execute' else 'prefill_ready',
            remote_id='browser-editor:'+saved['target_id'],remote_url=page.url,
            observed_account_identity=identity,tracking_scope='chrome_session_only',
            media_tracking_scope='native_cloud_processed_and_exact_blob',
            platform_draft_saved=False,final_click_available=False)
        if action=='verify':result.update(content_verified=True,verified_pack_digest=digest(pack),
                                         verified_asset_ids=[a['id'] for a in pack['assets']])
        return result
