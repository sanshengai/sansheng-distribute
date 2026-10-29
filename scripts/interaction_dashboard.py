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


def render(db_path, include_text=False, requested=()):
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
        has_observations = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_observations'").fetchone()
        observations = [json.loads(r[0]) for r in db.execute('SELECT document FROM interaction_observations ORDER BY observed_at, recorded_at')] if has_observations else []
        has_drafts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_drafts'").fetchone()
        drafts = [dict(r) for r in db.execute('SELECT * FROM interaction_drafts ORDER BY created_at')] if has_drafts else []
        has_dispatches = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_dispatches'").fetchone()
        dispatches = {r['draft_key']: dict(r) for r in db.execute('SELECT * FROM interaction_dispatches')} if has_dispatches else {}
    by_source = {}
    for item in items:
        key = (item['platform'], item['account'], item['channel_id'])
        by_source.setdefault(key, []).append(item)
    rows = []
    details = []
    seen_platforms = set()
    for src in sorted(sources, key=lambda x: (x['platform'], x['account'], x['channel_id'])):
        platform = src['platform']
        seen_platforms.add(platform)
        key = (platform, src['account'], src['channel_id'])
        incoming = [x for x in by_source.get(key, []) if not x['is_own']]
        verified = [x for x in incoming if (*key, x['comment_id']) in replied]
        pending = [x for x in incoming if x['local_status'] == 'new' and (*key, x['comment_id']) not in replied]
        handled = [x for x in incoming if x['local_status'] == 'handled']
        unknown = sum(x['local_status'] not in ('new', 'handled') for x in incoming)
        manual_count = sum((*key, x['comment_id']) in manual for x in incoming)
        if src['state'] == 'manual':
            coverage = '人工导入，未进行平台同步'
        elif platform == 'youtube':
            coverage = f"评论串 {src['parents_scanned']}/{src['parents_total']}；回复未完成 {src['reply_incomplete']}"
        else:
            coverage = '覆盖范围以平台同步记录为准'
        note = coverage + (('；' + str(src['error'])) if src.get('error') and src['error'] != coverage else '')
        note += f'；平台已核验回复 {len(verified)} 条（历史回执）'
        if manual_count:
            note += f'；含人工导入 {manual_count} 条'
        if unknown:
            note += f'；其他状态 {unknown}'
        rows.append(f"<tr><td>{e(PLATFORMS.get(platform, platform))}</td><td>{e(src['account'])}</td>"
                    f"<td>{e(STATES.get(src['state'], '状态未知'))}</td><td>{len(pending)}</td>"
                    f"<td>{len(handled)}</td><td>{e(local_time(src['last_success_at']))}</td><td>{e(note)}</td></tr>")
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
    # Separate evidence lane: never count browser observations as API inbox items.
    latest = {}
    for observation in observations:
        latest[(observation['platform'], observation['account'])] = observation
    labels = {'observed': '网页已检查', 'partial': '网页部分检查', 'blocked': '受阻', 'paused': '暂停', 'unavailable': '当前不可用'}
    for (platform, account), observation in sorted(latest.items()):
        seen_platforms.add(platform)
        note = '范围：' + observation['scope'] + '；原因：' + observation['reason'] + '；下一步：' + observation['next_action']
        rows.append(f"<tr><td>{e(PLATFORMS.get(platform, platform))}</td><td>{e(account)}</td>"
                    f"<td>{e(labels[observation['state']])}</td><td>未统计</td><td>另见网页记录</td>"
                    f"<td>{e(local_time(observation['observed_at']))}（观察）</td><td>{e(note)}</td></tr>")
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
    if observation_details:
        detail_section += '<details><summary>查看网页回复历史（不计入自动同步）</summary><ul>' + ''.join(observation_details) + '</ul></details>'
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
<div class="box"><table><thead><tr><th>平台</th><th>账号</th><th>检查状态</th><th>待处理</th><th>已处理</th><th>最近完整同步／网页观察</th><th>覆盖情况与原因</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>{detail_section}
<p class="note">此页面不会实时更新，也不会发送回复。评论及账号资料属于私有信息，请保存在私人位置。</p></main></body></html>'''


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
