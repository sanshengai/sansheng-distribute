#!/usr/bin/env python3
"""Append attributed, manual checks for one existing remote work; never publish."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3

from publish_core import HEX, PublishError, Store

FIELDS = {'cover': '封面', 'collection': '合集', 'declaration': '声明', 'topics': '话题'}
RESULTS = {'matched': '一致', 'mismatch': '不一致', 'unknown': '未确认'}


def _time(value):
    try:
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        value = value.astimezone(timezone.utc)
    except (AttributeError, ValueError, OverflowError):
        raise PublishError('观察时间必须为带时区的 ISO 时间') from None
    if value > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise PublishError('观察时间不能超过当前时间')
    return value


def record_component(store, key, field, result, remote_id, evidence, observed_at):
    """Persist a human-attributed observation, without modifying job state or grants."""
    if not isinstance(key, str) or not HEX.fullmatch(key):
        raise PublishError('任务 key 格式无效')
    if field not in FIELDS or result not in RESULTS:
        raise PublishError('未知分项或观察结果')
    if not isinstance(remote_id, str) or not remote_id.strip() or len(remote_id) > 500:
        raise PublishError('需要具体远端作品 ID')
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500:
        raise PublishError('需要不超过 500 字的观察来源与证据')
    observed = _time(observed_at)
    # An immediate transaction serializes the identity check and append. No
    # publishing lock is released and no existing job/event is modified.
    with store.tx():
        job = store.status(key)
        if not job.get('remote_id') or job['remote_id'] != remote_id:
            raise PublishError('远端作品 ID 与现有账本不一致，禁止记录')
        if job['state'] == 'prepared':
            raise PublishError('尚未提交的任务不可记录远端作品观察')
        store.db.execute('''CREATE TABLE IF NOT EXISTS component_observations (
            id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, remote_id TEXT NOT NULL,
            component TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT NOT NULL,
            observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
            FOREIGN KEY(job_key) REFERENCES jobs(key))''')
        last = store.db.execute('''SELECT observed_at FROM component_observations
            WHERE job_key=? AND remote_id=? AND component=? ORDER BY id DESC LIMIT 1''',
            (key, remote_id, field)).fetchone()
        if last and observed < _time(last['observed_at']):
            raise PublishError('不能用较旧观察覆盖最新结果')
        timestamp = observed.isoformat()
        store.db.execute('''INSERT INTO component_observations
            (job_key, remote_id, component, result, evidence, observed_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (key, remote_id, field, result, evidence.strip(), timestamp,
             datetime.now(timezone.utc).isoformat()))
    return {'job_key': key, 'component': field, 'result': result,
            'observed_at': timestamp, 'source': 'manual', 'remote_checked': False}


def component_snapshot(store, job):
    """Read only the latest manual observation of this same remote identity."""
    if not job.get('key') or not job.get('remote_id'):
        return {}
    table = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='component_observations'").fetchone()
    if not table:
        return {}
    rows = store.db.execute('''SELECT component, result, evidence, observed_at FROM component_observations
        WHERE job_key=? AND remote_id=? ORDER BY id''', (job['key'], job['remote_id']))
    latest = {}
    for row in rows:
        if row['component'] in FIELDS and row['result'] in RESULTS:
            latest[row['component']] = dict(row)
    return latest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir', required=True, help='已有私有账本目录')
    sub = p.add_subparsers(dest='action', required=True)
    record = sub.add_parser('record', help='记录人工观察；不连接平台、不修改发布状态')
    record.add_argument('--key', required=True)
    record.add_argument('--field', required=True, choices=sorted(FIELDS))
    record.add_argument('--result', required=True, choices=sorted(RESULTS))
    record.add_argument('--remote-id', required=True)
    record.add_argument('--evidence-file', required=True, help='UTF-8 私有证据文本文件；不会输出其内容')
    record.add_argument('--observed-at', required=True, help='带时区 ISO 时间')
    args = p.parse_args()
    store = None
    try:
        state = Path(args.state_dir).expanduser().resolve()
        if not (state / 'publish.sqlite3').is_file():
            raise PublishError('指定目录没有现有账本')
        path = Path(args.evidence_file).expanduser()
        if not path.is_file() or path.stat().st_size > 4000:
            raise PublishError('证据文件不存在或超过 4000 字节')
        evidence = path.read_text(encoding='utf-8')
        store = Store(state)
        value = record_component(store, args.key, args.field, args.result, args.remote_id, evidence, args.observed_at)
        print(json.dumps({'ok': True, 'result': value}, ensure_ascii=False))
        return 0
    except (PublishError, OSError, sqlite3.Error, UnicodeError) as exc:
        # Never display file paths, evidence, IDs or raw database errors.
        message = str(exc) if isinstance(exc, PublishError) else '读取或写入私人账本失败'
        print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False))
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
