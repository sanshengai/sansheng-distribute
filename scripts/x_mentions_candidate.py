#!/usr/bin/env python3
"""Opt-in official X mentions reader. No background execution or writes to X."""
import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from interaction_import import _safe_state_dir
from interaction_inbox import _lock, open_db
from publish_core import PublishError, now

ID = re.compile(r'^[0-9]{1,19}$')
ENV = re.compile(r'^[A-Z_][A-Z0-9_]{0,127}$')
HOST = 'https://api.x.com'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublishError('X 接口发生重定向，已停止以保护令牌')


def request_json(path, params, token):
    if path != '/2/users/me' and not re.fullmatch(r'/2/users/[0-9]{1,19}/mentions', path):
        raise PublishError('不允许请求此 X 接口')
    request = Request(HOST + path + ('?' + urlencode(params) if params else ''),
                      headers={'Authorization': 'Bearer ' + token}, method='GET')
    try:
        with build_opener(NoRedirect).open(request, timeout=20) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise PublishError('X 响应过大')
        data = json.loads(body)
    except (HTTPError, URLError, OSError, ValueError):
        raise PublishError('X 请求失败；请核对授权、配额、费用和接口可用性') from None
    if not isinstance(data, dict) or data.get('errors'):
        raise PublishError('X 响应有错误或不完整；不得视为无新互动')
    return data


def _page(client, user_id, cursor, page_size):
    # X's general field guide documents author_id as a tweet field; unlike an
    # expansion this does not request author user objects that may be billed.
    params = {'max_results': page_size, 'tweet.fields':'created_at,conversation_id,author_id'}
    if cursor:
        params['pagination_token'] = cursor
    data = client('/2/users/' + user_id + '/mentions', params)
    items = data.get('data', [])
    meta = data.get('meta')
    if not isinstance(items, list) or not isinstance(meta, dict) or type(meta.get('result_count')) is not int or meta['result_count'] != len(items):
        raise PublishError('X 返回的数据不完整')
    token = meta.get('next_token')
    if token is not None and (not isinstance(token, str) or not token or len(token) > 512):
        raise PublishError('X 分页令牌无效')
    return items, token


def _normalize(item, owner):
    if not isinstance(item, dict):
        raise PublishError('X 提及数据不完整')
    post_id = item.get('id')
    author = item.get('author_id')
    conversation = item.get('conversation_id')
    body = item.get('text')
    created = item.get('created_at')
    if (not isinstance(post_id, str) or not ID.fullmatch(post_id) or
        not isinstance(author, str) or not ID.fullmatch(author) or
        not isinstance(conversation, str) or not ID.fullmatch(conversation) or
        not isinstance(body, str) or not body.strip() or len(body) > 20000 or
        not isinstance(created, str) or not created):
        raise PublishError('X 提及缺少身份、正文或时间，已停止')
    return post_id, author, conversation, body, created, int(author == owner)


def _validate_source(account, user_id):
    if not isinstance(user_id, str) or not ID.fullmatch(user_id):
        raise PublishError('X 用户 ID 无效')
    if not isinstance(account, str) or not account.strip() or len(account) > 256 or '\x00' in account:
        raise PublishError('账号标识无效')


def _record_local_failure(root, account, user_id):
    _validate_source(account, user_id)
    root = _safe_state_dir(root)
    stamp = now()
    with _lock(root), closing(open_db(root)) as db:
        db.execute('''INSERT INTO interaction_sources(platform,account,channel_id,state,started_at,finished_at,error)
            VALUES('x',?,?,'error',?,?,?) ON CONFLICT(platform,account,channel_id) DO UPDATE SET
            state='error',started_at=excluded.started_at,finished_at=excluded.finished_at,error=excluded.error''',
            (account,user_id,stamp,stamp,'X OAuth 凭证不可用，本轮未访问平台'))
        db.commit()


def sync_mentions(root, account, user_id, client, *, page_size=10, max_pages=2):
    _validate_source(account, user_id)
    if type(page_size) is not int or not 5 <= page_size <= 100 or type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise PublishError('每页须 5–100 条，每轮最多 1–10 页')
    root = _safe_state_dir(root)
    with _lock(root), closing(open_db(root)) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS interaction_x_cursors (
            account TEXT NOT NULL, channel_id TEXT NOT NULL, next_token TEXT,
            PRIMARY KEY(account,channel_id))''')
        db.commit()
        started = now()
        cursor_row = db.execute('SELECT next_token FROM interaction_x_cursors WHERE account=? AND channel_id=?', (account,user_id)).fetchone()
        saved = cursor_row['next_token'] if cursor_row else None
        pages = 0
        try:
            identity = client('/2/users/me', {})
            if not isinstance(identity.get('data'), dict) or identity['data'].get('id') != user_id:
                raise PublishError('X OAuth 用户与指定账号不一致')
            # Always refresh first page; when backfilling, continue at saved cursor.
            latest, first_next = _page(client, user_id, None, page_size)
            pages = 1
            entries = list(latest)
            cursor = saved if saved and first_next else first_next
            seen_tokens = {cursor} if cursor else set()
            while cursor and pages < max_pages:
                older, next_token = _page(client, user_id, cursor, page_size)
                entries.extend(older)
                pages += 1
                if next_token and next_token in seen_tokens:
                    raise PublishError('X 分页令牌循环，已停止')
                if next_token:
                    seen_tokens.add(next_token)
                cursor = next_token
            stamp = now()
            for item in entries:
                post_id, author, conversation, body, created, is_own = _normalize(item, user_id)
                db.execute('''INSERT INTO interaction_items(platform,account,channel_id,comment_id,thread_id,
                    author_channel,body,published_at,first_seen_at,last_seen_at,is_own)
                    VALUES('x',?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(platform,account,channel_id,comment_id) DO UPDATE SET
                    thread_id=excluded.thread_id,author_channel=excluded.author_channel,body=excluded.body,
                    published_at=excluded.published_at,last_seen_at=excluded.last_seen_at,is_own=excluded.is_own''',
                    (account,user_id,post_id,conversation,author,body,created,stamp,stamp,is_own))
            db.execute('''INSERT INTO interaction_x_cursors(account,channel_id,next_token) VALUES(?,?,?)
                ON CONFLICT(account,channel_id) DO UPDATE SET next_token=excluded.next_token''', (account,user_id,cursor))
            note = '仅覆盖本次查询到的提及，不代表全部回复；结果需人工核对' + ('；仍有历史页待查' if cursor else '')
            db.execute('''INSERT INTO interaction_sources(platform,account,channel_id,state,started_at,finished_at,last_success_at,error)
                VALUES('x',?,?,'partial',?,?,?,?) ON CONFLICT(platform,account,channel_id) DO UPDATE SET
                state='partial',started_at=excluded.started_at,finished_at=excluded.finished_at,
                last_success_at=excluded.last_success_at,error=excluded.error''', (account,user_id,started,stamp,stamp,note))
            db.commit()
            return {'state':'partial','pages':pages,'posts_returned':len(entries),'history_pending':bool(cursor),'note':note}
        except (PublishError,OSError,sqlite3.Error,ValueError):
            db.rollback()
            stamp = now()
            db.execute('''INSERT INTO interaction_sources(platform,account,channel_id,state,started_at,finished_at,error)
                VALUES('x',?,?,'error',?,?,?) ON CONFLICT(platform,account,channel_id) DO UPDATE SET
                state='error',started_at=excluded.started_at,finished_at=excluded.finished_at,error=excluded.error''',
                (account,user_id,started,stamp,'X 读取未完成；请检查授权、配额、费用与接口状态'))
            db.commit()
            raise PublishError('X 提及读取未完成；不代表没有新互动') from None


def main():
    parser = argparse.ArgumentParser(description='X 官方提及读取候选；可能收费，仅在确认费用后按次执行')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--user-id', required=True)
    parser.add_argument('--token-env', help='OAuth access token 所在环境变量名，非 token 本身')
    parser.add_argument('--accept-paid-api', action='store_true', help='承认本次 API 查询可能产生费用')
    parser.add_argument('--reset-cursor', action='store_true', help='只重置私有分页进度，不联网，不删除记录')
    parser.add_argument('--page-size', type=int, default=10)
    parser.add_argument('--max-pages', type=int, default=2)
    args = parser.parse_args()
    try:
        if args.reset_cursor:
            _validate_source(args.account,args.user_id)
            root = _safe_state_dir(args.state_dir)
            with _lock(root), closing(open_db(root)) as db:
                db.execute('''CREATE TABLE IF NOT EXISTS interaction_x_cursors (
                    account TEXT NOT NULL, channel_id TEXT NOT NULL, next_token TEXT,
                    PRIMARY KEY(account,channel_id))''')
                db.execute('UPDATE interaction_x_cursors SET next_token=NULL WHERE account=? AND channel_id=?',
                           (args.account,args.user_id))
                db.execute('''UPDATE interaction_sources SET state='partial',
                    error='分页进度已重置，尚未重新查询；提及不代表全部回复'
                    WHERE platform='x' AND account=? AND channel_id=?''', (args.account,args.user_id))
                db.commit()
            print(json.dumps({'state':'cursor_reset','network':False},ensure_ascii=False))
            return
        if not args.accept_paid_api:
            raise PublishError('X API 可能收费；本次未明确确认费用，不调用接口')
        _validate_source(args.account,args.user_id)
        if not args.token_env or not ENV.fullmatch(args.token_env):
            _record_local_failure(args.state_dir,args.account,args.user_id)
            raise PublishError('凭证环境变量名无效')
        token = os.environ.get(args.token_env, '')
        if not token or '\n' in token or '\r' in token:
            _record_local_failure(args.state_dir,args.account,args.user_id)
            raise PublishError('X OAuth 凭证不可用')
        result = sync_mentions(args.state_dir, args.account, args.user_id,
                               lambda path, params: request_json(path, params, token),
                               page_size=args.page_size, max_pages=args.max_pages)
        print(json.dumps(result, ensure_ascii=False))
    except (PublishError,OSError,sqlite3.Error):
        print(json.dumps({'error':'X 读取未完成；检查费用、授权、配置和本地状态'},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
