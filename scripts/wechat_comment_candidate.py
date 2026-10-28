#!/usr/bin/env python3
"""Optional WeChat Official Account article-comment reader; never writes remotely."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import urllib.parse

from environment import EnvironmentError, load_profile
from interaction_import import _safe_state_dir
from interaction_inbox import _lock, open_db
from publish_core import PublishError, now
from wechat_adapter import credentials
import wechat_api


def _number(value, label, *, minimum=0, maximum=(2**64 - 1)):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PublishError(f'{label} 须为范围内的整数')
    return value


def _timestamp(value):
    _number(value, '评论时间')
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        raise PublishError('平台返回的评论时间无效') from None


def list_page(post, token, msg_data_id, index, begin, count=40):
    """Contract: listComment is read-only; it is not a comment/reply action."""
    _number(msg_data_id, '群发消息 ID', minimum=1)
    _number(index, '文章序号', maximum=1000)
    _number(begin, '分页起点', maximum=10000000)
    _number(count, '每页数量', minimum=1, maximum=49)
    if not isinstance(token, str) or not token:
        raise PublishError('公众号凭证不可用')
    endpoint = f'{wechat_api.API_BASE}/cgi-bin/comment/list?access_token={urllib.parse.quote(token, safe="")}'
    result = post(endpoint, {'msg_data_id': msg_data_id, 'index': index, 'begin': begin,
                             'count': count, 'type': 0}, 'comment/list')
    if not isinstance(result, dict) or result.get('errcode') not in (None, 0):
        raise PublishError('公众号评论接口未成功；请检查账号权限及群发消息 ID')
    total = result.get('total')
    comments = result.get('comment')
    if type(total) is not int or total < 0 or not isinstance(comments, list) or len(comments) > count:
        raise PublishError('公众号评论响应不完整')
    return total, comments


def _normalize(item, msg_data_id, index):
    if not isinstance(item, dict):
        raise PublishError('公众号评论响应不完整')
    comment_id = _number(item.get('user_comment_id'), '评论 ID', minimum=1)
    created = _timestamp(item.get('create_time'))
    body = item.get('content')
    if not isinstance(body, str) or not body.strip() or len(body) > 20000:
        raise PublishError('公众号评论正文无效')
    reply = item.get('reply')
    reply_body = reply_time = None
    if reply is not None:
        if not isinstance(reply, dict) or not isinstance(reply.get('content'), str):
            raise PublishError('公众号回复数据不完整')
        reply_body = reply['content']
        reply_time = _timestamp(reply.get('create_time'))
    identity = f'{msg_data_id}:{index}:{comment_id}'
    return identity, comment_id, created, body, reply_body, reply_time


def sync_article(root, account, channel, msg_data_id, index, fetch, *, max_pages=20):
    """Scan an explicitly selected broadcast article, without claiming account coverage."""
    _number(msg_data_id, '群发消息 ID', minimum=1)
    _number(index, '文章序号', maximum=1000)
    _number(max_pages, '最大页数', minimum=1, maximum=100)
    root = _safe_state_dir(root)
    with _lock(root), open_db(root) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS interaction_wechat_articles (
            account TEXT NOT NULL, channel_id TEXT NOT NULL, msg_data_id TEXT NOT NULL,
            article_index INTEGER NOT NULL, state TEXT NOT NULL, scanned_at TEXT,
            pages INTEGER NOT NULL DEFAULT 0, total INTEGER, error TEXT,
            PRIMARY KEY(account,channel_id,msg_data_id,article_index))''')
        db.execute('''CREATE TABLE IF NOT EXISTS interaction_wechat_comments (
            account TEXT NOT NULL, channel_id TEXT NOT NULL, comment_id TEXT NOT NULL,
            msg_data_id TEXT NOT NULL, article_index INTEGER NOT NULL, user_comment_id TEXT NOT NULL,
            reply_body TEXT, reply_time TEXT,
            PRIMARY KEY(account,channel_id,comment_id))''')
        stamp = now()
        seen = set()
        pages = begin = 0
        expected_total = None
        complete = False
        failure = None
        try:
            while pages < max_pages:
                total, comments = fetch(msg_data_id, index, begin, 40)
                pages += 1
                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    raise PublishError('查询期间留言数量变化，需要重新检查')
                if begin + len(comments) > total:
                    raise PublishError('分页数量超过平台声明总数，需要重新检查')
                for entry in comments:
                    identity, original, created, body, reply_body, reply_time = _normalize(entry, msg_data_id, index)
                    if identity in seen:
                        raise PublishError('分页中出现重复留言，需要重新检查')
                    seen.add(identity)
                    db.execute('''INSERT INTO interaction_items(platform,account,channel_id,comment_id,thread_id,
                        video_id,body,published_at,first_seen_at,last_seen_at,is_own)
                        VALUES('wechat',?,?,?,?,?,?,?,?,?,0)
                        ON CONFLICT(platform,account,channel_id,comment_id) DO UPDATE SET
                        body=excluded.body,published_at=excluded.published_at,last_seen_at=excluded.last_seen_at''',
                        (account,channel,identity,identity,f'{msg_data_id}:{index}',body,created,stamp,stamp))
                    db.execute('''INSERT INTO interaction_wechat_comments(account,channel_id,comment_id,msg_data_id,
                        article_index,user_comment_id,reply_body,reply_time) VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(account,channel_id,comment_id) DO UPDATE SET
                        reply_body=excluded.reply_body,reply_time=excluded.reply_time''',
                        (account,channel,identity,str(msg_data_id),index,str(original),reply_body,reply_time))
                begin += len(comments)
                if begin >= total:
                    complete = True
                    break
                if not comments:
                    raise PublishError('分页提前结束，留言覆盖不完整')
            if not complete:
                failure = '已达页数上限，所选文章尚未查完'
        except (PublishError, wechat_api.WechatError, OSError, ValueError, sqlite3.Error):
            failure = '查询失败或数据不完整；检查权限、网络及群发消息 ID'
        state = 'complete' if complete and not failure else 'partial' if pages and failure else 'error'
        db.execute('''INSERT INTO interaction_wechat_articles(account,channel_id,msg_data_id,article_index,state,
            scanned_at,pages,total,error) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account,channel_id,msg_data_id,article_index) DO UPDATE SET state=excluded.state,
            scanned_at=excluded.scanned_at,pages=excluded.pages,total=excluded.total,error=excluded.error''',
            (account,channel,str(msg_data_id),index,state,stamp,pages,expected_total,failure))
        source_note = '仅查询指定群发文章，不代表公众号全部文章' + (f'；{failure}' if failure else '')
        account_state = 'error' if state == 'error' else 'partial'
        db.execute('''INSERT INTO interaction_sources(platform,account,channel_id,state,started_at,finished_at,error)
            VALUES('wechat',?,?,?,?,?,?) ON CONFLICT(platform,account,channel_id) DO UPDATE SET
            state=excluded.state,started_at=excluded.started_at,finished_at=excluded.finished_at,error=excluded.error''',
            (account,channel,account_state,stamp,stamp,source_note))
        return {'article_state':state,'pages':pages,'observed':len(seen),
                'account_state':account_state,'note':source_note}


def record_auth_failure(root, account, channel):
    """Record an unsuccessful check only after credentials confirmed identity."""
    root = _safe_state_dir(root)
    stamp = now()
    with _lock(root), open_db(root) as db:
        db.execute('''INSERT INTO interaction_sources(platform,account,channel_id,state,started_at,finished_at,error)
            VALUES('wechat',?,?,'error',?,?,'凭证获取失败，本次未查询平台')
            ON CONFLICT(platform,account,channel_id) DO UPDATE SET state=excluded.state,
            started_at=excluded.started_at,finished_at=excluded.finished_at,error=excluded.error''',
            (account,channel,stamp,stamp))


def main():
    parser = argparse.ArgumentParser(description='公众号指定群发文章留言只读候选；不发评论')
    parser.add_argument('--profile', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--msg-data-id', required=True, type=int, help='指定已群发消息返回的数字 ID，不是草稿 ID')
    parser.add_argument('--index', type=int, default=0)
    parser.add_argument('--max-pages', type=int, default=20)
    args = parser.parse_args()
    try:
        _number(args.msg_data_id, '群发消息 ID', minimum=1)
        _number(args.index, '文章序号', maximum=1000)
        _number(args.max_pages, '最大页数', minimum=1, maximum=100)
        profile = load_profile(args.profile)
        binding = profile.get('accounts', {}).get(args.account, {})
        if 'wechat' not in binding.get('platforms', []):
            raise PublishError('公众号账号未绑定')
        app_id, secret = credentials(profile, args.account)
        channel = hashlib.sha256(app_id.encode()).hexdigest()
        # Resolve an existing cached credential privately; do not put it on argv or stdout.
        try:
            token = wechat_api.access_token(app_id, secret)
        except Exception:
            record_auth_failure(args.state_dir, args.account, channel)
            raise PublishError('公众号凭证获取失败') from None
        def fetch(msg, index, begin, count):
            return list_page(wechat_api.post_json, token, msg, index, begin, count)
        result = sync_article(args.state_dir, args.account, channel, args.msg_data_id,
                              args.index, fetch, max_pages=args.max_pages)
        print(json.dumps(result, ensure_ascii=False))
        if result['article_state'] == 'error':
            raise SystemExit(1)
    except (PublishError, wechat_api.WechatError, EnvironmentError, OSError, sqlite3.Error):
        print(json.dumps({'error': '公众号留言查询未完成；请检查配置、权限和群发文章身份'}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
