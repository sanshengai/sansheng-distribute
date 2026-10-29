#!/usr/bin/env python3
"""Append private browser observations. These are never remote comment identities."""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from interaction_import import PLATFORMS, _safe_state_dir, _string
from interaction_inbox import _lock, open_db
from publish_core import PublishError, now

FIELDS = {'platform', 'account', 'observed_at', 'state', 'scope', 'reason', 'next_action', 'evidence', 'replies'}
REPLY_FIELDS = {'author', 'comment', 'work', 'reply', 'verification'}
STATES = {'observed', 'partial', 'blocked', 'paused', 'unavailable'}
SCHEMA = '''CREATE TABLE IF NOT EXISTS interaction_observations (
 observation_key TEXT PRIMARY KEY, platform TEXT NOT NULL, account TEXT NOT NULL,
 observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL, document TEXT NOT NULL);'''


def validate(raw):
    if not isinstance(raw, dict) or set(raw) != FIELDS:
        raise PublishError('观察记录字段不完整或含未知字段')
    result = {k: _string(raw[k], k, 10000) for k in FIELDS - {'replies'}}
    if result['platform'] not in PLATFORMS or result['state'] not in STATES:
        raise PublishError('平台或观察状态不受支持')
    try:
        stamp = datetime.fromisoformat(result['observed_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
            raise ValueError()
        result['observed_at'] = stamp.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise PublishError('观察时间须带时区且不能在未来')
    replies = raw['replies']
    if not isinstance(replies, list) or len(replies) > 100:
        raise PublishError('replies 须为最多 100 条的列表')
    result['replies'] = []
    for row in replies:
        if not isinstance(row, dict) or set(row) != REPLY_FIELDS:
            raise PublishError('网页回复须有原作者、原文、作品、实际回复及读回证据')
        result['replies'].append({k: _string(row[k], k, 20000) for k in REPLY_FIELDS})
    if replies and result['state'] not in {'observed', 'partial'}:
        raise PublishError('阻塞或暂停记录不能包含本次成功回复')
    return result


def record(root, raw):
    doc = validate(raw)
    root = _safe_state_dir(root)
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    # Deduplicates identical observations only, never identifies a remote comment.
    key = hashlib.sha256(payload.encode()).hexdigest()
    with _lock(root), open_db(root) as db:
        db.execute(SCHEMA)
        cursor = db.execute('INSERT OR IGNORE INTO interaction_observations VALUES (?,?,?,?,?,?)',
                            (key, doc['platform'], doc['account'], doc['observed_at'], now(), payload))
        inserted = cursor.rowcount == 1
    return {'observation_key': key, 'inserted': inserted, 'remote_identity': False}


def main():
    p = argparse.ArgumentParser(description='记录网页检查及人工读回证据；不联网、不发送、不创建评论ID')
    p.add_argument('--state-dir', required=True)
    p.add_argument('--input', required=True)
    args = p.parse_args()
    try:
        source = Path(args.input)
        if source.stat().st_mode & 0o077:
            raise PublishError('含评论的输入文件权限须为 0600')
        print(json.dumps(record(args.state_dir, json.loads(source.read_text())), ensure_ascii=False))
    except (PublishError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
