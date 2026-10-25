"""Native video fields; creation type and content declaration remain separate."""
from __future__ import annotations
import re
from pathlib import Path
from browser_runtime import click_background
from publish_core import PublishError

TITLE='input[placeholder="填写标题（0～30个字）"]'
TEXT='textarea[placeholder="有什么新鲜事想分享给大家？"]'
DECLARATION='div.woo-pop-wrap[class*="_statement_"] > .woo-pop-ctrl'
TYPES={'原创','二创','转载'}
DECLARATIONS={'无','内容为自主创作','内容为转载','内容由AI生成','内容为虚构演绎'}


def declaration(metadata):
    label=metadata.get('declaration')
    ai=metadata.get('ai_generated')
    if (not isinstance(label,str) or label not in DECLARATIONS
            or not isinstance(metadata.get('creation_type'),str) or metadata['creation_type'] not in TYPES
            or ('ai_generated' in metadata and type(ai) is not bool)
            or (ai is True and label!='内容由AI生成')
            or (ai is False and label=='内容由AI生成')):
        raise PublishError('微博视频须明确类型和独立内容声明，且与 AI 来源事实一致')
    return label


def check(pack):
    if pack['platform']!='weibo' or pack['endpoint']!='prefill' or pack['content_type']!='video':
        raise PublishError('微博视频内置路线仅支持预填，不保存平台草稿或发送')
    fields,meta=pack['fields'],pack['platform_metadata']
    if pack['document'] or pack['subtitle_asset_ids'] or set(meta)-{'creation_type','declaration','ai_generated'}:
        raise PublishError('微博视频未映射文章、字幕、合集、定时或其他扩展字段；不省略')
    declaration(meta)
    videos=[a for a in pack['assets'] if a['kind']=='video']
    covers=[a for a in pack['assets'] if a['kind']=='image']
    if (len(videos)!=1 or Path(videos[0]['path']).suffix.lower()!='.mp4'
            or len(videos)+len(covers)!=len(pack['assets'])
            or ([a['id'] for a in covers]!=[pack['cover_asset_id']] if pack['cover_asset_id'] else bool(covers))
            or any(Path(a['path']).suffix.lower() not in {'.png','.jpg','.jpeg'} for a in covers)):
        raise PublishError('微博视频须一条 MP4 和明确指定的可选 PNG/JPEG 封面')
    if (not fields['title'].strip() or len(fields['title'].encode('utf-16-le'))//2>30
            or not fields['composed_text'].strip() or len(fields['composed_text'].encode('utf-16-le'))//2>1000
            or (fields['text'] and fields['description'] and fields['text']!=fields['description'])):
        raise PublishError('微博视频标题或描述超出当前支持范围，不截断或遗漏文案')
    names=[tag.strip('#') for tag in fields['tags']]
    if (len(names)>5 or len(set(names))!=len(names)
            or any(not name or re.search(r'[#\s\u200b]',name) for name in names)):
        raise PublishError('当前微博视频预填支持最多五个不同的普通 #话题#，不含空白或内部井号')


def unique(node,label,*,hidden=False):
    if node.count()!=1 or (not hidden and not node.is_visible()):raise PublishError('微博视频'+label+'不能唯一核对')
    return node


def snapshot(page,*,cover_editing=False):
    title=unique(page.locator(TITLE),'标题',hidden=cover_editing);text=unique(page.locator(TEXT),'描述',hidden=cover_editing)
    radios=page.locator('label.woo-radio-main'+('' if cover_editing else ':visible')).evaluate_all('''ns=>ns.map(n=>({
      label:n.innerText.trim(),checked:n.querySelector('input[type=radio]')?.checked}))''')
    if len(radios)!=3 or {r['label'] for r in radios}!=TYPES or any(type(r['checked']) is not bool for r in radios):
        raise PublishError('微博视频类型控件改变；不猜选')
    node=unique(page.locator(DECLARATION),'内容声明',hidden=cover_editing)
    label=(node.text_content() if cover_editing else node.inner_text()).strip()
    return dict(title=title.input_value(),description=text.input_value(),radios=radios,declaration=label)


def empty(page):
    value=snapshot(page)
    if (value['title'] or value['description'] or any(r['checked'] for r in value['radios'])
            or value['declaration']!='内容声明'):
        raise PublishError('微博视频已存在文案、类型或声明；保留原稿，不覆盖')


def observe(page,pack,*,cover_editing=False):
    value=snapshot(page,cover_editing=cover_editing)
    if (value['title']!=pack['fields']['title'] or value['description']!=pack['fields']['composed_text']
            or value['declaration']!=declaration(pack['platform_metadata'])
            or [r['label'] for r in value['radios'] if r['checked']]!=[pack['platform_metadata']['creation_type']]):
        raise PublishError('微博视频文案、类型或声明与封存包不符；不报告预填完成')
    return value


def fill(page,pack,checkpoint):
    empty(page)
    for selector,value,phase in [(TITLE,pack['fields']['title'],'title'),(TEXT,pack['fields']['composed_text'],'description')]:
        node=unique(page.locator(selector),phase);limit=node.get_attribute('maxlength')
        if limit and int(limit)>=0 and len(value.encode('utf-16-le'))//2>int(limit):
            raise PublishError('微博当前字段限制不容纳完整文案；不截断')
        checkpoint('before_'+phase);node.fill(value)
        if node.input_value()!=value:raise PublishError('微博实际文案不完整；不重复填写')
    kind=unique(page.locator('label.woo-radio-main:visible').filter(
        has_text=re.compile('^'+re.escape(pack['platform_metadata']['creation_type'])+'$')),'视频类型')
    kind.scroll_into_view_if_needed(timeout=5000);checkpoint('before_creation_type');click_background(page,kind)
    if not kind.locator('input[type=radio]').is_checked():raise PublishError('微博视频类型未生效')
    control=unique(page.locator(DECLARATION),'内容声明');control.scroll_into_view_if_needed(timeout=5000)
    checkpoint('before_declaration_open');click_background(page,control)
    choice=page.locator('.woo-pop-main:visible').get_by_role('button',name=declaration(pack['platform_metadata']),exact=True)
    choice.wait_for(state='visible',timeout=5000);unique(choice,'声明选项');choice.scroll_into_view_if_needed(timeout=5000)
    checkpoint('before_declaration_choice');click_background(page,choice)
    observe(page,pack)
