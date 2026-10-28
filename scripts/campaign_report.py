"""Render an offline, private snapshot of a campaign's existing ledger."""
from __future__ import annotations

import html
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from campaign import _load, campaign_status
from component_results import FIELDS, RESULTS, component_snapshot
from publish_core import PublishError

LABELS = {
    'prepared': ('待执行', 'wait'), 'submission_unknown': ('结果待确认', 'warn'),
    'pending': ('处理中 / 待核验', 'wait'), 'failed': ('执行失败', 'bad'),
    'blocked': ('需要处理', 'bad'), 'draft_ready': ('草稿已就绪', 'wait'),
    'prefill_ready': ('已预填，待作者操作', 'wait'), 'published': ('已发布', 'ok'),
    'broadcasted': ('已群发', 'ok'),
}
PLATFORMS = {'wechat': '微信公众号', 'wechat_channels': '微信视频号', 'x': 'X',
             'weibo': '微博', 'douyin': '抖音', 'bilibili': 'B 站',
             'xiaohongshu': '小红书', 'tencent_video': '腾讯视频',
             'youtube': 'YouTube', 'alipay': '支付宝生活号'}
META = {'collection': '合集', 'collection_name': '合集', 'category': '分类',
        'original': '原创', 'is_original': '原创', 'ai_generated': 'AI 生成',
        'is_ai_generated': 'AI 生成', 'activity': '活动',
        'copyright': '版权声明', 'creation_type': '创作类型', 'declaration': '声明'}
ACTION_LABELS = {'done': '已达到约定终点', 'authorize': '记录授权后执行', 'inspect': '检查任务与错误',
                 'advance': '继续处理原稿', 'author_submit': '作者完成最后发布',
                 'verify': '只读核验原作品', 'resume': '只读恢复', 'execute': '执行'}


def esc(value):
    return html.escape(str(value), quote=True)


def remote_link(value):
    if not isinstance(value, str):
        return ''
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password:
            return ''
    except ValueError:
        return ''
    return '<a href="' + esc(value) + '" rel="noreferrer">打开平台作品</a>'


def _value(value):
    if value is True:
        return '是'
    if value is False:
        return '否'
    if isinstance(value, (str, int, float)):
        return str(value)
    return ''


def display_time(value):
    if not value:
        return '未知'
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M %z') if parsed.tzinfo else value
    except (TypeError, ValueError):
        return str(value)


def render_report(campaign_id, store):
    """Use ledger status and immutable requested fields; never contact platforms."""
    ids = [campaign_id] if isinstance(campaign_id, str) else list(campaign_id)
    if not ids or len(ids) != len(set(ids)):
        raise PublishError('批次不能为空或重复')
    entries = []
    for ident in ids:
        definition = _load(store, ident)
        report = campaign_status(ident, store)
        targets = {row['id']: row for row in definition['manifest']['targets']}
        entries.extend((row, targets[row['target_id']]) for row in report['targets'])
    rows = []
    counts = {}
    for row, target in entries:
        label, tone = LABELS.get(row['state'], (str(row['state']), 'wait'))
        platform = PLATFORMS.get(row['platform'], row['platform'])
        counts.setdefault(platform, {})[label] = counts.setdefault(platform, {}).get(label, 0) + 1
        title = target.get('title') or (target.get('text') or '').split('\n', 1)[0] or '未提供标题'
        tags = target.get('tags') or []
        tag_text = ' '.join('#' + str(tag).lstrip('#') for tag in tags if isinstance(tag, str))
        metadata = target.get('platform_metadata') or {}
        info = []
        if isinstance(metadata, dict):
            for key, name in META.items():
                value = _value(metadata.get(key))
                if value:
                    info.append(name + '：' + value)
        if target.get('cover_asset_id'):
            info.append('封面素材：' + str(target['cover_asset_id']))
        details = '；'.join(info) or '未提供'
        # These are attributed manual observations, not platform verification.
        observations = component_snapshot(store, row)
        components = []
        for key, name in FIELDS.items():
            observed = observations.get(key)
            if observed:
                text = (f'{name}：{RESULTS[observed["result"]]}（人工记录，'
                        f'{display_time(observed["observed_at"])}；{observed["evidence"]}）')
            else:
                text = f'{name}：未核验'
            components.append(esc(text))
        component_html = '<ul class="components">' + ''.join('<li>' + text + '</li>' for text in components) + '</ul>'
        desc = target.get('description') or target.get('text') or '未提供'
        notice = row.get('error') or row.get('reason') or row.get('message') or row.get('author_action') or ''
        action = row.get('author_action') or ACTION_LABELS.get(row.get('next_action'), row.get('next_action')) or '暂无'
        if any(o['result'] == 'mismatch' for o in observations.values()):
            action += '；分项不一致，请处理并复核原作品'
        elif any(o['result'] == 'unknown' for o in observations.values()):
            action += '；分项未确认，请核对原作品'
        rows.append(f'''<article class="item"><div class="platform">{esc(platform)}
          <span class="account">{esc(row['account'])}</span></div><div class="body"><div class="top"><h2>{esc(title)}</h2>
          <span class="status {tone}">{esc(label)}</span></div><p class="detail">{esc(desc)}</p>
          <dl><div><dt>目标</dt><dd>{esc(row['endpoint'])}</dd></div><div><dt>下一步</dt><dd>{esc(action)}</dd></div>
          <div><dt>原始提示</dt><dd>{esc(notice or '暂无')}</dd></div><div><dt>话题（计划）</dt><dd>{esc(tag_text or '未提供')}</dd></div>
          <div><dt>封面和声明（计划）</dt><dd>{esc(details)}</dd></div></dl>
          <div class="checks"><strong>分项结果（人工观察）</strong>{component_html}</div>
          <div class="foot"><span>账本更新时间：{esc(display_time(row.get('updated_at')))}</span>{remote_link(row.get('remote_url'))}</div>
          </div></article>''')
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M %z')
    overview = ''.join(f'<li><strong>{esc(platform)}</strong>：{esc("；".join(f"{label} {count}" for label, count in states.items()))}</li>' for platform, states in counts.items())
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>分发结果</title><style>
:root{{color-scheme:light;--ink:#173b40;--soft:#5d7477;--paper:#f2f7f5;--line:#d7e5df;--blue:#185e66}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:PingFang SC,Microsoft YaHei,sans-serif;line-height:1.6}}
main{{max-width:1080px;margin:auto;padding:52px 28px 80px}}header{{border-bottom:2px solid var(--ink);padding-bottom:22px;margin-bottom:28px}}
header p{{color:var(--soft);margin:4px 0}}h1{{font-size:clamp(30px,5vw,48px);letter-spacing:.02em;margin:0;font-weight:700}}
.summary{{font-size:18px;font-weight:600;margin-top:16px}}.overview{{display:flex;flex-wrap:wrap;gap:10px;list-style:none;padding:0;margin:18px 0 0}}.overview li{{background:#fff;border:1px solid var(--line);border-radius:6px;padding:8px 12px}}.item{{background:#fff;display:grid;grid-template-columns:170px 1fr;border:1px solid var(--line);margin:12px 0;border-radius:8px;overflow:hidden}}
.platform{{background:#e1eee9;padding:24px;font-size:19px;font-weight:700}}.account{{display:block;font-size:13px;font-weight:400;color:var(--soft);overflow-wrap:anywhere}}
.body{{padding:22px 26px}}.top{{display:flex;gap:16px;align-items:start;justify-content:space-between}}h2{{font-size:19px;line-height:1.4;margin:0;overflow-wrap:anywhere}}
.status{{white-space:nowrap;border-radius:4px;padding:4px 9px;font-size:13px;font-weight:700;background:#e7eef1;color:#36515b}}.ok{{background:#d9f1e4;color:#186443}}.bad{{background:#fce7e2;color:#a3372c}}.warn{{background:#fff0cf;color:#795000}}
.detail{{white-space:pre-wrap;overflow-wrap:anywhere;color:#455e61}}dl{{display:grid;grid-template-columns:1fr 1fr;gap:10px 24px;margin:22px 0}}dl div{{min-width:0}}dt{{color:var(--soft);font-size:13px}}dd{{margin:2px 0 0;overflow-wrap:anywhere}}
.checks{{border-top:1px solid var(--line);padding:12px 0;color:#455e61;font-size:14px}}.components{{list-style:none;margin:6px 0 0;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;overflow-wrap:anywhere}}
.foot{{border-top:1px solid var(--line);padding-top:12px;display:flex;justify-content:space-between;gap:15px;color:var(--soft);font-size:13px}}a{{color:var(--blue)}}a:focus-visible{{outline:3px solid #397d86;outline-offset:4px}}
@media(max-width:680px){{main{{padding:28px 16px}}.item{{grid-template-columns:1fr}}.platform{{padding:12px 18px}}.account{{display:inline;margin-left:10px}}.body{{padding:18px}}.top{{flex-direction:column}}dl{{grid-template-columns:1fr}}.components{{grid-template-columns:1fr}}.foot{{flex-direction:column}}}}
</style></head><body><main><header><p>分发任务 · 本地结果</p><h1>这次发布，进展如何？</h1>
<p class="summary">共 {len(rows)} 个目标</p><p>生成于 {esc(stamp)}。这是本地账本的上次记录，并非实时刷新；草稿、预填与已公开分别显示。旧任务的待执行状态不能证明平台没有其他作品。</p>
<p>封面、话题和声明的计划不代表平台已确认；分项结果为所记录的人工观察，不是本报告实时核验。此页面可能含私有内容，请妥善保存。</p><ul class="overview">{overview}</ul></header>{''.join(rows)}</main></body></html>'''


def write_report(campaign_id, store, output):
    """Create a new private file without overwriting an existing artifact."""
    rendered = render_report(campaign_id, store)
    path = Path(output).expanduser().resolve()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(rendered)
    except OSError as exc:
        raise PublishError(f'无法创建报告：{exc.strerror}') from exc
    return {'campaign_id': campaign_id, 'report': str(path), 'remote_checked': False}
