"""Bounded native Douyin video prefill. No submit, save-draft or retry upload."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from browser_assets import checked_path, stage
from browser_runtime import background_tab, browser_config, connect, select_page
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json

HOME = 'https://creator.douyin.com/creator-micro/home'
UPLOAD = '/creator-micro/content/upload'
EDITOR = '/creator-micro/content/post/video'
TITLE = 'input[placeholder*="填写作品标题"]'
DESCRIPTION = 'div.zone-container[contenteditable="true"]'
COLLECTION = '.semi-select[class*="select-collection-"]'
DECLARATION_MODAL = '.semi-portal [role="modal"]'
TOPIC_NAME = '.mention-suggest-mount-dom [class^="tag-hash-view-name-"]'
NATIVE_DOCUMENT_READ = '''(n) => {
  const topics=[];
  const lines=Array.from(n.children);
  if (!lines.length || !lines.every(x=>x.matches('.ace-line[data-node="true"]'))) {
    if(n.querySelector('[data-mention],[data-rect-container]')) throw new Error('unknown native document');
    return {text:n.innerText,topics};
  }
  const text=lines.map(line=>{
    const clone=line.cloneNode(true);
    for(const box of clone.querySelectorAll('[data-rect-container="true"]')) {
      const zero=box.children[0],fake=box.children[1],mention=fake?.firstElementChild;
      const label=mention?.children[0];
      if(box.childNodes.length!==2 || !zero?.matches('[data-zero-space="true"]')
        || zero.textContent!=='\\u200b' || !fake?.matches('[data-fake-text=" "][contenteditable="false"]')
        || fake.childNodes.length!==1 || !mention?.matches('[data-mention="#"]')
        || mention.childNodes.length!==3 || mention.firstChild.textContent!=='\\u00a0'
        || mention.lastChild.textContent!=='\\u00a0' || mention.children.length!==1
        || !label?.textContent.startsWith('#') || label.textContent.length<2)
          throw new Error('unknown native topic structure');
      topics.push(label.textContent.slice(1));
      box.replaceWith(document.createTextNode(label.textContent));
    }
    if(clone.querySelector('[data-mention],[data-zero-space],[data-fake-text]'))
      throw new Error('unrecognized native entity');
    for(const marker of clone.querySelectorAll('[data-string="true"][data-enter="true"]')) {
      if(marker.textContent!=='\\u200b') throw new Error('unknown native line marker');
      marker.remove();
    }
    return clone.textContent;
  }).join('\\n');
  return {text,topics};
}'''
CARET_AT_END = '''(n) => {
  const s=getSelection();
  if(!s || !s.rangeCount || !s.isCollapsed || !n.contains(s.anchorNode)) return false;
  const parent=s.anchorNode.nodeType===3?s.anchorNode.parentElement:s.anchorNode;
  const lines=Array.from(n.children);
  if(lines.length && parent.closest('.ace-line[data-node="true"]')!==lines[lines.length-1]) return false;
  for(const marker of n.querySelectorAll('[data-string="true"][data-enter="true"]'))
    if(marker.textContent!=='\\u200b') return false;
  const r=s.getRangeAt(0).cloneRange();r.setEnd(n,n.childNodes.length);
  const fragment=r.cloneContents();
  for(const marker of fragment.querySelectorAll('[data-string="true"][data-enter="true"]')) {
    // A range starting after the native marker contains an empty partial clone.
    // The original marker above must still have its verified full value.
    if(marker.textContent!=='' && marker.textContent!=='\\u200b') return false;
    marker.remove();
  }
  return fragment.textContent==='' && !fragment.querySelector('[data-mention]');
}'''
DESCRIPTION_READ = '''(n) => {
  const lines=Array.from(n.children);
  if (!lines.length || !lines.every(x=>x.matches('.ace-line[data-node="true"]')))
    return n.innerText;
  return lines.map(line=>{
    const clone=line.cloneNode(true);
    for (const marker of clone.querySelectorAll('[data-string="true"][data-enter="true"]')) {
      if (marker.textContent !== '\\u200b') throw new Error('unknown native line marker');
      marker.remove();
    }
    return clone.textContent;
  }).join('\\n');
}'''
DECLARATIONS = {'内容由AI生成', '内容为个人观点或见解', '内容为转载信息',
                '内容含营销推广信息', '虚构演绎，仅供娱乐', '无需添加自主声明'}


def settings(profile, target):
    account = profile.get('accounts', {}).get(target['account'], {})
    identity = account.get('public_identities', {}).get('douyin')
    if (target['platform'] != 'douyin' or 'douyin' not in account.get('platforms', [])
            or not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', identity)):
        raise PublishError('抖音须绑定实际抖音号，不能使用昵称或本地别名')
    endpoint, browser = browser_config(profile)
    return identity, endpoint, browser


def declaration(metadata):
    ai = metadata.get('ai_generated')
    if 'ai_generated' in metadata and type(ai) is not bool:
        raise PublishError('ai_generated 须为真实布尔值')
    label = metadata.get('declaration', '内容由AI生成' if ai is True else None)
    if (not isinstance(label, str) or label not in DECLARATIONS
            or (ai is True and label != '内容由AI生成')
            or (ai is False and label == '内容由AI生成')):
        raise PublishError('抖音单选声明须明确且与 AI 来源事实一致；不自动推断无需声明')
    return label


def check_pack(pack):
    if pack['platform'] != 'douyin' or pack['endpoint'] != 'prefill' or pack['content_type'] != 'video':
        raise PublishError('当前抖音包内路线仅 video:prefill；不保存或公开发送')
    fields, metadata = pack['fields'], pack['platform_metadata']
    if pack['document'] or pack['cover_asset_id'] or pack['subtitle_asset_ids']:
        raise PublishError('抖音文章、指定封面和字幕尚未接入；不静默省略')
    topic_names(pack)
    if (len(pack['assets']) != 1 or pack['assets'][0]['kind'] != 'video'
            or Path(pack['assets'][0]['path']).suffix.lower() != '.mp4'):
        raise PublishError('当前抖音路线须且仅须一个 MP4；其他素材不能丢弃')
    if (not fields['title'].strip() or len(fields['title']) > 30
            or len(topic_document(pack)['text']) > 1000
            or (fields['text'] and fields['description'] and fields['text'] != fields['description'])):
        raise PublishError('抖音标题/简介不符合当前已核对字段；不截断或丢弃第二套描述')
    if set(metadata) - {'ai_generated', 'declaration', 'collection_name'}:
        raise PublishError('抖音尚未映射合集ID、定时或其他扩展字段；不忽略')
    if 'collection_name' in metadata and (not isinstance(metadata['collection_name'], str)
            or not metadata['collection_name'].strip()):
        raise PublishError('指定合集名称必须为非空字符串')
    declaration(metadata)


def topic_names(pack):
    names = [tag.lstrip('#') for tag in pack['fields']['tags']]
    # This is the bounded engine's supported input, not a platform-wide limit.
    if (len(names) > 5 or len(set(names)) != len(names)
            or any(not name or re.search(r'[#\s\u200b]', name) for name in names)):
        raise PublishError('当前抖音原生话题支持最多五个不同的非空名称；不能含空白或井号')
    return names


def topic_document(pack):
    fields = pack['fields']; names = topic_names(pack)
    if not names:
        return dict(text=fields['composed_text'], topics=[])
    body = fields['text'] or fields['description']
    links = '\n'.join((x.get('label', '')+'：' if x.get('label') else '')+x['url'] for x in fields['links'])
    # Native selection adds one ordinary trailing space per topic. Preserve it
    # explicitly; never strip authored spaces or invisible characters globally.
    value = (body+'\n\n' if body else '') + ''.join('#'+name+' ' for name in names)
    if links:
        value += '\n\n'+links
    return dict(text=value, topics=names)


def origin(page, path):
    parts = urlsplit(page.url)
    if parts.scheme != 'https' or parts.netloc != 'creator.douyin.com' or parts.path != path:
        raise PublishError('抖音原生页面来源或路径改变；不认领替代入口')


def assert_identity(page, expected):
    # The editor exposes a nickname only. A temporary native homepage in the
    # SAME context independently exposes the stable Douyin ID; never export cookies.
    with background_tab(page) as identity:
        identity.goto(HOME, wait_until='domcontentloaded', timeout=15000)
        origin(identity, '/creator-micro/home')
        number = identity.get_by_text(re.compile(r'^抖音号[：:]?')).filter(visible=True)
        number.wait_for(state='visible', timeout=15000)
        if number.count() != 1:
            raise PublishError('抖音原生首页账号编号不是唯一')
        actual = re.sub(r'^抖音号\s*[：:]?\s*', '', number.inner_text())
        if actual != expected:
            raise PublishError('抖音实际登录账号与封存目标不符')


def no_warning(page):
    for alert in page.locator('[role="alert"], [role="dialog"], [role="modal"]').all():
        if alert.is_visible() and re.search(r'验证码|安全验证|操作频繁|账号异常|captcha|rate limit', alert.inner_text(), re.I):
            raise PublishError('抖音出现验证或频率提示；停止，不自动重试')


def unique(node, label):
    if node.count() != 1 or not node.is_visible():
        raise PublishError('抖音' + label + '不能唯一核对')
    return node


def empty_upload(page):
    origin(page, UPLOAD)
    if page.locator(TITLE+', '+DESCRIPTION+', video').count():
        raise PublishError('抖音已有编辑内容；不替换作者稿件')
    file = page.locator('input[type="file"]')
    if (file.count() != 1 or 'video/' not in (file.get_attribute('accept') or '')
            or not file.evaluate('(n)=>!n.files||!n.files.length')):
        raise PublishError('抖音上传入口不唯一或已有文件；不继续')
    return file


def text_fields(page):
    origin(page, EDITOR)
    return unique(page.locator(TITLE), '标题'), unique(page.locator(DESCRIPTION), '简介')


def assert_empty_fields(page):
    title, description = text_fields(page)
    if title.input_value() or description_text(description):
        raise PublishError('抖音出现已有文案；保留现场，不覆盖')
    return title, description


def description_text(field):
    # Ignore only verified editor-owned line markers; preserve authored hidden
    # characters and whitespace. The original DOM is never altered.
    return field.evaluate(DESCRIPTION_READ)


def native_document(field):
    return field.evaluate(NATIVE_DOCUMENT_READ)


def wait_document(page, expected):
    page.wait_for_function('''expected => {
      const n=document.querySelector('''+json.dumps(DESCRIPTION)+''');
      if(!n) return false;
      const actual=('''+NATIVE_DOCUMENT_READ+''')(n);
      return actual.text===expected.text && JSON.stringify(actual.topics)===JSON.stringify(expected.topics);
    }''', arg=expected, timeout=5000)
    if native_document(page.locator(DESCRIPTION)) != expected:
        raise PublishError('抖音原生话题与完整文案读回不符')


def append_text(field, text):
    if not field.evaluate(CARET_AT_END):
        raise PublishError('抖音简介光标不在末尾；不猜测插入位置')
    # Enter is an editor action, not a literal newline inside a text leaf.
    for index, part in enumerate(text.split('\n')):
        if index:
            field.press('Enter', timeout=5000)
        if part:
            field.press_sequentially(part, timeout=5000)


def fill_description(page, pack, *, progress=lambda phase: None):
    field = page.locator(DESCRIPTION); names = topic_names(pack)
    if not names:
        field.fill(pack['fields']['composed_text'])
        return
    fields = pack['fields']; body = fields['text'] or fields['description']
    field.fill(body)
    wait_document(page, dict(text=body, topics=[]))
    current = body
    if body:
        append_text(field, '\n\n'); current += '\n\n'
    chosen = []
    for index, name in enumerate(names):
        progress('topic_'+str(index+1)+'_search')
        if not field.evaluate(CARET_AT_END):
            raise PublishError('抖音话题插入光标不在末尾')
        unique(page.get_by_text('#添加话题', exact=True), '添加话题').click(timeout=5000)
        wait_document(page, dict(text=current+'#', topics=list(chosen)))
        append_text(field, name)
        wait_document(page, dict(text=current+'#'+name, topics=list(chosen)))
        match = page.locator(TOPIC_NAME).filter(has_text=re.compile('^'+re.escape(name)+'$')).filter(visible=True)
        match.wait_for(state='visible', timeout=10000)
        unique(match, '指定原生话题').click(timeout=5000)
        current += '#'+name+' '; chosen.append(name)
        progress('topic_'+str(index+1)+'_readback')
        wait_document(page, dict(text=current, topics=list(chosen)))
    links = '\n'.join((x.get('label', '')+'：' if x.get('label') else '')+x['url'] for x in fields['links'])
    if links:
        append_text(field, '\n\n'+links)
    wait_document(page, topic_document(pack))


def video_identity(page):
    origin(page, EDITOR)
    video = unique(page.locator('video'), '视频预览')
    value = video.evaluate('(n)=>({src:n.currentSrc,duration:n.duration,ready:n.readyState})')
    parts = urlsplit(value.get('src') or '')
    duration = value.get('duration')
    if (parts.scheme != 'https' or parts.netloc != 'video-cn.douyin.com' or not parts.path
            or type(duration) not in {int, float} or not math.isfinite(duration) or duration <= 0
            or value.get('ready', 0) < 1
            or page.locator('input[type="file"]').evaluate_all('(ns)=>ns.some(n=>n.files&&n.files.length)')):
        raise PublishError('抖音上传预览未完成或来源无法确认')
    unique(page.get_by_text('重新上传', exact=True), '已上传状态')
    # This native preview also places a signature/expiry BEFORE the object path.
    # Bind the observed TOS object, not its expiring transport URL. This remains
    # a session preview identity, never a saved draft ID or a public work ID.
    match = re.fullmatch(r'/(?:[^/]+/){2}(tos-cn-v-[A-Za-z0-9-]+/[A-Za-z0-9_-]+)', parts.path)
    if not match:
        raise PublishError('抖音视频预览对象路径改变；不能确认原上传')
    return dict(preview_key=digest([parts.netloc, match.group(1)]), duration=duration)


def declaration_control(page):
    section = page.get_by_text('自主声明', exact=True).locator('xpath=ancestor::section[1]')
    return unique(section.locator('div[class^="selectText-"]'), '声明字段')


def select_declaration(page, label, *, progress=lambda phase: None):
    field = declaration_control(page)
    if field.inner_text() != '请选择自主声明':
        raise PublishError('抖音已有声明；不替换作者选择')
    field.click(timeout=5000)
    progress('declaration_modal')
    modal = page.locator(DECLARATION_MODAL).filter(has_text='对作品内容添加声明').filter(visible=True)
    modal.wait_for(state='visible', timeout=5000)
    unique(modal, '声明弹窗')
    choose_declaration(page, modal, label, progress=progress)


def choose_declaration(page, modal, label, *, progress=lambda phase: None):
    # The addon span is covered by its native label hit area; click the label
    # itself, never force a click through the radio control.
    option = modal.locator('label').filter(has_text=re.compile('^'+re.escape(label)+'$'))
    progress('declaration_choice')
    unique(option, '声明选项').click(timeout=5000)
    chosen = modal.locator('label:has(input[type="radio"]:checked)')
    # This is a live preview, not saved declaration evidence. Wait for its
    # parent render before checking the transient radio state below.
    progress('declaration_live_preview')
    preview = declaration_control(page).filter(has_text=re.compile('^'+re.escape(label)+'$'))
    preview.wait_for(state='visible', timeout=5000)
    # First selection can update the outer field and remount this native form,
    # resetting its transient radio state. Only in that observed state select
    # the SAME value once more. Never change choices or loop confirmations.
    if chosen.count() == 0 and declaration_control(page).inner_text() == label:
        progress('declaration_remounted')
        unique(option, '重绘后的同一声明选项').click(timeout=5000)
    progress('declaration_checked')
    chosen.wait_for(state='visible', timeout=5000)
    if chosen.count() != 1 or chosen.inner_text() != label:
        raise PublishError('抖音声明单选读回不符')
    progress('declaration_confirm')
    unique(modal.get_by_role('button', name='确定', exact=True), '声明确认').click(timeout=5000)
    # The native modal may remain mounted during its close transition.
    progress('declaration_closed')
    modal.wait_for(state='hidden', timeout=5000)
    if declaration_control(page).inner_text() != label:
        raise PublishError('抖音声明保存后的原生字段不符')


def collection_control(page):
    return unique(page.locator(COLLECTION), '合集字段')


def collection_value(page):
    selection = unique(collection_control(page).locator('.semi-select-selection-text'), '合集选中状态')
    title = selection.locator('[class^="selected-item-title-"]')
    if title.count() == 1 and title.is_visible():
        return title.inner_text()
    if title.count() == 0 and selection.inner_text() == '请选择合集':
        return '请选择合集'
    raise PublishError('抖音合集选中名称无法唯一核对')


def select_collection(page, name):
    control = collection_control(page)
    if collection_value(page) != '请选择合集':
        raise PublishError('抖音已有合集选择；不替换')
    control.click(timeout=5000)
    # Text includes an episode counter; match the native title, never a substring
    # of the full option or the hidden placeholder option.
    title = page.locator('[role="option"] [class^="option-title-"]').filter(
        has_text=re.compile('^'+re.escape(name)+'$')).filter(visible=True)
    title.wait_for(state='visible', timeout=5000)
    unique(title, '指定合集')
    option = title.locator('xpath=ancestor::*[@role="option"][1]')
    unique(option, '指定合集选项').click(timeout=5000)
    selected = control.locator('.semi-select-selection-text [class^="selected-item-title-"]').filter(
        has_text=re.compile('^'+re.escape(name)+'$'))
    selected.wait_for(state='visible', timeout=5000)
    if collection_value(page) != name:
        raise PublishError('抖音合集选择后读回不符；不能忽略')


def verify_fields(page, pack, saved):
    title, description = text_fields(page)
    expected = topic_document(pack)
    actual = native_document(description)
    if title.input_value() != pack['fields']['title'] or actual != expected:
        raise PublishError('抖音标题或简介已改变；不报告预填完成')
    if video_identity(page) != saved.get('video'):
        raise PublishError('抖音视频预览身份已改变；不重新上传')
    metadata = pack['platform_metadata']
    if declaration_control(page).inner_text() != declaration(metadata):
        raise PublishError('抖音声明与交接来源事实不符')
    selected = collection_value(page)
    if selected != metadata.get('collection_name', '请选择合集'):
        raise PublishError('抖音合集与交接包不符')


def marker_path(request_path):
    return Path(request_path).with_suffix('.browser-attempt.json')


def dispatch(request_path, action, profile, lock_fd):
    if action not in {'execute', 'verify'}: raise PublishError('不支持的抖音阶段')
    try: os.fstat(lock_fd)
    except OSError as exc: raise PublishError('抖音执行须继承核心账号锁') from exc
    request = read_json(request_path); manifest = read_json(request['manifest_path'])
    if digest(manifest) != request['manifest_digest'] or request['target'] not in manifest.get('targets', []):
        raise PublishError('抖音请求与封存清单不符')
    target = request['target']; identity, endpoint, browser = settings(profile, target)
    if action == 'execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('抖音账号受限，只准备本地包')
    pack = build_pack(manifest, target['id'], profile, check_files=action == 'execute'); check_pack(pack)
    marker = marker_path(request_path)
    bound = dict(pack_digest=digest(pack), identity=identity, cdp_url=endpoint, browser_profile=browser)
    if action == 'execute' and marker.exists():
        raise PublishError('抖音已有尝试；只查询，不再次上传')
    if action == 'verify':
        saved = read_json(marker)
        if any(saved.get(k) != v for k, v in bound.items()) or not saved.get('target_id'):
            raise PublishError('抖音原编辑会话绑定不符')
    from browser_engine import target_id
    # Even verification opens an owned read-only identity tab; serialize its UI
    # lifecycle with all other operations sharing this dedicated Chrome profile.
    with connect(profile, writing=True) as connection:
        page = select_page(connection, 'douyin', target_id=saved['target_id'] if action == 'verify' else None)
        assert_identity(page, identity); no_warning(page)
        if action == 'execute':
            empty_upload(page)
            assets = stage(pack, request_path)
            saved = dict(bound, target_id=target_id(page), state='attempted', phase='upload',
                         tracking_scope='chrome_session_only', video=None)
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(saved, stream); stream.flush(); os.fsync(stream.fileno())
            assert_identity(page, identity); no_warning(page)
            empty_upload(page).set_input_files(checked_path(assets[0]), timeout=15000)
            page.wait_for_function('''() => {
              const ns=Array.from(document.querySelectorAll('video'));
              return ns.length===1 && ns[0].currentSrc.startsWith('https://video-cn.douyin.com/')
                && ns[0].readyState>=1 && Number.isFinite(ns[0].duration) && ns[0].duration>0
                && Array.from(document.querySelectorAll('input[type=file]')).every(n=>!n.files||!n.files.length);
            }''', timeout=180000)
            saved['video'] = video_identity(page); saved['state'] = 'uploaded'; saved['phase'] = 'fields'; write_json(marker, saved)
            assert_identity(page, identity); no_warning(page)
            title, description = assert_empty_fields(page)
            # Do not overwrite an existing native declaration or collection.
            if (declaration_control(page).inner_text() != '请选择自主声明'
                    or collection_value(page) != '请选择合集'):
                raise PublishError('抖音已有声明或合集；不替换作者选择')
            def progress(phase):
                saved['phase'] = phase; write_json(marker, saved)
            title.fill(pack['fields']['title'])
            fill_description(page, pack, progress=progress)
            saved['phase'] = 'declaration'; write_json(marker, saved)
            select_declaration(page, declaration(pack['platform_metadata']), progress=progress)
            if pack['platform_metadata'].get('collection_name'):
                saved['phase'] = 'collection'; write_json(marker, saved)
                select_collection(page, pack['platform_metadata']['collection_name'])
            saved['phase'] = 'readback'; write_json(marker, saved)
            verify_fields(page, pack, saved)
            saved['state'] = 'filled'; saved['phase'] = 'complete'; write_json(marker, saved)
        if saved.get('state') not in {'uploaded', 'filled'} or not saved.get('video'):
            raise PublishError('抖音尚未完成全部字段；只查询，不补填或重传')
        verify_fields(page, pack, saved); assert_identity(page, identity); no_warning(page)
        result = {k:pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')}
        result.update(state='pending' if action == 'execute' else 'prefill_ready',
            remote_id='browser-editor:'+saved['target_id'], remote_url=page.url,
            observed_account_identity=identity, tracking_scope='chrome_session_only',
            platform_draft_saved=False, final_click_available=False)
        if action == 'verify':
            result.update(content_verified=True, verified_pack_digest=digest(pack),
                          verified_asset_ids=[a['id'] for a in pack['assets']])
        return result
