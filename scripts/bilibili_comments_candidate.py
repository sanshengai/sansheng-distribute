#!/usr/bin/env python3
"""Explicit, bounded Bilibili web-protocol reads; never posts or refreshes login."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

from interaction_import import _safe_state_dir
from interaction_inbox import _lock, open_db
from publish_core import PublishError, now

SCHEMA = '''CREATE TABLE IF NOT EXISTS interaction_bilibili_cursors (
 account TEXT NOT NULL, channel_id TEXT NOT NULL, video_id TEXT NOT NULL,
 kind TEXT NOT NULL, parent_id TEXT NOT NULL, next_page INTEGER NOT NULL,
 last_attempt_at TEXT, last_complete_at TEXT,
 PRIMARY KEY(account,channel_id,video_id,kind,parent_id));
CREATE TABLE IF NOT EXISTS interaction_bilibili_baselines (
 account TEXT NOT NULL, channel_id TEXT NOT NULL, comment_id TEXT NOT NULL,
 recorded_at TEXT NOT NULL, PRIMARY KEY(account,channel_id,comment_id));
CREATE TABLE IF NOT EXISTS interaction_bilibili_scope (
 account TEXT NOT NULL, channel_id TEXT NOT NULL, video_ids TEXT NOT NULL,
 not_before TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(account,channel_id));'''


def _private_json(path):
    p = Path(path)
    if not p.is_absolute() or p.is_symlink():
        raise PublishError('需要绝对路径的私有普通 JSON 文件')
    fd = os.open(p, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o777 != 0o600 or info.st_size > 128 * 1024):
            raise PublishError('私有文件须由当前用户持有、权限 0600 且不超过 128 KiB')
        with os.fdopen(fd, encoding='utf-8') as f:
            fd = -1
            return json.load(f)
    finally:
        if fd >= 0:
            os.close(fd)


def _id(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value):
        return value
    raise PublishError('B站身份须为平台返回的正整数 ID')


def validate_config(raw):
    fields = {'enabled', 'account', 'uid', 'credentials', 'not_before', 'works'}
    if not isinstance(raw, dict) or set(raw) != fields or raw['enabled'] is not True:
        raise PublishError('B站只读候选须显式 enabled=true 且配置字段完整')
    if not isinstance(raw['account'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', raw['account']):
        raise PublishError('账号别名格式无效')
    _id(raw['uid'])
    if not isinstance(raw['credentials'], str) or not Path(raw['credentials']).is_absolute():
        raise PublishError('凭证路径须为私有绝对路径')
    try:
        t = datetime.fromisoformat(raw['not_before'].replace('Z', '+00:00'))
        if t.tzinfo is None or t > datetime.now(timezone.utc):
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        raise PublishError('not_before 须为带时区且不在未来的监测起点')
    if not isinstance(raw['works'], list) or not 1 <= len(raw['works']) <= 20:
        raise PublishError('须明确列出 1 至 20 个自己的作品；空配置不表示没有评论')
    seen = set()
    for work in raw['works']:
        if (not isinstance(work, dict) or set(work) != {'aid', 'bvid'}
                or not isinstance(work['bvid'], str) or not re.fullmatch(r'BV[A-Za-z0-9]{10}', work['bvid'])):
            raise PublishError('每个作品须有实际 aid 和 bvid')
        aid = _id(work['aid'])
        if aid in seen:
            raise PublishError('作品列表不可重复')
        seen.add(aid)
    return raw


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublishError('B站只读接口发生重定向；不转发凭据')


class Client:
    def __init__(self, credentials):
        raw = _private_json(credentials)
        items = raw.get('cookie_info', {}).get('cookies') if isinstance(raw, dict) else None
        if not isinstance(items, list) or not items:
            raise PublishError('既有凭证缺少网页 Cookie；不另行登录或刷新')
        pairs = []
        for item in items:
            if not isinstance(item, dict):
                raise PublishError('凭证字段格式无效')
            name, value = item.get('name'), item.get('value')
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name)
                    or not isinstance(value, str) or any(c in value for c in '\r\n;')):
                raise PublishError('凭证字段格式无效')
            pairs.append(name + '=' + value)
        self.cookie = '; '.join(pairs)
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, kind, **params):
        paths = {'identity': '/x/web-interface/nav', 'work': '/x/web-interface/view',
                 'threads': '/x/v2/reply', 'replies': '/x/v2/reply/reply'}
        if kind not in paths:
            raise PublishError('不支持的 B站只读动作')
        url = 'https://api.bilibili.com' + paths[kind]
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method='GET', headers={
            'Cookie': self.cookie, 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
        try:
            with self.opener.open(req, timeout=25) as response:
                b = response.read(2 * 1024 * 1024 + 1)
            if len(b) > 2 * 1024 * 1024:
                raise PublishError('B站响应过大；保留未完成状态')
            raw = json.loads(b)
        except urllib.error.HTTPError as exc:
            raise PublishError(f'B站只读 {kind} 查询 HTTP {exc.code}；不换身份或自动重试') from None
        except (OSError, ValueError):
            raise PublishError('B站只读查询失败；不把错误当作空评论') from None
        if isinstance(raw, dict) and type(raw.get('code')) is int and raw['code'] != 0:
            raise PublishError(f'B站只读 {kind} 查询被拒绝，code={raw["code"]}；不回显凭据或响应正文')
        if (not isinstance(raw, dict) or type(raw.get('code')) is not int
                or not isinstance(raw.get('data'), dict)):
            raise PublishError('B站接口拒绝或数据缺失；不把错误当作空评论')
        return raw['data']


@contextmanager
def _account_lock(root, account, uid):
    # Local aliases must not create two pollers for the same physical account.
    key = hashlib.sha256(('bilibili:' + uid).encode()).hexdigest()
    p = root / ('bilibili-comments-' + key + '.lock')
    fd = os.open(p, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise PublishError('B站巡检锁权限无效')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishError('同账号 B站只读巡检仍在运行') from None
        yield
    finally:
        os.close(fd)


def _comment(raw, aid, uid, root=None):
    if (not isinstance(raw, dict) or _id(raw.get('oid')) != aid
            or type(raw.get('type')) is not int or raw['type'] != 1):
        raise PublishError('评论的作品身份不符')
    rid, author = _id(raw.get('rpid')), _id(raw.get('mid'))
    for k in ('rpid', 'oid', 'mid', 'root', 'parent'):
        if k + '_str' in raw and str(raw.get(k)) != raw[k + '_str']:
            raise PublishError('B站整型与文字身份字段冲突')
    if raw.get('invisible') is True or raw.get('state', 0) != 0:
        raise PublishError('评论可见性或状态未确认')
    if root is None:
        if (type(raw.get('root')) is not int or type(raw.get('parent')) is not int
                or raw['root'] != 0 or raw['parent'] != 0):
            raise PublishError('一级评论父级不符')
        thread, parent = rid, None
    else:
        if _id(raw.get('root')) != root:
            raise PublishError('子回复根评论不符')
        thread, parent = root, _id(raw.get('parent'))
    member, content = raw.get('member'), raw.get('content')
    if (not isinstance(member, dict) or _id(member.get('mid')) != author
            or not isinstance(content, dict) or not isinstance(content.get('message'), str)
            or not content['message'].strip() or len(content['message']) > 20000):
        raise PublishError('B站作者或正文不完整')
    epoch = raw.get('ctime')
    if type(epoch) is not int or not 0 < epoch <= datetime.now(timezone.utc).timestamp():
        raise PublishError('B站评论时间无效')
    stamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    return {'comment_id': rid, 'thread_id': thread, 'parent_id': parent, 'video_id': aid,
            'author_channel': author, 'author_name': member.get('uname'), 'body': content['message'],
            'published_at': stamp, 'updated_at': stamp, 'is_own': int(author == uid)}


def _store(db, account, uid, item, not_before):
    old = db.execute('''SELECT * FROM interaction_items WHERE platform='bilibili'
        AND account=? AND channel_id=? AND comment_id=?''', (account, uid, item['comment_id'])).fetchone()
    if old and any(old[k] != item[k] for k in ('video_id', 'thread_id', 'parent_id', 'author_channel')):
        raise PublishError('既有评论身份冲突；不改绑其他作品或作者')
    baseline = old is None and datetime.fromisoformat(item['published_at']) < not_before
    stamp = now()
    fields = ('comment_id', 'thread_id', 'parent_id', 'video_id', 'author_channel', 'author_name',
              'body', 'published_at', 'updated_at')
    db.execute('''INSERT INTO interaction_items
        (platform,account,channel_id,comment_id,thread_id,parent_id,video_id,author_channel,
         author_name,body,published_at,updated_at,first_seen_at,last_seen_at,is_own,local_status)
        VALUES ('bilibili',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform,account,channel_id,comment_id) DO UPDATE SET
          author_name=excluded.author_name,body=excluded.body,updated_at=excluded.updated_at,
          last_seen_at=excluded.last_seen_at,is_own=excluded.is_own''',
        (account, uid, *(item[k] for k in fields), stamp, stamp, item['is_own'], 'handled' if baseline else 'new'))
    if baseline:
        db.execute('INSERT OR IGNORE INTO interaction_bilibili_baselines VALUES (?,?,?,?)',
                   (account, uid, item['comment_id'], stamp))
    return int(old is None), int(baseline)


def _scan(client, root, cfg, work, kind, parent, limit, cutoff):
    account, uid, aid = cfg['account'], _id(cfg['uid']), _id(work['aid'])
    binding = (account, uid, aid, kind, parent)
    with _lock(root), open_db(root) as db:
        row = db.execute('SELECT next_page FROM interaction_bilibili_cursors WHERE account=? AND channel_id=?'
                         ' AND video_id=? AND kind=? AND parent_id=?', binding).fetchone()
        cursor = row[0] if row else 1
    pn, pages, added, baseline, head_full = 1, 0, 0, 0, cursor == 1
    seen, exhausted = set(), False
    while pages < limit:
        params = {'type': 1, 'oid': aid, 'pn': pn, 'ps': 20}
        params.update(sort=0) if kind == 'threads' else params.update(root=parent)
        raw = client.get(kind, **params)
        page, rows = raw.get('page'), raw.get('replies')
        if (not isinstance(page, dict) or type(page.get('num')) is not int or page['num'] != pn
                or type(page.get('size')) is not int or page['size'] != 20
                or type(page.get('count')) is not int or page['count'] < 0
                or (rows is not None and not isinstance(rows, list))):
            raise PublishError('B站分页结构无效')
        rows = rows or []
        expected = min(20, max(0, page['count'] - (pn - 1) * 20))
        if len(rows) != expected:
            raise PublishError('B站分页未覆盖声明的评论数量')
        if kind == 'replies':
            _comment(raw.get('root'), aid, uid)  # The returned root must be the requested root.
            if _id(raw['root']['rpid']) != parent:
                raise PublishError('子回复列表返回了其他根评论')
        normalized = [_comment(r, aid, uid, parent if kind == 'replies' else None) for r in rows]
        ids = {i['comment_id'] for i in normalized}
        if len(ids) != len(rows) or ids & seen:
            raise PublishError('B站分页返回重复评论；不宣称扫描完成')
        seen.update(ids)
        pages += 1
        exhausted = pn * 20 >= page['count']
        if exhausted:
            next_page = 1
        elif pn == 1 and cursor > 1:
            next_page = cursor
        else:
            next_page = pn + 1
        with _lock(root), open_db(root) as db:
            for item in normalized:
                n, b = _store(db, account, uid, item, cutoff)
                added += n; baseline += b
                if kind == 'threads':
                    db.execute('''INSERT OR IGNORE INTO interaction_bilibili_cursors
                        (account,channel_id,video_id,kind,parent_id,next_page) VALUES (?,?,?,'replies',?,1)''',
                        (account, uid, aid, item['comment_id']))
            db.execute('''INSERT INTO interaction_bilibili_cursors VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(account,channel_id,video_id,kind,parent_id) DO UPDATE SET
                next_page=excluded.next_page,last_attempt_at=excluded.last_attempt_at,
                last_complete_at=COALESCE(excluded.last_complete_at,interaction_bilibili_cursors.last_complete_at)''',
                (*binding, next_page, now(), now() if exhausted else None))
        if exhausted:
            break
        pn = next_page
    # A resumed history tail is not a fresh full traversal from the head.
    return {'pages': pages, 'added': added, 'baseline': baseline,
            'complete': exhausted and (head_full or pages == 1), 'tail_exhausted': exhausted}


def _source(root, cfg, started, state, *, counts=None, error=None):
    counts = counts or {}
    with _lock(root), open_db(root) as db:
        db.executescript(SCHEMA)
        db.execute('''INSERT INTO interaction_sources
            (platform,account,channel_id,state,started_at,finished_at,last_success_at,error,
             thread_pages,threads_complete,parents_scanned,parents_total,reply_pages,reply_incomplete)
            VALUES ('bilibili',?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform,account,channel_id) DO UPDATE SET state=excluded.state,
             started_at=excluded.started_at,finished_at=excluded.finished_at,
             last_success_at=COALESCE(excluded.last_success_at,interaction_sources.last_success_at),
             error=excluded.error,thread_pages=excluded.thread_pages,threads_complete=excluded.threads_complete,
             parents_scanned=excluded.parents_scanned,parents_total=excluded.parents_total,
             reply_pages=excluded.reply_pages,reply_incomplete=excluded.reply_incomplete''',
            (cfg['account'], _id(cfg['uid']), state, started, now() if state != 'running' else None,
             now() if state == 'complete' else None, error, counts.get('thread_pages', 0),
             int(counts.get('threads_complete', False)), counts.get('parents_scanned', 0),
             counts.get('parents_total', 0), counts.get('reply_pages', 0), counts.get('reply_incomplete', 0)))


def sync(root, cfg, client, *, max_pages=3, max_parents=10, max_reply_pages=3):
    cfg = validate_config(cfg)
    if (type(max_pages) is not int or not 2 <= max_pages <= 10
            or type(max_reply_pages) is not int or not 2 <= max_reply_pages <= 10
            or type(max_parents) is not int or not 1 <= max_parents <= 50):
        raise PublishError('B站扫描预算超出范围')
    root = _safe_state_dir(root)
    account, uid, started = cfg['account'], _id(cfg['uid']), now()
    cutoff = datetime.fromisoformat(cfg['not_before'].replace('Z', '+00:00'))
    counts = dict(thread_pages=0, threads_complete=True, parents_scanned=0, parents_total=0,
                  reply_pages=0, reply_incomplete=0, added=0, baseline=0)
    with _account_lock(root, account, uid):
        _source(root, cfg, started, 'running')
        with _lock(root), open_db(root) as db:
            db.execute('INSERT OR REPLACE INTO interaction_bilibili_scope VALUES (?,?,?,?,?)',
                       (account, uid, json.dumps([_id(w['aid']) for w in cfg['works']]), cfg['not_before'], now()))
        try:
            who = client.get('identity')
            if who.get('isLogin') is not True or _id(who.get('mid')) != uid:
                raise PublishError('B站登录失效或实际账号不符')
            # Verify every selected work before recording any comment.
            aids = []
            for work in cfg['works']:
                data = client.get('work', aid=_id(work['aid']))
                if (_id(data.get('aid')) != _id(work['aid']) or data.get('bvid') != work['bvid']
                        or _id((data.get('owner') or {}).get('mid')) != uid):
                    raise PublishError('B站作品与实际账号、aid、bvid 不符')
                aids.append(_id(work['aid']))
            for work in cfg['works']:
                r = _scan(client, root, cfg, work, 'threads', '', max_pages, cutoff)
                counts['thread_pages'] += r['pages']; counts['threads_complete'] &= r['complete']
                counts['added'] += r['added']; counts['baseline'] += r['baseline']
            with _lock(root), open_db(root) as db:
                parents = db.execute('''SELECT * FROM interaction_bilibili_cursors
                    WHERE account=? AND channel_id=? AND kind='replies' AND video_id IN ('''
                    + ','.join('?' for _ in aids) + ''') ORDER BY COALESCE(last_attempt_at,''),video_id,parent_id''',
                    (account, uid, *aids)).fetchall()
            counts['parents_total'] = len(parents)
            for row in parents[:max_parents]:
                r = _scan(client, root, cfg, {'aid': row['video_id']}, 'replies', row['parent_id'], max_reply_pages, cutoff)
                counts['parents_scanned'] += 1; counts['reply_pages'] += r['pages']
                counts['reply_incomplete'] += int(not r['complete'])
                counts['added'] += r['added']; counts['baseline'] += r['baseline']
            complete = (counts['threads_complete'] and counts['parents_scanned'] == counts['parents_total']
                        and not counts['reply_incomplete'])
            state = 'complete' if complete else 'partial'
            reason = (f'仅覆盖明确配置的 {len(aids)} 个作品；历史基线不自动拟稿；第三方只读协议，未验收发送'
                      + ('；分页或父评论轮转未完成' if not complete else ''))
            _source(root, cfg, started, state, counts=counts, error=reason)
            return {'platform': 'bilibili', 'state': state, 'scope': 'configured_works', 'works': len(aids),
                    **counts, 'official_api': False, 'writes': 0, 'send_enabled': False}
        except (PublishError, OSError, ValueError, TypeError, KeyError):
            _source(root, cfg, started, 'error', counts=counts,
                    error='B站只读扫描失败；既有评论保留，不自动重试或发送')
            raise PublishError('B站只读扫描失败；查看私有状态，不将失败计为空评论') from None


def main():
    p = argparse.ArgumentParser(description='显式启用的 B站只读候选；不刷新凭证、不发送')
    p.add_argument('--config', required=True); p.add_argument('--state-dir', required=True)
    p.add_argument('--max-pages', type=int, default=3); p.add_argument('--max-parents', type=int, default=10)
    p.add_argument('--max-reply-pages', type=int, default=3)
    args = p.parse_args()
    try:
        cfg = validate_config(_private_json(args.config))
        # Reject a public state path before reading any credential or contacting Bilibili.
        _safe_state_dir(args.state_dir)
        result = sync(args.state_dir, cfg, Client(cfg['credentials']), max_pages=args.max_pages,
                      max_parents=args.max_parents, max_reply_pages=args.max_reply_pages)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['state'] == 'complete' else 3
    except (PublishError, OSError, ValueError, TypeError):
        print('B站只读候选未完成；核对私有配置、账号权限与扫描状态', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
