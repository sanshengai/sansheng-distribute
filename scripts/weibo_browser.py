"""Bundled Weibo text/image prefill. Upload receipts are persisted per image.

There is deliberately no final-send action in this module. A Chrome editor ID
is temporary and never denotes a saved Weibo draft or a public post.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from browser_assets import stage, checked_path
from browser_runtime import browser_config, connect, select_page
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json


def settings(profile, target):
    account = profile.get('accounts', {}).get(target['account'], {})
    identity = account.get('public_identities', {}).get('weibo')
    if (target['platform'] != 'weibo' or 'weibo' not in account.get('platforms', [])
            or not isinstance(identity, str) or not re.fullmatch(r'[1-9][0-9]{4,19}', identity)):
        raise PublishError('微博账号须绑定实际数字 UID，不能使用昵称或本地别名')
    endpoint, browser = browser_config(profile)
    return identity, endpoint, browser


def check_pack(pack):
    if (pack['platform'] != 'weibo' or pack['endpoint'] != 'prefill'
            or pack['content_type'] not in {'text', 'images'}):
        raise PublishError('当前微博内置模块仅承接 text/images:prefill；视频和最终发送尚未接入')
    if (pack['cover_asset_id'] or pack['subtitle_asset_ids'] or pack['platform_metadata']
            or (pack['document'] and pack['document']['format'] != 'text')):
        raise PublishError('微博尚未映射独立封面、字幕、长文章或平台扩展字段')
    fields = pack['fields']
    if fields['title'] or (fields['text'] and fields['description'] and fields['text'] != fields['description']):
        raise PublishError('微博普通帖不能静默丢弃标题或第二套描述')
    assets = pack['assets']
    if any(a['kind'] != 'image' for a in assets) or (pack['content_type'] == 'text' and assets):
        raise PublishError('微博此路线只接收图文中的图片；不省略其他素材')
    if len({a['sha256'] for a in assets}) != len(assets):
        raise PublishError('微博重复图片可能复用同一 PID；请制作端去重后重新封存')
    if pack['content_type'] == 'images' and not assets:
        raise PublishError('微博图文缺少图片')
    if not fields['composed_text'].strip():
        raise PublishError('微博预填需要完整文案')


def assert_identity(page, expected):
    parts = urlsplit(page.url)
    if (parts.scheme != 'https' or parts.netloc not in {'weibo.com', 'www.weibo.com'}
            or parts.path not in {'/', '/newlogin', '/mygroups'}):
        raise PublishError('微博首页来源或路径改变；不选择其他发布入口')
    # The profile avatar in the application nav is distinct from feed authors.
    link = page.locator('a:has(img[alt="profile"])')
    if (link.count() != 1 or not link.is_visible()
            or link.get_attribute('href') != '/u/' + expected):
        raise PublishError('微博实际登录 UID 与任务绑定不符或登录失效')


def editor(page):
    card = page.locator('#homeWrap > .woo-panel-main')
    if card.count() != 1:
        raise PublishError('微博发布卡片不是唯一；不猜选第一个')
    node = card.locator('textarea')
    if node.count() != 1 or not node.is_visible():
        raise PublishError('微博原生输入框无法唯一观察')
    return card, node


def media_items(card):
    # Scope to the composer. Feed images and toolbar icons cannot count as uploads.
    return card.locator('img').evaluate_all('''nodes => nodes.filter(n => {
      const src=n.getAttribute('src')||'';
      return /^(blob:|data:)/.test(src) || !!n.closest('.woo-picture-main')?.parentElement?.querySelector(':scope > [title="删除"]');
    }).map(n => {
      try { const u=new URL(n.getAttribute('src'),document.baseURI);
        return {url:u.href,host:u.hostname,path:u.pathname,complete:n.complete,width:n.naturalWidth};
      } catch { return {url:n.getAttribute('src')||'',host:'',path:'',complete:false,width:0}; }
    })''')


def empty_editor(page):
    card, node = editor(page)
    if node.input_value().strip():
        raise PublishError('微博已有文案；保留作者草稿，不覆盖')
    if (media_items(card) or card.locator('video').count()
            or card.locator('input[type="file"]').evaluate_all('(nodes)=>nodes.some(n=>n.files&&n.files.length)')):
        raise PublishError('微博已有媒体或文件选择；不替换原稿')
    return card, node


def assert_no_warning(page):
    for alert in page.locator('[role="alert"], [role="dialog"]').all():
        if alert.is_visible() and re.search(r'验证码|安全验证|操作频繁|账号异常|captcha|rate limit', alert.inner_text(), re.I):
            raise PublishError('微博出现安全验证或频率提示；保留现场，不自动重试')


def upload_response(response):
    parts = urlsplit(response.url)
    return (parts.scheme == 'https' and parts.netloc == 'picupload.weibo.com'
            and parts.path == '/interface/upload.php')


def picture_id(response):
    if not upload_response(response) or response.status != 200:
        raise PublishError('微博图片上传响应来源或状态不符')
    try: data = response.json()
    except Exception as exc: raise PublishError('微博图片上传没有可核验回执') from exc
    pid = data.get('pic', {}).get('pid') if isinstance(data, dict) and data.get('ret') is True else None
    if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9]{8,128}', pid):
        raise PublishError('微博图片上传未返回实际 PID；不能以预览出现冒充完成')
    return pid


def assert_media(card, uploaded):
    items = media_items(card)
    if (card.locator('video').count() or len(items) != len(uploaded)
            or card.locator('input[type="file"]').evaluate_all('(nodes)=>nodes.some(n=>n.files&&n.files.length)')):
        raise PublishError('微博当前媒体数量或类型与上传回执不符')
    expected = [u['pid'] for u in uploaded]
    actual = []
    for item in items:
        pid = Path(item['path']).stem
        if (pid not in expected or item['complete'] is not True or item['width'] <= 0
                or not re.fullmatch(r'(?:[A-Za-z0-9-]+\.)*sinaimg\.cn', item['host'])):
            raise PublishError('微博图片未完成加载或 PID 与回执不符')
        actual.append(pid)
    if actual != expected:
        raise PublishError('微博图片次序或身份与交接包不符')


def marker_path(request_path):
    return Path(request_path).with_suffix('.browser-attempt.json')


def dispatch(request_path, action, profile, lock_fd):
    if action not in {'execute', 'verify'}: raise PublishError('不支持的微博阶段')
    try: os.fstat(lock_fd)
    except OSError as exc: raise PublishError('微博执行须继承核心账号锁') from exc
    request = read_json(request_path); manifest = read_json(request['manifest_path'])
    if digest(manifest) != request['manifest_digest'] or request['target'] not in manifest.get('targets', []):
        raise PublishError('微博请求与封存清单不符')
    target = request['target']; identity, endpoint, browser = settings(profile, target)
    if action == 'execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('微博账号受限，只准备本地包')
    pack = build_pack(manifest, target['id'], profile, check_files=action == 'execute'); check_pack(pack)
    bound = dict(pack_digest=digest(pack), identity=identity, cdp_url=endpoint, browser_profile=browser)
    marker = marker_path(request_path)
    if action == 'execute' and marker.exists():
        raise PublishError('微博任务已有尝试记录；只读核对，不重复填稿或上传')
    if action == 'verify':
        saved = read_json(marker)
        if any(saved.get(k) != v for k, v in bound.items()) or not saved.get('target_id'):
            raise PublishError('微博原编辑会话绑定不符；不认领其他窗口或草稿')
    from browser_engine import target_id
    with connect(profile, writing=action == 'execute') as connection:
        page = select_page(connection, 'weibo', target_id=saved['target_id'] if action == 'verify' else None)
        assert_identity(page, identity); assert_no_warning(page)
        if action == 'execute':
            page.bring_to_front()
            empty_editor(page)
            assets = stage(pack, request_path) if pack['assets'] else []
            saved = dict(bound, target_id=target_id(page), state='attempted',
                         tracking_scope='chrome_session_only', uploaded=[])
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(saved, stream); stream.flush(); os.fsync(stream.fileno())
            assert_identity(page, identity); assert_no_warning(page)
            card, node = empty_editor(page)
            node.fill(pack['fields']['composed_text'])
            if node.input_value() != pack['fields']['composed_text']:
                raise PublishError('微博实际文案与封存包不符；先读回，不重复填写')
            for asset in assets:
                assert_identity(page, identity); assert_no_warning(page)
                card, node = editor(page)
                if node.input_value() != pack['fields']['composed_text']:
                    raise PublishError('微博文案已被修改；停止后续上传')
                assert_media(card, saved['uploaded'])
                file = card.locator('input[type="file"]')
                if file.count() != 1: raise PublishError('微博上传入口不是唯一')
                with page.expect_response(upload_response, timeout=90000) as response:
                    file.set_input_files(checked_path(asset))
                pid = picture_id(response.value)
                if pid in [u['pid'] for u in saved['uploaded']]:
                    raise PublishError('微博重复图片 PID；无法证明图序，不继续上传')
                saved['uploaded'].append(dict(id=asset['id'], sha256=asset['sha256'], pid=pid))
                write_json(marker, saved)
                # Wait on this exact remote image, never a guessed fixed sleep.
                card.locator('img[src*="'+pid+'"]').wait_for(state='visible', timeout=15000)
                page.wait_for_function('''pid => {
                  const imgs=Array.from(document.querySelectorAll('#homeWrap > .woo-panel-main img'))
                    .filter(n => { try {return new URL(n.getAttribute('src'),document.baseURI).pathname.split('/').pop().split('.')[0]===pid;} catch{return false;} });
                  return imgs.length===1 && imgs[0].complete && imgs[0].naturalWidth>0
                    && Array.from(document.querySelectorAll('#homeWrap > .woo-panel-main input[type=file]')).every(n=>!n.files||!n.files.length);
                }''', arg=pid, timeout=15000)
                assert_media(card, saved['uploaded'])
            saved['state'] = 'filled'; write_json(marker, saved)
        card, node = editor(page)
        if node.input_value() != pack['fields']['composed_text']:
            raise PublishError('微博原编辑会话文案已变；不能报告预填完成')
        uploaded = saved.get('uploaded', [])
        if [(u.get('id'), u.get('sha256')) for u in uploaded] != [(a['id'], a['sha256']) for a in pack['assets']]:
            raise PublishError('微博素材回执不完整；保持未知，不重复上传')
        assert_media(card, uploaded)
        assert_identity(page, identity); assert_no_warning(page)
        result = {k:pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')}
        result.update(state='pending' if action == 'execute' else 'prefill_ready',
            remote_id='browser-editor:'+saved['target_id'], remote_url=page.url,
            observed_account_identity=identity, tracking_scope='chrome_session_only',
            platform_draft_saved=False, final_click_available=False)
        if action == 'verify':
            result.update(content_verified=True, verified_pack_digest=digest(pack),
                          verified_asset_ids=[a['id'] for a in pack['assets']])
        return result
