#!/usr/bin/env python3
"""Discover Weibo comments through the bounded official reader, without inventing threads."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from interaction_import import _safe_state_dir
from interaction_inbox import _lock, open_db
from publish_core import PublishError, now
from weibo_comments_capture import _created, _id, _iso, _private_json, _text, plain_text
from weibo_official_probe import run as probe, validate_config as validate_probe

SCHEMA = '''CREATE TABLE IF NOT EXISTS weibo_official_sources (
 account TEXT PRIMARY KEY, uid TEXT UNIQUE NOT NULL, not_before TEXT NOT NULL,
 state TEXT NOT NULL, started_at TEXT NOT NULL, checked_at TEXT, last_success_at TEXT,
 evidence TEXT, returned_rows INTEGER, reported_counts TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS weibo_official_items (
 account TEXT NOT NULL, uid TEXT NOT NULL, comment_id TEXT NOT NULL,
 work_id TEXT NOT NULL, author_id TEXT NOT NULL, author_name TEXT NOT NULL,
 published_at TEXT NOT NULL, is_own INTEGER NOT NULL, baseline INTEGER NOT NULL,
 views_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 PRIMARY KEY(account,uid,comment_id));'''


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) != {
        'schema', 'enabled', 'account', 'uid', 'credentials', 'work_ids', 'not_before'
    } or raw['schema'] != 'weibo-official-inbox.v1':
        raise PublishError('微博官方发现配置无效')
    candidate = {k: v for k, v in raw.items() if k != 'not_before'}
    candidate['schema'] = 'weibo-official-probe.v1'
    validate_probe(candidate)
    cutoff = _iso(raw['not_before'])
    if cutoff > datetime.now(timezone.utc):
        raise PublishError('监测起点不能在未来；历史留言不能靠改起点重新变成新留言')
    return {**raw, 'not_before': cutoff.isoformat()}


def probe_config(config):
    return {**{k: v for k, v in config.items() if k != 'not_before'},
            'schema': 'weibo-official-probe.v1'}


def normalize(result, config):
    """Keep endpoint representations, not a guessed canonical body or thread."""
    if not isinstance(result, dict) or result.get('schema') != 'weibo-official-probe-result.v1':
        raise PublishError('不是微博官方只读试验证据')
    if (result.get('state') != 'read_probe_complete' or
        result.get('account') != config['account'] or result.get('uid') != config['uid'] or
        type(result.get('social_writes')) is not int or result['social_writes'] != 0 or
        result.get('automatic_sync') is not False):
        raise PublishError('证据身份、只读状态或完成状态不符')
    started, finished = _iso(result.get('started_at')), _iso(result.get('finished_at'))
    if finished < started or finished > datetime.now(timezone.utc):
        raise PublishError('证据时间顺序无效')
    requests = result.get('requests')
    expected = [('received', None), *(('work', x) for x in config['work_ids'])]
    if not isinstance(requests, list) or len(requests) != len(expected):
        raise PublishError('读取范围与配置不符，不推断空结果')
    items, counts, row_count = {}, [], 0
    for request, identity in zip(requests, expected):
        if not isinstance(request, dict) or (request.get('kind'), request.get('work_id')) != identity:
            raise PublishError('评论接口或作品范围不符')
        comments, count = request.get('comments'), request.get('count')
        if not isinstance(comments, list) or len(comments) > 1000 or type(count) is not int or count < 0:
            raise PublishError('评论列表或计数无效')
        counts.append({'kind': identity[0], 'work_id': identity[1], 'reported': count,
                       'returned': len(comments)})
        seen = set()
        for row in comments:
            if not isinstance(row, dict) or not isinstance(row.get('user'), dict):
                raise PublishError('评论或作者字段无效')
            cid, author = _id(row.get('id')), _id(row['user'].get('id'))
            if cid in seen:
                raise PublishError('同一接口重复评论 ID，停止核对')
            seen.add(cid)
            link = urlsplit(_text(row.get('postJumpLink'), 1000))
            parts = link.path.strip('/').split('/')
            if (link.scheme != 'https' or link.netloc not in {'weibo.com', 'www.weibo.com'} or
                link.query or link.fragment or len(parts) != 2 or parts[0] != config['uid']):
                raise PublishError('博文链接不属于当前本人账号或格式无效')
            work = _id(parts[1])
            if identity[0] == 'work' and work != identity[1]:
                raise PublishError('指定作品接口返回其他作品，不改绑')
            published = _created(row.get('createdAt'))
            if _iso(published) > finished:
                raise PublishError('评论时间晚于实际读取时间')
            value = {'comment_id': cid, 'work_id': work, 'author_id': author,
                     'author_name': _text(row['user'].get('name'), 256),
                     'published_at': published, 'is_own': int(author == config['uid']),
                     'baseline': int(_iso(published) < _iso(config['not_before'])), 'views': {}}
            old = items.get(cid)
            if old and any(old[k] != value[k] for k in ('work_id', 'author_id', 'published_at')):
                raise PublishError('同评论 ID 的作品、作者或时间冲突，不改绑')
            if old:
                value = old
            # mid is deliberately unused: real samples contain the comment ID there.
            value['views'][identity[0]] = {'body': _text(plain_text(_text(row.get('text')))),
                                         'observed_at': finished.isoformat()}
            items[cid] = value
            row_count += 1
    return {'started_at': started.isoformat(), 'finished_at': finished.isoformat(),
            'items': list(items.values()), 'returned_rows': row_count, 'reported_counts': counts}


def _bind(db, config, stamp):
    db.executescript(SCHEMA)
    bindings = db.execute('SELECT * FROM weibo_official_sources WHERE account=? OR uid=?',
                          (config['account'], config['uid'])).fetchall()
    if any(r['account'] != config['account'] or r['uid'] != config['uid'] or
           r['not_before'] != config['not_before'] for r in bindings):
        raise PublishError('账号、UID 或历史起点与现有发现记录冲突，不重建基线')
    db.execute('''INSERT INTO weibo_official_sources(account,uid,not_before,state,started_at)
        VALUES (?,?,?,'running',?) ON CONFLICT(account) DO NOTHING''',
        (config['account'], config['uid'], config['not_before'], stamp))


def ingest(root, config, result, evidence):
    config = validate_config(config)
    data = normalize(result, config)
    root = _safe_state_dir(root)
    new = 0
    with _lock(root), open_db(root) as db:
        _bind(db, config, data['started_at'])
        source = db.execute('SELECT * FROM weibo_official_sources WHERE account=?', (config['account'],)).fetchone()
        if source['checked_at'] and _iso(source['checked_at']) > _iso(data['finished_at']):
            raise PublishError('旧证据不能覆盖较新的读取状态')
        for row in data['items']:
            key = (config['account'], config['uid'], row['comment_id'])
            canonical = db.execute('''SELECT * FROM interaction_items WHERE platform='weibo'
                AND account=? AND channel_id=? AND comment_id=?''', key).fetchone()
            if canonical and (canonical['video_id'] != row['work_id'] or
                              canonical['author_channel'] != row['author_id'] or
                              canonical['published_at'] != row['published_at']):
                raise PublishError('与原生收件箱身份冲突，不覆盖原文或关系')
            old = db.execute('SELECT * FROM weibo_official_items WHERE account=? AND uid=? AND comment_id=?', key).fetchone()
            if old and any(old[k] != row[k] for k in ('work_id', 'author_id', 'published_at')):
                raise PublishError('已有评论身份冲突，不改绑')
            views = json.loads(old['views_json']) if old else {}
            views.update(row['views'])
            new += int(old is None)
            db.execute('''INSERT INTO weibo_official_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,uid,comment_id) DO UPDATE SET
                author_name=excluded.author_name,views_json=excluded.views_json,last_seen_at=excluded.last_seen_at''',
                (*key, row['work_id'], row['author_id'], row['author_name'], row['published_at'],
                 row['is_own'], row['baseline'], json.dumps(views, ensure_ascii=False),
                 data['finished_at'], data['finished_at']))
        db.execute('''UPDATE weibo_official_sources SET state='partial',started_at=?,checked_at=?,
            last_success_at=?,evidence=?,returned_rows=?,reported_counts=?,error=NULL WHERE account=?''',
            (data['started_at'], data['finished_at'], data['finished_at'], str(evidence),
             data['returned_rows'], json.dumps(data['reported_counts']), config['account']))
    return {'state': 'partial', 'returned_rows': data['returned_rows'],
            'distinct_comments': len(data['items']), 'new_local_candidates': new,
            'canonical_imports': 0, 'social_writes': 0, 'evidence': str(evidence),
            'coverage': '仅官方有界返回；父级、分页及当前可回复性待补查'}


@contextmanager
def _reader_lock(root, uid):
    fd = os.open(root / f'.weibo-official-{uid}.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishError('同一状态目录的微博 UID 已有只读发现任务') from None
        yield
    finally:
        os.close(fd)


def sync(root, config, client=None):
    config = validate_config(config)
    root = _safe_state_dir(root)
    with _reader_lock(root, config['uid']):
        stamp = now()
        with _lock(root), open_db(root) as db:
            _bind(db, config, stamp)
            db.execute("UPDATE weibo_official_sources SET state='running',started_at=?,error=NULL WHERE account=?",
                       (stamp, config['account']))
        try:
            receipt = probe(root / 'weibo-official-probes', probe_config(config), client=client)
            result = _private_json(receipt['evidence'])
            return ingest(root, config, result, receipt['evidence'])
        except (PublishError, OSError, ValueError, KeyError):
            with _lock(root), open_db(root) as db:
                db.execute("UPDATE weibo_official_sources SET state='error',checked_at=?,error=? WHERE account=?",
                           (now(), '官方读取或校验失败；未重试，不代表零评论', config['account']))
            raise PublishError('微博官方发现未完成；已保留旧记录及失败状态，不重试') from None


def overview(db, include_text=False):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='weibo_official_sources'").fetchone():
        return []
    values = []
    for source in db.execute('SELECT * FROM weibo_official_sources ORDER BY account'):
        value = dict(source)
        rows = [dict(r) for r in db.execute('''SELECT o.*,
            EXISTS(SELECT 1 FROM interaction_items i WHERE i.platform='weibo' AND
              i.account=o.account AND i.channel_id=o.uid AND i.comment_id=o.comment_id AND
              i.video_id=o.work_id AND i.author_channel=o.author_id) AS known_inbox
            FROM weibo_official_items o WHERE o.account=? AND o.uid=?''', (source['account'], source['uid']))]
        pending = [r for r in rows if not r['is_own'] and not r['baseline'] and not r['known_inbox']]
        value.update(needs_context=len(pending), historical=sum(not r['is_own'] and r['baseline'] for r in rows),
                     own=sum(r['is_own'] for r in rows), unique_seen=len(rows),
                     representation_differences=sum(len({v['body'] for v in json.loads(r['views_json']).values()}) > 1 for r in rows),
                     pending=pending if include_text else [])
        values.append(value)
    return values


def main():
    p = argparse.ArgumentParser(description='微博官方只读发现；缺少父级的留言不进入发送队列')
    p.add_argument('--state-dir', required=True)
    p.add_argument('--config', required=True)
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('sync')
    sub.add_parser('ingest').add_argument('--evidence', required=True)
    args = p.parse_args()
    try:
        config = _private_json(args.config)
        if args.action == 'sync':
            result = sync(args.state_dir, config)
        else:
            result = ingest(args.state_dir, config, _private_json(args.evidence), args.evidence)
        print(json.dumps(result, ensure_ascii=False))
    except (PublishError, OSError, ValueError, KeyError, TypeError):
        print('微博官方发现失败：配置、权限、读取或身份校验未通过；未发送，详情保留在私有状态。', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
