#!/usr/bin/env python3
"""Read-only cross-task video reuse candidates from one local publish ledger.

A result is an indication to inspect the original task, not a remote state or a
permission to publish. The database and old snapshots are never modified.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from publish_core import PublishError, Store, digest, read_json, target_of, validate


def _videos(manifest, target):
    if target.get('content_type') != 'video':
        raise PublishError('仅支持视频目标的重复候选查询')
    ids = target.get('asset_ids', [])
    assets = {a['id']: a for a in manifest.get('assets', [])}
    videos = [assets[i] for i in ids if i in assets and assets[i].get('kind') == 'video']
    if not videos:
        raise PublishError('视频目标没有登记视频素材')
    return {asset['sha256'] for asset in videos}


def _category(state):
    if state in {'submission_unknown', 'pending', 'blocked'}:
        return '需核验'
    if state in {'published', 'broadcasted'}:
        return '已记录发布'
    return '其他状态'


def find_candidates(manifest, target_id, state_dir):
    # Validate the selected incoming file against its sealed digest. A changed
    # or missing input cannot be compared as if it were the original bytes.
    validate(manifest)
    target = target_of(manifest, target_id)
    hashes = _videos(manifest, target)
    current_key = Store.key(None, manifest, target)
    state = Path(state_dir).expanduser().resolve()
    db_path = state / 'publish.sqlite3'
    if not db_path.is_file():
        raise PublishError('账本不存在；没有查询到历史记录，不等于未发布')
    try:
        db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                'SELECT key,manifest_digest,target_id,platform,account,endpoint,state,updated_at '
                'FROM jobs WHERE platform=? AND account=? ORDER BY updated_at DESC',
                (target['platform'], target['account'])).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise PublishError('无法只读查询账本；不能推断历史状态') from exc

    matches, inaccessible = [], 0
    for row in rows:
        job = dict(row)
        key = job['key']
        # A database may be damaged or edited; never let a key escape snapshots.
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            inaccessible += 1
            continue
        try:
            snapshot = read_json(state / 'snapshots' / (key + '.json'))
            old = target_of(snapshot, job['target_id'])
            if (digest(snapshot) != job['manifest_digest'] or Store.key(None, snapshot, old) != key
                    or any(old.get(field) != job[field] for field in ('platform', 'account', 'endpoint'))):
                raise ValueError('snapshot identity mismatch')
            if old.get('content_type') != 'video':
                continue
            old_hashes = _videos(snapshot, old)
            if not all(isinstance(h, str) and len(h) == 64 and all(c in '0123456789abcdef' for c in h)
                       for h in old_hashes):
                raise ValueError('invalid sealed video hash')
        except (OSError, ValueError, KeyError, TypeError, PublishError):
            inaccessible += 1
            continue
        shared = hashes & old_hashes
        if shared:
            matches.append({
                'job_key': key, 'target_id': job['target_id'], 'state': job['state'],
                'category': _category(job['state']), 'updated_at': job['updated_at'],
                'same_task': key == current_key, 'matching_video_count': len(shared),
            })
    return {
        'platform': target['platform'], 'account': target['account'],
        'candidates': matches, 'candidate_count': len(matches),
        'unavailable_snapshot_count': inaccessible,
        'coverage_complete_for_local_rows': inaccessible == 0,
        'notice': ('仅为同平台、同本地账号标识和视频字节的历史候选；非平台实时查询，'
                   '缺失或不匹配快照不可判定，候选为零不代表从未发布。结果不明先核验原任务，'
                   '不能因本查询自动重发；作者有意重新发布仍须结合具体授权和平台状态判断。'),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, help='已封存的当前交接清单')
    parser.add_argument('--target', required=True, help='目标 ID')
    parser.add_argument('--state-dir', required=True, help='既有私有账本目录')
    args = parser.parse_args()
    try:
        result = find_candidates(read_json(args.manifest), args.target, args.state_dir)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2))
        return 0
    except (PublishError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
