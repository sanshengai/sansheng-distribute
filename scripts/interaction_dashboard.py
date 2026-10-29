#!/usr/bin/env python3
"""Render a private, static snapshot of the local interaction inbox."""
import argparse
from contextlib import closing
import html
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from interaction_drafts import source as draft_source
from publish_core import PublishError
from weibo_official_inbox import overview as weibo_overview
from interaction_monitor import snapshot as monitor_snapshot

PLATFORMS = {'youtube': 'YouTube', 'x': 'X', 'weibo': '微博', 'bilibili': 'B 站',
             'douyin': '抖音', 'wechat': '公众号', 'wechat_channels': '微信视频号',
             'alipay': '支付宝生活号', 'xiaohongshu': '小红书'}
STATES = {'complete': '已检查', 'partial': '检查不完整', 'error': '检查失败',
          'manual': '人工导入（未同步）', 'running': '检查中或上次中断'}


def e(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def local_time(value):
    if not value:
        return '尚无记录'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return '时间未标明时区'
        return parsed.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
    except ValueError:
        return '时间无法解析'


def weibo_read_label(value):
    return {'partial': '官方已读（范围有限）', 'running': '官方检查中',
            'error': '官方检查失败'}.get(value['state'], '官方状态待核对')


def weibo_read_note(value):
    stale = {'error': '本次失败，已有记录可能过时；',
             'running': '本次检查未结束，已有记录可能过时；'}.get(value['state'], '')
    return (f"官方 API：已发现待补上下文 {value['needs_context']} 条，历史 {value['historical']} 条，"
            f"自评论 {value['own']} 条；接口文字表示有差异 {value['representation_differences']} 条；"
            + stale + '不代表全量覆盖或当前可回复；补查父级和可见性后再拟稿，不自动发送')


def watch_note(root, platform, account, channel):
    parts = []
    monitors = [(platform, '读取巡检')]
    if platform == 'youtube':
        monitors.append(('draft-youtube', '拟稿观察'))
    for kind, title in monitors:
        try:
            value = monitor_snapshot(root, kind, account, channel)
        except (PublishError, OSError, ValueError, TypeError):
            parts.append(title + '记录无法核验')
            continue
        if value['state'] == 'not_started' and not value['lock_held']:
            continue
        health = value['health']
        label = ({'running': '执行中', 'sleeping': '等待下轮'}.get(value['state'], '状态待核验')
                 if health == 'lock_observed' else
                 {'overdue': '检查超时', 'stopped_unexpectedly': '意外停止',
                  'terminal': {'completed': '有限观察已结束', 'failed': '失败已停止',
                               'stopped': '已停止'}.get(value['state'], '状态待核验')}.get(health, '状态待核验'))
        note = title + '：' + label
        if 'cycles' in value:
            note += f"，{value['cycles']}/{value['requested_cycles']} 轮，失败 {value['error_cycles']}"
            if value['partial_cycles']:
                note += f"，部分覆盖 {value['partial_cycles']}"
            if health == 'lock_observed' and value['next_due_at']:
                note += '，下次 ' + local_time(value['next_due_at'])
        parts.append(note)
    return ('；' + '；'.join(parts)) if parts else ''


def render(db_path, include_text=False, requested=()):
    state_root = Path(db_path).resolve(strict=True).parent
    uri = Path(db_path).resolve(strict=True).as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        sources = [dict(r) for r in db.execute('SELECT * FROM interaction_sources')]
        items = [dict(r) for r in db.execute('SELECT * FROM interaction_items')]
        has_receipts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_reply_receipts'").fetchone()
        replied = {tuple(r) for r in db.execute('SELECT platform,account,channel_id,parent_id FROM interaction_reply_receipts')} if has_receipts else set()
        has_provenance = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_manual_provenance'").fetchone()
        manual = {tuple(r) for r in db.execute('SELECT platform,account,channel_id,comment_id FROM interaction_manual_provenance')} if has_provenance else set()
        has_baselines = db.execute("SELECT 1 FROM sqlite_master WHERE name='interaction_bilibili_baselines'").fetchone()
        baselines = {('bilibili',r['account'],r['channel_id'],r['comment_id']) for r in
                     db.execute('SELECT * FROM interaction_bilibili_baselines')} if has_baselines else set()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_baselines'").fetchone():
            baselines.update(tuple(r) for r in db.execute(
                'SELECT platform,account,channel_id,comment_id FROM interaction_baselines'))
        has_observations = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_observations'").fetchone()
        observations = [json.loads(r[0]) for r in db.execute('SELECT document FROM interaction_observations ORDER BY observed_at, recorded_at')] if has_observations else []
        weibo_reads = {r['account']: r for r in weibo_overview(db, include_text)}
        has_drafts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_drafts'").fetchone()
        drafts = [dict(r) for r in db.execute('SELECT * FROM interaction_drafts ORDER BY created_at')] if has_drafts else []
        has_dispatches = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_dispatches'").fetchone()
        dispatches = {r['draft_key']: dict(r) for r in db.execute('SELECT * FROM interaction_dispatches')} if has_dispatches else {}
        has_suggestions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_suggestions'").fetchone()
        suggestions = {}
        if has_suggestions:
            for row in db.execute('SELECT * FROM interaction_suggestions ORDER BY created_at,suggestion_key'):
                value = dict(row)
                key = tuple(value[k] for k in ('platform', 'account', 'channel_id', 'comment_id'))
                try:
                    _, fingerprint = draft_source(db, *key)
                    value['current_source'] = fingerprint == value['source_digest']
                except PublishError:
                    value['current_source'] = False
                suggestions[key] = value
        cycle_runs, cycle_states = {}, {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='interaction_draft_cycle_runs'").fetchone():
            for row in db.execute('SELECT * FROM interaction_draft_cycle_runs ORDER BY started_at,rowid'):
                cycle_runs[(row['platform'],row['account'],row['channel_id'])] = dict(row)
            for row in db.execute('SELECT * FROM interaction_draft_cycle_state'):
                cycle_states[(row['platform'],row['channel_id'])] = dict(row)
    by_source = {}
    for item in items:
        key = (item['platform'], item['account'], item['channel_id'])
        by_source.setdefault(key, []).append(item)
    rows = []
    details = []
    seen_platforms = set()
    source_accounts = {(s['platform'], s['account']) for s in sources}
    latest_observations = {}
    for observation in observations:
        latest_observations[(observation['platform'], observation['account'])] = observation
    for src in sorted(sources, key=lambda x: (x['platform'], x['account'], x['channel_id'])):
        platform = src['platform']
        official = weibo_reads.get(src['account']) if platform == 'weibo' else None
        seen_platforms.add(platform)
        key = (platform, src['account'], src['channel_id'])
        incoming = [x for x in by_source.get(key, []) if not x['is_own']]
        verified = [x for x in incoming if (*key, x['comment_id']) in replied]
        pending = [x for x in incoming if x['local_status'] == 'new' and (*key, x['comment_id']) not in replied]
        baseline_count = sum((*key, x['comment_id']) in baselines for x in incoming)
        handled = [x for x in incoming if x['local_status'] == 'handled' and (*key, x['comment_id']) not in baselines]
        unknown = sum(x['local_status'] not in ('new', 'handled') for x in incoming)
        manual_count = sum((*key, x['comment_id']) in manual for x in incoming)
        if src['state'] == 'manual':
            coverage = '人工导入，未进行平台同步'
        elif platform == 'youtube':
            coverage = f"评论串 {src['parents_scanned']}/{src['parents_total']}；回复未完成 {src['reply_incomplete']}"
        else:
            coverage = '覆盖范围以平台同步记录为准'
        note = ('已接官方只读发现；原生历史对话仍保留' if official else
                coverage + (('；' + str(src['error'])) if src.get('error') and src['error'] != coverage else ''))
        note += f'；平台已核验回复 {len(verified)} 条（历史回执）'
        if manual_count:
            note += f'；含人工导入 {manual_count} 条'
        if baseline_count:
            note += f'；历史基线 {baseline_count} 条（不自动拟稿，不计已回复）'
        if (platform, src['account']) in latest_observations and not official:
            observation = latest_observations[(platform, src['account'])]
            note += ('；网页补充（' + local_time(observation['observed_at']) + '）：'
                     + observation['reason'] + '；下一步：' + observation['next_action']
                     + '（不并入同步计数，范围详见折叠记录）')
        if unknown:
            note += f'；其他状态 {unknown}'
        decisions = [json.loads(suggestions[(*key, x['comment_id'])]['decision_json']) for x in pending
                     if (*key, x['comment_id']) in suggestions and suggestions[(*key, x['comment_id'])]['current_source']]
        if decisions:
            counts = {action: sum(d['action'] == action for d in decisions) for action in ('reply', 'review', 'skip')}
            note += f"；模型建议：拟回复 {counts['reply']}／转人工 {counts['review']}／建议跳过 {counts['skip']}（均待审）"
        cycle = cycle_runs.get(key)
        circuit = cycle_states.get((platform, src['channel_id']))
        if cycle:
            labels = {'prepared':'已整理', 'budget_limited':'本轮预算已到上限',
                      'budget_exhausted':'今日预算已用完', 'waiting_for_sync':'等待近期成功同步',
                      'circuit_open':'拟稿暂停', 'policy_changed':'策略已变化',
                      'interrupted':'中断待检查', 'error':'拟稿失败', 'running':'拟稿中'}
            note += ('；最近拟稿周期：' + labels.get(cycle['state'], '状态待核对')
                     + '（' + local_time(cycle['finished_at'] or cycle['started_at']) + '）'
                     + f"，本轮模型请求 {cycle['model_requests']} 次；拟稿不代表发送")
        if circuit and circuit['reason']:
            note += '；拟稿控制：' + circuit['reason']
        if official:
            note += '；' + weibo_read_note(official)
        note += watch_note(state_root, platform, src['account'], src['channel_id'])
        pending_display = str(len(pending)) + (f"；待补上下文 {official['needs_context']}" if official else '')
        rows.append(f"<tr><td>{e(PLATFORMS.get(platform, platform))}</td><td>{e(src['account'])}</td>"
                    f"<td>{e(weibo_read_label(official) if official else STATES.get(src['state'], '状态未知'))}</td><td>{e(pending_display)}</td>"
                    f"<td>{len(handled)}</td><td>{e(local_time(official['last_success_at'] if official else src['last_success_at']))}</td><td>{e(note)}</td></tr>")
        if include_text:
            for item in pending:
                video = str(item['video_id'] or '')
                link = (f'<a href="https://www.youtube.com/watch?v={e(video)}" rel="noreferrer">查看作品</a>'
                        if platform == 'youtube' and re.fullmatch(r'[A-Za-z0-9_-]{11}', video) else '')
                comment_id = str(item['comment_id'] or '')
                if platform == 'x' and re.fullmatch(r'[0-9]{1,19}', comment_id):
                    link = f'<a href="https://x.com/i/web/status/{comment_id}" rel="noreferrer">查看原帖</a>'
                origin = ' · 人工导入' if (*key, item['comment_id']) in manual else ''
                details.append(f"<li><b>{e(PLATFORMS.get(platform, platform))} · {e(item['author_name'] or '未显示昵称')}{origin}</b>"
                               f" <span>{e(local_time(item['published_at']))}</span><p>{e(item['body'])}</p>{link}</li>")
    # Keep evidence types separate without repeating an account in the summary.
    observation_notes = []
    labels = {'observed': '网页已检查', 'partial': '网页部分检查', 'blocked': '受阻', 'paused': '暂停', 'unavailable': '当前不可用'}
    for (platform, account), observation in sorted(latest_observations.items()):
        seen_platforms.add(platform)
        note = '范围：' + observation['scope'] + '；原因：' + observation['reason'] + '；下一步：' + observation['next_action']
        if (platform, account) in source_accounts:
            observation_notes.append(f"<li><b>{e(PLATFORMS.get(platform,platform))} · {e(account)}</b>"
                                     f" · {e(labels[observation['state']])} · {e(local_time(observation['observed_at']))}"
                                     f"<p>{e(note)}</p></li>")
            continue
        official = weibo_reads.get(account) if platform == 'weibo' else None
        if official:
            note += '；' + weibo_read_note(official)
            note += watch_note(state_root, 'weibo', account, official['uid'])
        rows.append(f"<tr><td>{e(PLATFORMS.get(platform, platform))}</td><td>{e(account)}</td>"
                    f"<td>{e(weibo_read_label(official) if official else labels[observation['state']])}</td>"
                    f"<td>{e('待补上下文 ' + str(official['needs_context']) if official else '未统计')}</td><td>另见网页记录</td>"
                    f"<td>{e(local_time(official['last_success_at'] if official else observation['observed_at']))}</td><td>{e(note)}</td></tr>")
    for account, official in weibo_reads.items():
        seen_platforms.add('weibo')
        if ('weibo', account) not in source_accounts and ('weibo', account) not in latest_observations:
            rows.append(f"<tr><td>微博</td><td>{e(account)}</td><td>{e(weibo_read_label(official))}</td>"
                        f"<td>待补上下文 {official['needs_context']}</td><td>未统计</td>"
                        f"<td>{e(local_time(official['last_success_at']))}</td><td>{e(weibo_read_note(official) + watch_note(state_root, 'weibo', account, official['uid']))}</td></tr>")
        if include_text:
            for item in official['pending']:
                views = json.loads(item['views_json'])
                body = ''.join(f"<p>{e(kind)}：{e(view['body'])}</p>" for kind, view in views.items())
                link = (f'<a href="https://weibo.com/{official["uid"]}/{item["work_id"]}" rel="noreferrer">查看作品补查对话</a>'
                        if re.fullmatch(r'[0-9]+', official['uid']) and re.fullmatch(r'[0-9]+', item['work_id']) else '')
                details.append(f"<li><b>微博 · {e(item['author_name'])} · 已发现待补上下文</b>"
                               f"<span>{e(local_time(item['published_at']))}</span>{body}{link}"
                               '<p>根评论、直接父级和当前可回复性尚未确认，未进入回复草稿。</p></li>')
    observation_details = []
    if include_text:
        for observation in observations:
            for reply in observation['replies']:
                observation_details.append(f"<li><b>{e(PLATFORMS.get(observation['platform']))} · {e(reply['author'])}</b>"
                    f" · {e(reply['work'])}<p>原评：{e(reply['comment'])}</p><p>已回复：{e(reply['reply'])}</p>"
                    f"<p>网页读回记录（非 API 回执）：{e(reply['verification'])}</p></li>")
    for platform in requested:
        if platform not in seen_platforms:
            rows.append(f'<tr><td>{e(PLATFORMS.get(platform, platform))}</td><td>—</td><td>无同步记录</td>'
                        '<td>—</td><td>—</td><td>尚无记录</td><td>尚未确认是否有评论</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="7">还没有同步记录，无法判断是否有评论。</td></tr>')
    detail_section = ''
    if include_text:
        detail_section = '<details><summary>查看待处理评论</summary><ul>' + (''.join(details) or '<li>当前记录中没有待处理评论。</li>') + '</ul></details>'
    if observation_notes:
        detail_section += '<details><summary>查看同账号网页观察（与同步分开记账）</summary><ul>' + ''.join(observation_notes) + '</ul></details>'
    if observation_details:
        detail_section += '<details><summary>查看网页回复历史（不计入自动同步）</summary><ul>' + ''.join(observation_details) + '</ul></details>'
    if suggestions:
        suggestion_items = []
        action_labels = {'reply': '建议回复', 'review': '转人工判断', 'skip': '建议跳过'}
        for suggestion in suggestions.values():
            value = json.loads(suggestion['decision_json'])
            label = action_labels.get(value['action'], '未知建议') if suggestion['current_source'] else '原评论变化或已处理，历史建议待复核'
            item = f"<li>{e(PLATFORMS.get(suggestion['platform'], suggestion['platform']))} · {e(suggestion['account'])} · {e(label)}"
            if include_text:
                item += f"<p>{e(value['text'])}</p><p>原因：{e(value['reason'])}</p>"
            suggestion_items.append(item + '</li>')
        detail_section += '<details><summary>模型建议（' + str(len(suggestions)) + ' 条原评论；均待审）</summary><p>只显示每条原评论最近保存的一项建议；作品或对话仍须复核。建议跳过不会标记已处理，任何建议都不是发送授权或平台回执。</p><ul>' + ''.join(suggestion_items) + '</ul></details>'
    if drafts:
        bound = [d for d in drafts if d['draft_key'] in dispatches]
        drafts = [d for d in drafts if d['draft_key'] not in dispatches]
        labels = {'prepared': '已绑定，待授权', 'authorized': '已授权，待发送',
                  'unknown': '结果不明，冻结重发', 'verified': '历史回执已核验'}
        if bound:
            queue_items = ''.join('<li>' + e(PLATFORMS.get(d['platform'], d['platform'])) + ' · '
                + e(d['account']) + ' · ' + e(labels.get(dispatches[d['draft_key']]['state'],
                                                       dispatches[d['draft_key']]['state']))
                + (f'<p>{e(d["reply"])}</p>' if include_text else '') + '</li>' for d in bound)
            detail_section += '<details><summary>YouTube 回复发送队列（' + str(len(bound)) + ' 条）</summary><p>本地队列快照；历史核验不保证当前可见。结果不明须沿原计划只读恢复，不能重复发送。</p><ul>' + queue_items + '</ul></details>'
    if drafts:
        draft_items = ''.join(f'<li>{e(PLATFORMS.get(d["platform"],d["platform"]))} · {e(d["account"])}'
                              + (f'<p>{e(d["reply"])}</p>' if include_text else '') + '</li>' for d in drafts)
        detail_section += '<details><summary>本地回复草稿（' + str(len(drafts)) + ' 条；非发送队列）</summary><p>草稿不表示已授权或已发送；发送前用 interaction_drafts.py list 核对原评论变化及既有回复回执，再检查平台当前上下文。</p><ul>' + draft_items + '</ul></details>'
    stamp = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>互动收件箱</title><style>body{{font:14px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#163b3d;background:#f2f7f5;margin:0;padding:28px}}main{{max-width:1200px;margin:auto}}h1{{font-size:24px;margin:0 0 8px}}p.note{{color:#526d6d;line-height:1.6}}.box{{overflow-x:auto;background:white;border:1px solid #d8e7e1;border-radius:10px}}table{{width:100%;border-collapse:collapse;min-width:760px}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #e5efeb;vertical-align:top}}th{{color:#526d6d;font-weight:600}}td:last-child{{max-width:400px;overflow-wrap:anywhere}}details{{margin-top:20px}}li{{padding:10px;border-bottom:1px solid #d8e7e1}}li p{{white-space:pre-wrap}}li span{{color:#526d6d}}</style></head><body><main>
<h1>互动收件箱</h1><p class="note">本地快照 · 北京时间 {e(stamp)}。数字只代表已获取并保留的记录；检查不完整、失败或无同步记录时，不能判断平台没有新评论。</p>
<div class="box"><table><thead><tr><th>平台</th><th>账号</th><th>检查状态</th><th>待处理</th><th>已处理</th><th>最近成功读取／观察</th><th>覆盖情况与原因</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>{detail_section}
<p class="note">此页面不会实时更新，也不会发送回复。巡检运行状态来自生成时的本机锁与时间检查，不代表平台全量覆盖。评论及账号资料属于私有信息，请保存在私人位置。</p></main></body></html>'''


def main():
    parser = argparse.ArgumentParser(description='生成私有互动收件箱快照；不联网，不发送评论')
    parser.add_argument('--state-dir', required=True, help='互动收件箱私有状态目录')
    parser.add_argument('--output', required=True, help='新的私有 HTML 文件（不会覆盖已有文件）')
    parser.add_argument('--include-text', action='store_true', help='在页面展示待处理评论正文')
    parser.add_argument('--platform', action='append', choices=sorted(PLATFORMS), default=[], help='也显示尚无同步记录的平台，可重复指定')
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    skill_root = Path(__file__).resolve().parent.parent
    if output.is_relative_to(skill_root):
        parser.error('看板可能包含私有信息，输出路径不能位于 Skill 仓库内')
    page = render(Path(args.state_dir).expanduser() / 'interaction-inbox.sqlite3', args.include_text, args.platform)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        file.write(page)
    print('已生成本地快照；未联网、未发送回复。')


if __name__ == '__main__':
    main()
