#!/usr/bin/env python3
"""Private, read-only interaction inbox. Nothing here sends a comment."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from environment import EnvironmentError, load_profile
from publish_core import PublishError, now
from youtube_adapter import APIClient, config, identity
from youtube_comments_candidate import SCOPE, _account_channel, _private_dir, replies, threads

SCHEMA = '''
CREATE TABLE IF NOT EXISTS interaction_items (
 platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
 comment_id TEXT NOT NULL, thread_id TEXT NOT NULL, parent_id TEXT, video_id TEXT,
 author_channel TEXT, author_name TEXT, body TEXT, published_at TEXT, updated_at TEXT,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, is_own INTEGER NOT NULL,
 local_status TEXT NOT NULL DEFAULT 'new' CHECK(local_status IN ('new','handled')),
 PRIMARY KEY(platform,account,channel_id,comment_id)
);
CREATE TABLE IF NOT EXISTS interaction_sources (
 platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
 state TEXT NOT NULL, started_at TEXT, finished_at TEXT, last_success_at TEXT,
 error TEXT, thread_pages INTEGER NOT NULL DEFAULT 0, threads_complete INTEGER NOT NULL DEFAULT 0,
 parents_scanned INTEGER NOT NULL DEFAULT 0, parents_total INTEGER NOT NULL DEFAULT 0,
 reply_pages INTEGER NOT NULL DEFAULT 0, reply_incomplete INTEGER NOT NULL DEFAULT 0,
 next_thread_token TEXT,
 PRIMARY KEY(platform,account,channel_id)
);
CREATE TABLE IF NOT EXISTS interaction_parents (
 platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
 parent_id TEXT NOT NULL, thread_id TEXT NOT NULL, video_id TEXT,
 last_complete_scan_at TEXT, last_attempt_at TEXT, next_page_token TEXT,
 PRIMARY KEY(platform,account,channel_id,parent_id)
);
'''


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def open_db(root):
    path = root / 'interaction-inbox.sqlite3'
    if path.is_symlink():
        raise PublishError('互动账本不能是符号链接')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise PublishError('互动账本权限须为 0600')
    finally:
        os.close(fd)
    db = sqlite3.connect(path, timeout=30, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # Existing pre-release inboxes may not have the cursor/rotation fields.
    for table, columns in {
        'interaction_sources': {'next_thread_token':'TEXT'},
        'interaction_parents': {'next_page_token':'TEXT','last_attempt_at':'TEXT'},
    }.items():
        existing = {row[1] for row in db.execute('PRAGMA table_info('+table+')')}
        for name, kind in columns.items():
            if name not in existing:
                db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)
    db.commit()
    return db


def _lock(root):
    path = root / 'interaction-inbox.lock'
    if path.is_symlink():
        raise PublishError('互动锁不能是符号链接')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    file = os.fdopen(fd, 'r+')
    fcntl.flock(file, fcntl.LOCK_EX)
    return file


def _nonempty(value):
    return isinstance(value, str) and 0 < len(value) <= 10000


def _comment(item, *, thread_id, parent_id, video_id, channel):
    if not isinstance(item, dict) or not _nonempty(item.get('id')) or not isinstance(item.get('snippet'), dict):
        raise PublishError('YouTube 评论数据不完整')
    snippet = item['snippet']
    if parent_id and snippet.get('parentId') != parent_id:
        raise PublishError('YouTube 子回复父级不匹配')
    author = snippet.get('authorChannelId')
    author_channel = author.get('value') if isinstance(author, dict) else None
    text = snippet.get('textDisplay')
    if not isinstance(text, str):
        raise PublishError('YouTube 评论正文缺失')
    return (item['id'], thread_id, parent_id, video_id, author_channel,
            snippet.get('authorDisplayName'), text, snippet.get('publishedAt'),
            snippet.get('updatedAt'), int(author_channel == channel))


def _pages(fetch, limit, initial_token=None):
    token = initial_token
    seen = {initial_token} if initial_token else set()
    items = []
    count = 0
    while count < limit:
        result = fetch(token)
        count += 1
        items.extend(result['items'])
        token = result.get('next_page_token')
        if not token:
            return items, count, True, None
        if token in seen:
            raise PublishError('YouTube 返回循环分页令牌')
        seen.add(token)
    return items, count, False, token


def _upsert(db, account, channel, item, stamp):
    db.execute('''INSERT INTO interaction_items
        (platform,account,channel_id,comment_id,thread_id,parent_id,video_id,
         author_channel,author_name,body,published_at,updated_at,first_seen_at,last_seen_at,is_own)
        VALUES ('youtube',?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform,account,channel_id,comment_id) DO UPDATE SET
          thread_id=excluded.thread_id,parent_id=excluded.parent_id,video_id=excluded.video_id,
          author_channel=excluded.author_channel,author_name=excluded.author_name,body=excluded.body,
          published_at=excluded.published_at,updated_at=excluded.updated_at,
          last_seen_at=excluded.last_seen_at,is_own=excluded.is_own''',
        (account, channel, *item[:-1], stamp, stamp, item[-1]))


def _save_source(db, account, channel, *, state, started, error=None, thread_pages=0,
                 threads_complete=False, parents_scanned=0, parents_total=0,
                 reply_pages=0, reply_incomplete=0, next_thread_token=None, update_cursor=False):
    finished = now()
    db.execute('''INSERT INTO interaction_sources
        (platform,account,channel_id,state,started_at,finished_at,last_success_at,error,
         thread_pages,threads_complete,parents_scanned,parents_total,reply_pages,reply_incomplete)
        VALUES ('youtube',?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform,account,channel_id) DO UPDATE SET state=excluded.state,
         started_at=excluded.started_at,finished_at=excluded.finished_at,
         last_success_at=COALESCE(excluded.last_success_at,interaction_sources.last_success_at),
         error=excluded.error,thread_pages=excluded.thread_pages,threads_complete=excluded.threads_complete,
         parents_scanned=excluded.parents_scanned,parents_total=excluded.parents_total,
         reply_pages=excluded.reply_pages,reply_incomplete=excluded.reply_incomplete''',
        (account, channel, state, started, finished, finished if state == 'complete' else None,
         error, thread_pages, int(threads_complete), parents_scanned, parents_total, reply_pages, reply_incomplete))
    if update_cursor:
        db.execute('''UPDATE interaction_sources SET next_thread_token=? WHERE platform='youtube'
            AND account=? AND channel_id=?''', (next_thread_token,account,channel))
    db.commit()


def sync_youtube(client, root, account, channel, *, max_thread_pages=20, max_parents=20, max_reply_pages=10):
    """Pull one bounded cycle; rotate through all known parents and never delete unseen data."""
    if not 2 <= max_thread_pages <= 100 or not 1 <= max_parents <= 1000 or not 2 <= max_reply_pages <= 100:
        raise PublishError('同步限制不在允许范围内')
    root = _private_dir(root)
    with _lock(root), open_db(root) as db:
        started = now()
        # Persist interruption visibly; an earlier successful scan is retained.
        _save_source(db, account, channel, state='running', started=started)
        thread_pages = reply_pages = parents_scanned = reply_incomplete = parent_errors = 0
        total = 0
        complete = False
        try:
            identity(client, channel)
            if SCOPE not in client.granted_scopes():
                raise PublishError('缺少 YouTube 评论权限')
            cursor = db.execute('''SELECT next_thread_token FROM interaction_sources WHERE platform='youtube'
                AND account=? AND channel_id=?''', (account,channel)).fetchone()[0]
            fetch_threads = lambda token: threads(client, channel_id=channel, page_size=100, page_token=token)
            full_head_scan = cursor is None
            if cursor:
                # Always re-read the newest page even while backfilling a long
                # history. Continue history with the remaining request budget.
                latest, first_pages, first_complete, _ = _pages(fetch_threads, 1)
                if first_complete:
                    raw, thread_pages, complete, next_thread_token = latest, first_pages, True, None
                    full_head_scan = True
                else:
                    older, other_pages, complete, next_thread_token = _pages(fetch_threads, max_thread_pages - 1, cursor)
                    raw, thread_pages = latest + older, first_pages + other_pages
            else:
                raw, thread_pages, complete, next_thread_token = _pages(fetch_threads, max_thread_pages)
            stamp = now()
            for thread in raw:
                if not isinstance(thread, dict) or not _nonempty(thread.get('id')) or not isinstance(thread.get('snippet'), dict):
                    raise PublishError('YouTube 评论串数据不完整')
                sn = thread['snippet']
                top = _comment(sn.get('topLevelComment'), thread_id=thread['id'], parent_id=None,
                               video_id=sn.get('videoId'), channel=channel)
                _upsert(db, account, channel, top, stamp)
                db.execute('''INSERT INTO interaction_parents(platform,account,channel_id,parent_id,thread_id,video_id)
                    VALUES('youtube',?,?,?,?,?) ON CONFLICT(platform,account,channel_id,parent_id)
                    DO UPDATE SET thread_id=excluded.thread_id,video_id=excluded.video_id''',
                    (account,channel,top[0],thread['id'],sn.get('videoId')))
            db.execute('''UPDATE interaction_sources SET next_thread_token=? WHERE platform='youtube'
                AND account=? AND channel_id=?''', (next_thread_token,account,channel))
            db.commit()
            total = db.execute('SELECT COUNT(*) FROM interaction_parents WHERE platform=? AND account=? AND channel_id=?',
                               ('youtube',account,channel)).fetchone()[0]
            parents = db.execute('''SELECT parent_id,thread_id,video_id,next_page_token FROM interaction_parents
                WHERE platform=? AND account=? AND channel_id=?
                ORDER BY CASE WHEN last_attempt_at IS NULL THEN 0 ELSE 1 END,
                         last_attempt_at, parent_id LIMIT ?''', ('youtube',account,channel,max_parents)).fetchall()
            for parent in parents:
                try:
                    fetch_replies = lambda token: replies(client, parent['parent_id'], page_size=100, page_token=token)
                    parent_cursor = parent['next_page_token']
                    full_reply_scan = parent_cursor is None
                    if parent_cursor:
                        recent, first_count, first_done, _ = _pages(fetch_replies, 1)
                        if first_done:
                            found, count, done, next_page_token = recent, first_count, True, None
                            full_reply_scan = True
                        else:
                            older, more_count, done, next_page_token = _pages(fetch_replies, max_reply_pages-1, parent_cursor)
                            found, count = recent + older, first_count + more_count
                    else:
                        found, count, done, next_page_token = _pages(fetch_replies, max_reply_pages)
                except PublishError:
                    # Rotate past inaccessible threads; never call a failed parent empty.
                    db.execute('''UPDATE interaction_parents SET last_attempt_at=? WHERE platform=? AND account=?
                        AND channel_id=? AND parent_id=?''',
                        (now(),'youtube',account,channel,parent['parent_id']))
                    db.commit()
                    reply_incomplete += 1
                    parent_errors += 1
                    continue
                reply_pages += count
                stamp = now()
                for item in found:
                    _upsert(db, account, channel, _comment(item, thread_id=parent['thread_id'],
                        parent_id=parent['parent_id'], video_id=parent['video_id'], channel=channel), stamp)
                if done and full_reply_scan:
                    db.execute('''UPDATE interaction_parents SET last_complete_scan_at=?,last_attempt_at=?,next_page_token=NULL WHERE platform=?
                        AND account=? AND channel_id=? AND parent_id=?''',
                        (stamp,stamp,'youtube',account,channel,parent['parent_id']))
                    parents_scanned += 1
                else:
                    reply_incomplete += 1
                    db.execute('''UPDATE interaction_parents SET next_page_token=?,last_attempt_at=? WHERE platform=? AND account=?
                        AND channel_id=? AND parent_id=?''',
                        (next_page_token,stamp,'youtube',account,channel,parent['parent_id']))
                db.commit()
            # A bounded rotation is explicitly partial, not a claim of full coverage.
            # Finishing a continuation only proves the tail was checked in this
            # invocation, not that recent first-page comments were checked too.
            status = 'complete' if full_head_scan and complete and parents_scanned == total and reply_incomplete == 0 else 'partial'
            _save_source(db, account, channel, state=status, started=started,
                error='部分评论回复未查完或查询失败' if reply_incomplete else None, thread_pages=thread_pages,
                threads_complete=complete, parents_scanned=parents_scanned, parents_total=total,
                reply_pages=reply_pages, reply_incomplete=reply_incomplete)
        except (PublishError, OSError, ValueError, sqlite3.Error):
            db.rollback()
            _save_source(db, account, channel, state='error', started=started, error='同步未完成；检查账号授权、网络与平台响应',
                thread_pages=thread_pages, threads_complete=complete, parents_scanned=parents_scanned,
                parents_total=total, reply_pages=reply_pages, reply_incomplete=reply_incomplete)
            raise PublishError('YouTube 评论同步未完成；状态已记录，请检查账号授权、网络与平台响应') from None
        return {'state':status,'thread_pages':thread_pages,'threads_complete':complete,
                'parents_scanned':parents_scanned,'parents_total':total,'reply_pages':reply_pages,
                'reply_incomplete':reply_incomplete,'parent_errors':parent_errors}


def run_cli(argv=None, *, client_factory=APIClient, sleep_fn=time.sleep):
    p = argparse.ArgumentParser(description='本机私有互动收件箱；仅查询，不发送')
    p.add_argument('--profile', required=True); p.add_argument('--account', required=True); p.add_argument('--state-dir', required=True)
    sub = p.add_subparsers(dest='action', required=True)
    sync = sub.add_parser('sync-youtube')
    sync.add_argument('--max-thread-pages', type=int, default=20)
    sync.add_argument('--max-parents', type=int, default=20)
    sync.add_argument('--max-reply-pages', type=int, default=10)
    watch = sub.add_parser('watch-youtube', help='主动启动前台只读轮询，不发送；关闭后不运行')
    watch.add_argument('--interval-seconds',type=int,default=1800)
    watch.add_argument('--cycles',type=int,default=0,help='0 表示持续运行；正数表示运行的轮数')
    watch.add_argument('--notify-macos', action='store_true', help='同步后按需请求 macOS 汇总通知；默认关闭')
    sub.add_parser('status')
    sub.add_parser('monitor-status', help='只读检查同机巡检锁、时间及轮次；不请求 OAuth 或平台')
    listing = sub.add_parser('list'); listing.add_argument('--include-text', action='store_true')
    mark = sub.add_parser('mark'); mark.add_argument('--comment-id', required=True); mark.add_argument('--status', required=True, choices=['new','handled'])
    sub.add_parser('reset-cursors', help='分页令牌失效时从头扫描；保留已发现评论和处理状态')
    args = p.parse_args(argv)
    profile = load_profile(args.profile)
    channel = _account_channel(profile, args.account)
    root = _private_dir(args.state_dir)
    if args.action == 'monitor-status':
        from interaction_monitor import status as monitor_status
        return monitor_status(root, 'youtube', args.account, channel)
    if args.action in {'sync-youtube','watch-youtube'}:
        if args.action == 'watch-youtube' and (args.interval_seconds < 300 or args.cycles < 0):
            raise PublishError('轮询间隔至少 300 秒，轮数不能为负')
        def one_sync():
            try:
                configured, token = config(profile, {'platform':'youtube','account':args.account}, writing=False)
                if configured != channel:
                    raise PublishError('频道绑定不一致')
            except PublishError:
                with _lock(root), open_db(root) as db:
                    _save_source(db,args.account,channel,state='error',started=now(),error='YouTube 授权或账号配置不可用')
                raise
            opts = {'max_thread_pages':args.max_thread_pages, 'max_parents':args.max_parents,
                    'max_reply_pages':args.max_reply_pages} if args.action == 'sync-youtube' else {}
            return sync_youtube(client_factory(token),root,args.account,channel,**opts)
        if args.action == 'sync-youtube':
            return one_sync()
        from interaction_monitor import Monitor
        with Monitor(root, 'youtube', args.account, channel,
                     interval=args.interval_seconds, cycles=args.cycles) as monitor:
            failures = cycles = 0
            while not args.cycles or cycles < args.cycles:
                monitor.begin_cycle()
                try:
                    result = one_sync()
                    # One inaccessible old thread is partial, not a global failure.
                    failures = 0
                    if args.notify_macos:
                        try:
                            from interaction_notify import check as check_notification
                            result['notification'] = check_notification(root, notify=True)['notification']
                        except (PublishError, OSError, sqlite3.Error):
                            result['notification'] = 'error'
                except PublishError:
                    result = {'state':'error','error':'同步失败，详情见本机状态'}
                    failures += 1
                cycles += 1
                monitor.record_cycle(result['state'])
                print(json.dumps({'cycle':cycles, 'monitor_run_id':monitor.data['run_id'],
                                  **result},ensure_ascii=False),flush=True)
                if failures >= 3:
                    stopped = {'state':'error','reason':'连续三次同步失败，已停止','cycles':cycles}
                    if args.notify_macos:
                        try:
                            from interaction_notify import notify_monitor_stopped
                            stopped['notification'] = notify_monitor_stopped()
                        except (PublishError, OSError, sqlite3.Error):
                            stopped['notification'] = 'error'
                    monitor.finish('failed', 'consecutive_sync_errors')
                    return stopped
                if args.cycles and cycles >= args.cycles:
                    monitor.finish('completed', 'cycle_limit_reached')
                    return {'state':result['state'],'cycles':cycles}
                monitor.sleep()
                sleep_fn(args.interval_seconds)
    with _lock(root), open_db(root) as db:
        where = ('youtube',args.account,channel)
        if args.action == 'reset-cursors':
            db.execute('UPDATE interaction_sources SET next_thread_token=NULL WHERE platform=? AND account=? AND channel_id=?', where)
            db.execute('UPDATE interaction_parents SET next_page_token=NULL WHERE platform=? AND account=? AND channel_id=?', where)
            return {'state':'cursors_reset', 'next_step':'重新只读同步'}
        if args.action == 'mark':
            result = db.execute('''UPDATE interaction_items SET local_status=? WHERE platform=? AND account=?
                AND channel_id=? AND comment_id=?''', (args.status,*where,args.comment_id))
            if result.rowcount != 1:
                raise PublishError('找不到该评论')
            return {'state':'updated','status':args.status}
        if args.action == 'status':
            source = db.execute('SELECT * FROM interaction_sources WHERE platform=? AND account=? AND channel_id=?', where).fetchone()
            counts = db.execute('''SELECT local_status,COUNT(*) AS count FROM interaction_items
                WHERE platform=? AND account=? AND channel_id=? AND is_own=0 GROUP BY local_status''', where).fetchall()
            if source is None:
                return {'state':'not_checked'}
            return {'state':source['state'], 'last_success_at':source['last_success_at'],
                    'error':source['error'], 'counts':{r['local_status']:r['count'] for r in counts},
                    'threads_complete':bool(source['threads_complete']), 'parents_scanned':source['parents_scanned'],
                    'parents_total':source['parents_total'], 'reply_incomplete':source['reply_incomplete']}
        fields = 'comment_id,thread_id,parent_id,video_id,published_at,updated_at,local_status'
        if args.include_text: fields += ',author_name,body'
        rows = db.execute('SELECT '+fields+''' FROM interaction_items WHERE platform=? AND account=? AND channel_id=?
            AND is_own=0 ORDER BY COALESCE(published_at,first_seen_at) DESC''', where).fetchall()
        return {'items':[dict(row) for row in rows]}


def main():
    try:
        result = run_cli()
        print(json.dumps(result, ensure_ascii=False))
        if result.get('state') == 'error':
            raise SystemExit(1)
    except (PublishError, EnvironmentError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
