#!/usr/bin/env python3
"""Import explicitly supplied observations into a private inbox; no network."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from interaction_inbox import _lock, _private_dir, open_db
from publish_core import PublishError, now

PLATFORMS = {'youtube', 'x', 'weibo', 'bilibili', 'douyin', 'wechat',
             'wechat_channels', 'alipay', 'xiaohongshu'}
TOP_FIELDS = {'platform', 'account', 'channel_id', 'items'}
ITEM_FIELDS = {'comment_id', 'video_id', 'thread_id', 'parent_id', 'author_name',
               'author_channel', 'body', 'published_at', 'updated_at', 'is_own'}


def _safe_state_dir(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise PublishError('评论状态目录须为本机绝对路径')
    if path.resolve().is_relative_to(Path(__file__).resolve().parent.parent):
        raise PublishError('评论状态目录不能位于 Skill 仓库内')
    return _private_dir(path)


def _string(value, field, max_length=1000, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length or '\x00' in value:
        raise PublishError(f'{field} 须为非空文字且长度合法')
    return value


def validate(raw):
    if not isinstance(raw, dict) or set(raw) != TOP_FIELDS:
        raise PublishError('导入文件须含 platform、account、channel_id 和 items 四个字段')
    platform = _string(raw['platform'], 'platform', 40)
    if platform not in PLATFORMS:
        raise PublishError('不支持的平台')
    account = _string(raw['account'], 'account', 256)
    channel = _string(raw['channel_id'], 'channel_id', 256)
    items = raw['items']
    if not isinstance(items, list) or not items or len(items) > 1000:
        raise PublishError('items 须为 1 至 1000 项的列表；空输入不表示平台没有评论')
    normalized = []
    seen = set()
    for row in items:
        if not isinstance(row, dict) or set(row) - ITEM_FIELDS or not {'comment_id', 'video_id', 'body', 'is_own'} <= set(row):
            raise PublishError('每条评论须包含作品 ID、评论 ID、正文和 is_own')
        comment = _string(row['comment_id'], 'comment_id', 256)
        video = _string(row['video_id'], 'video_id', 256)
        body = _string(row['body'], 'body', 20000)
        if not isinstance(row['is_own'], bool):
            raise PublishError('is_own 须明确为 true 或 false')
        if comment in seen:
            raise PublishError('同一文件含重复评论 ID')
        seen.add(comment)
        record = {'comment_id': comment, 'video_id': video, 'body': body, 'is_own': int(row['is_own'])}
        for field in ('thread_id', 'parent_id', 'author_name', 'author_channel', 'published_at', 'updated_at'):
            record[field] = _string(row.get(field), field, 1000, required=False)
        normalized.append(record)
    return platform, account, channel, normalized


def import_manual(root, document, *, baseline_before=None):
    platform, account, channel, items = validate(document)
    cutoff = None
    if baseline_before is not None:
        try:
            cutoff = datetime.fromisoformat(baseline_before.replace('Z', '+00:00'))
            if cutoff.tzinfo is None:
                raise ValueError()
            cutoff = cutoff.astimezone(timezone.utc)
            for item in items:
                published = datetime.fromisoformat(item['published_at'].replace('Z', '+00:00'))
                if published.tzinfo is None:
                    raise ValueError()
                item['baseline'] = not item['is_own'] and published < cutoff
        except (TypeError, AttributeError, ValueError):
            raise PublishError('历史基线需要带时区的截止时间及每条评论时间') from None
    root = _safe_state_dir(root)
    stamp = now()
    imported = skipped = 0
    with _lock(root), open_db(root) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS interaction_manual_provenance (
            platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
            comment_id TEXT NOT NULL, first_imported_at TEXT NOT NULL, last_imported_at TEXT NOT NULL,
            PRIMARY KEY(platform,account,channel_id,comment_id))''')
        if cutoff is not None:
            db.execute('''CREATE TABLE IF NOT EXISTS interaction_baselines (
                platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
                comment_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
                PRIMARY KEY(platform,account,channel_id,comment_id))''')
        for row in items:
            key = (platform, account, channel, row['comment_id'])
            old = db.execute('''SELECT video_id,body,is_own,thread_id,parent_id,author_channel,published_at
                FROM interaction_items
                WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''', key).fetchone()
            if old:
                if any(old[field] != (row[field] or '' if field == 'thread_id' else row[field])
                       for field in ('video_id', 'body', 'is_own', 'thread_id', 'parent_id',
                                     'author_channel', 'published_at')):
                    raise PublishError('已有同 ID 评论内容不同；请核对原记录，不覆盖')
                skipped += 1
                db.execute('''UPDATE interaction_manual_provenance SET last_imported_at=?
                    WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''', (stamp, *key))
                continue
            db.execute('''INSERT INTO interaction_items
                (platform,account,channel_id,comment_id,thread_id,parent_id,video_id,
                 author_channel,author_name,body,published_at,updated_at,first_seen_at,last_seen_at,is_own)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (*key, row['thread_id'] or '', row['parent_id'], row['video_id'], row['author_channel'],
                 row['author_name'], row['body'], row['published_at'], row['updated_at'], stamp, stamp, row['is_own']))
            db.execute('''INSERT INTO interaction_manual_provenance
                (platform,account,channel_id,comment_id,first_imported_at,last_imported_at)
                VALUES(?,?,?,?,?,?)''', (*key, stamp, stamp))
            if row.get('baseline'):
                db.execute('INSERT INTO interaction_baselines VALUES (?,?,?,?,?)', (*key, stamp))
                db.execute('''UPDATE interaction_items SET local_status='handled'
                    WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''', key)
            imported += 1
        if items:
            db.execute('''INSERT INTO interaction_sources(platform,account,channel_id,state,started_at,finished_at,error)
                VALUES (?,?,?,'manual',?,?,'人工导入，未进行平台同步')
                ON CONFLICT(platform,account,channel_id) DO NOTHING''', (platform, account, channel, stamp, stamp))
    return {'state': 'manual_import', 'imported': imported, 'already_known': skipped,
            'coverage': '未知，未进行平台同步'}


def mark_manual(root, platform, account, channel, comment_id, status):
    platform = _string(platform, 'platform', 40)
    if platform not in PLATFORMS or status not in {'new', 'handled'}:
        raise PublishError('平台或状态无效')
    account = _string(account, 'account', 256)
    channel = _string(channel, 'channel_id', 256)
    comment_id = _string(comment_id, 'comment_id', 256)
    root = _safe_state_dir(root)
    with _lock(root), open_db(root) as db:
        try:
            provenance = db.execute('''SELECT 1 FROM interaction_manual_provenance
                WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''',
                (platform, account, channel, comment_id)).fetchone()
        except sqlite3.OperationalError:
            provenance = None
        if not provenance:
            raise PublishError('该评论没有人工导入记录')
        result = db.execute('''UPDATE interaction_items SET local_status=?
            WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''',
            (status, platform, account, channel, comment_id))
        if result.rowcount != 1:
            raise PublishError('找不到该评论')
    return {'state': 'updated', 'status': status, 'note': '仅更新本地处理标记，不代表已回复'}


def main():
    parser = argparse.ArgumentParser(description='将人工收集的评论导入私有收件箱；不联网或回复')
    parser.add_argument('--state-dir', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    importer = sub.add_parser('import')
    importer.add_argument('--input', required=True, help='权限为 0600 的本地 JSON')
    marker = sub.add_parser('mark')
    marker.add_argument('--platform', required=True)
    marker.add_argument('--account', required=True)
    marker.add_argument('--channel-id', required=True)
    marker.add_argument('--comment-id', required=True)
    marker.add_argument('--status', required=True, choices=['new', 'handled'])
    args = parser.parse_args()
    try:
        if args.action == 'mark':
            print(json.dumps(mark_manual(args.state_dir, args.platform, args.account, args.channel_id,
                                         args.comment_id, args.status), ensure_ascii=False))
            return
        path = Path(args.input).expanduser()
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise PublishError('输入 JSON 不可为符号链接，文件权限须为 0600')
        if path.stat().st_size > 2 * 1024 * 1024:
            raise PublishError('导入 JSON 不得超过 2 MiB')
        with path.open(encoding='utf-8') as file:
            document = json.load(file)
        print(json.dumps(import_manual(args.state_dir, document), ensure_ascii=False))
    except (PublishError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, PublishError) else '读取或写入失败；请检查文件和格式'}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
