"""Compact, offline series status from explicitly mapped ledger identities.

The mapping is a local display aid, never an authorization or a platform read.
Series and episode markers are validated against the sealed snapshot; those
markers cannot prove that a wrongly labelled piece of content is the right one.
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from campaign import _load, campaign_status
from campaign_report import PLATFORMS
from publish_core import HEX, PublishError, digest, read_json, target_of


STATES = {
    'published': ('已发布', 'ok'), 'broadcasted': ('已群发', 'ok'),
    'prepared': ('账本待执行', 'wait'), 'pending': ('待核验', 'wait'),
    'submission_unknown': ('结果不明', 'warn'), 'failed': ('需处理', 'bad'),
    'blocked': ('需核对', 'warn'), 'draft_ready': ('草稿', 'wait'),
    'prefill_ready': ('待作者', 'wait'),
}


def esc(value):
    return html.escape(str(value), quote=True)


def _string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise PublishError(f'{label}必须为非空字符串')
    return value


def _snapshot_target(store, entry):
    """Retrieve the precise sealed target and latest ledger row, with no writes."""
    keys = set(entry)
    if keys == {'campaign_id', 'target_id', 'episode'}:
        cid = entry['campaign_id']
        definition = _load(store, cid)
        target_id = _string(entry['target_id'], '目标 ID')
        if target_id not in definition['target_ids']:
            raise PublishError('目标不属于所选批次')
        target = target_of(definition['manifest'], target_id)
        rows = campaign_status(cid, store)['targets']
        row = next((r for r in rows if r['target_id'] == target_id), None)
        if row is None:
            raise PublishError('批次缺少该目标的账本记录')
        return definition['manifest'], target, row
    if keys == {'job_key', 'episode'}:
        key = entry['job_key']
        if not isinstance(key, str) or not HEX.fullmatch(key):
            raise PublishError('非法单任务 ID')
        row = store.status(key)
        snapshot = read_json(store.directory / 'snapshots' / f'{key}.json')
        if digest(snapshot) != row['manifest_digest']:
            raise PublishError('单任务快照与账本摘要不一致')
        target = target_of(snapshot, row['target_id'])
        if store.key(snapshot, target) != key or any(
                row[k] != target[k] for k in ('platform', 'account', 'endpoint')):
            raise PublishError('单任务与封存目标身份不一致')
        return snapshot, target, row
    raise PublishError('映射须使用批次目标或单任务 ID，不得混用或添加未知字段')


def _validate_marker(target, series_code, episode):
    # Only the explicit ID and first line of a title/text are considered.
    # Reject differing markers, so an ambiguous label cannot silently pick one.
    pattern = re.compile(r'(?<![A-Za-z0-9])' + re.escape(series_code) + r'E0*([0-9]+)(?![A-Za-z0-9])', re.I)
    values = [target.get('id', '')]
    for field in ('title', 'text'):
        if isinstance(target.get(field), str):
            values.append(target[field].split('\n', 1)[0])
    found = {int(m.group(1)) for value in values if isinstance(value, str) for m in pattern.finditer(value)}
    if found != {episode}:
        raise PublishError('系列标记缺失、集数不一致或存在歧义')


def render_series_report(spec, store):
    if not isinstance(spec, dict):
        raise PublishError('系列看板映射必须为对象')
    if set(spec) != {'title', 'series_job_prefix', 'series_code', 'episodes', 'platforms', 'targets'}:
        raise PublishError('系列看板映射字段不完整或包含未知字段')
    title = _string(spec['title'], '标题')
    prefix = _string(spec['series_job_prefix'], '系列任务前缀')
    code = _string(spec['series_code'], '系列代码')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', code):
        raise PublishError('系列代码仅支持字母、数字、横线和下划线')
    episodes = spec['episodes']
    if (not isinstance(episodes, list) or not episodes
            or any(type(x) is not int or x <= 0 for x in episodes)
            or len(episodes) != len(set(episodes))):
        raise PublishError('预期集数必须为非空、不重复的正整数列表')
    platforms = spec['platforms']
    if not isinstance(platforms, list) or not platforms:
        raise PublishError('预期平台账号必须为非空列表')
    platform_rows = []
    notes = {}
    explanations = {}
    for item in platforms:
        if not isinstance(item, dict) or not {'platform', 'account'} <= set(item) or set(item) - {'platform', 'account', 'note', 'reason', 'action'}:
            raise PublishError('平台账号字段不正确')
        if not isinstance(item['account'], str):
            raise PublishError('账号必须为字符串')
        pair = (_string(item['platform'], '平台'), item['account'])
        if pair in platform_rows:
            raise PublishError('平台账号重复')
        note = item.get('note', '')
        if not isinstance(note, str) or len(note) > 100:
            raise PublishError('人工备注必须是最多 100 字的字符串')
        notes[pair] = note
        reason, action = item.get('reason', ''), item.get('action', '')
        if any(not isinstance(value, str) or len(value) > 160 for value in (reason, action)):
            raise PublishError('原因与建议必须是最多 160 字的字符串')
        explanations[pair] = (reason, action)
        platform_rows.append(pair)
    entries = spec['targets']
    if not isinstance(entries, list):
        raise PublishError('目标映射必须为列表')
    cells = {}
    used_keys = set()
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get('episode')) is not int or entry['episode'] not in episodes:
            raise PublishError('目标映射集数超出预期')
        manifest, target, row = _snapshot_target(store, entry)
        if not isinstance(manifest.get('job_id'), str) or not manifest['job_id'].startswith(prefix):
            raise PublishError('目标不属于预期系列任务前缀')
        _validate_marker(target, code, entry['episode'])
        pair = (target.get('platform'), target.get('account'))
        if pair not in platform_rows:
            raise PublishError('目标的平台账号不在预期列表中')
        if not pair[1]:
            raise PublishError('未指定账号的平台不能映射目标')
        cell = (pair, entry['episode'])
        if cell in cells:
            raise PublishError('同一平台账号和集数有多个目标，需先核对')
        key = row.get('key')
        if key and key in used_keys:
            raise PublishError('同一账本目标被重复映射')
        if key:
            used_keys.add(key)
        cells[cell] = row

    header = ''.join(f'<th scope="col">第 {n} 集</th>' for n in episodes)
    table_rows = []
    for pair in platform_rows:
        count = 0
        # Only adjacent episodes with identical underlying status merge.
        # Missing mappings never merge with prepared ledger targets.
        groups = []
        for ep in episodes:
            row = cells.get((pair, ep))
            if row is None:
                label, tone = '无记录', 'empty'
                identity = ('missing',)
            elif row.get('error'):
                label, tone = '需核对', 'warn'
                identity = ('error', row.get('state'), str(row['error']))
            else:
                label, tone = STATES.get(row.get('state'), ('状态未知', 'warn'))
                identity = ('state', row.get('state'))
                if row.get('state') in {'published', 'broadcasted'}:
                    count += 1
            if groups and groups[-1]['identity'] == identity and ep == groups[-1]['end'] + 1:
                groups[-1]['end'] = ep
                groups[-1]['span'] += 1
            else:
                groups.append({'identity': identity, 'start': ep, 'end': ep, 'span': 1,
                               'label': label, 'tone': tone})
        row_cells = []
        for group in groups:
            start, end, span = group['start'], group['end'], group['span']
            episode_range = f'第 {start} 集' if start == end else f'第 {start}–{end} 集'
            row_cells.append(f'<td colspan="{span}" aria-label="{esc(episode_range)}：{esc(group["label"])}">'
                             f'<span class="status {group["tone"]}">{esc(group["label"])}</span></td>')
        platform, account = pair
        note = f'<small>人工备注：{esc(notes[pair])}</small>' if notes[pair] else ''
        reason, action = explanations[pair]
        details = ''.join(f'<div><b>{label}：</b>{esc(value)}</div>'
                          for label, value in (('原因', reason), ('建议', action)) if value)
        details = details or '<span class="muted">暂无说明</span>'
        table_rows.append(f'<tr><th scope="row"><strong>{esc(PLATFORMS.get(platform, platform))}</strong>'
                          f'<small>{esc(account or "尚无对应账本")}</small>{note}</th>{"".join(row_cells)}'
                          f'<td class="total">{count} / {len(episodes)}</td>'
                          f'<td class="details">{details}</td></tr>')
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M %z')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 发布进度</title><style>
:root{{color-scheme:light;--ink:#16393e;--muted:#587477;--line:#d7e5df;--paper:#f2f7f5}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:PingFang SC,Microsoft YaHei,sans-serif}}
main{{max-width:1180px;margin:auto;padding:30px 22px}}header{{margin-bottom:16px}}.eyebrow{{color:var(--muted);font-size:13px;margin:0 0 6px}}
h1{{font-size:clamp(25px,3vw,36px);margin:0 0 9px}}.note{{font-size:13px;line-height:1.5;color:var(--muted);margin:0}}
.table-wrap{{background:white;border:1px solid var(--line);border-radius:9px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px;min-width:900px}}th,td{{padding:8px 6px;border-bottom:1px solid #e8f0ed;text-align:center;white-space:nowrap}}thead th{{font-weight:600;color:var(--muted);background:#f8fbfa}}
tbody tr:last-child th,tbody tr:last-child td{{border-bottom:0}}tbody th{{text-align:left;padding-left:15px}}small{{display:block;font-size:11px;color:var(--muted);font-weight:400;margin-top:3px;white-space:normal;max-width:180px;line-height:1.3;overflow-wrap:anywhere}}
.status{{display:inline-block;border-radius:4px;padding:4px 6px;color:#425c60;background:#edf2f1}}.ok{{color:#176044;background:#dcefe5}}.warn{{color:#765108;background:#fff1d4}}.bad{{color:#a5362c;background:#fbe9e5}}.empty{{color:#708381;background:#f2f5f4}}.total{{font-weight:600;font-size:12px}}
.details{{text-align:left;white-space:normal;min-width:210px;max-width:280px;color:var(--muted);font-size:12px;line-height:1.4;overflow-wrap:anywhere}}.details b{{font-weight:600;color:var(--ink)}}.muted{{color:var(--muted)}}
@media(max-width:680px){{main{{padding:20px 12px}}.table-wrap{{border-radius:6px}}}}
</style></head><body><main><header><p class="eyebrow">分发进度 · 本地账本快照</p><h1>{esc(title)}</h1>
<p class="note">生成于 {esc(stamp)}；未实时查询平台。无记录不代表未发布；账本待执行也不能证明平台没有其他作品。</p></header>
<div class="table-wrap" role="region" aria-label="系列分发进度" tabindex="0"><table><thead><tr><th scope="col">平台 / 账号</th>{header}<th scope="col">账本已发布</th><th scope="col">原因与建议</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></div>
</main></body></html>'''


def write_series_report(spec_path, store, output):
    rendered = render_series_report(read_json(spec_path), store)
    path = Path(output).expanduser().resolve()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(rendered)
    except OSError as exc:
        raise PublishError(f'无法创建报告：{exc.strerror}') from exc
    return {'report': str(path), 'remote_checked': False}
