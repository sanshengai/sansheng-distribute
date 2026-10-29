#!/usr/bin/env python3
"""Validate an Agent's visible Alipay DOM capture; no browser or network access."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

from interaction_import import _safe_state_dir, _string, import_manual, mark_manual
from interaction_inbox import _lock, open_db
from publish_core import PublishError

SCHEMA = '''CREATE TABLE IF NOT EXISTS interaction_web_provenance (
 platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
 comment_id TEXT NOT NULL, identity_kind TEXT NOT NULL, source_url TEXT NOT NULL,
 work_title TEXT NOT NULL, observed_at TEXT NOT NULL, own_reply_seen INTEGER NOT NULL,
 PRIMARY KEY(platform,account,channel_id,comment_id));'''
LOCAL_TZ = timezone(timedelta(hours=8))


def _dom_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'commentContent_[a-f0-9]{32}', value):
        raise PublishError('缺少页面真实 commentContent 标识；不能使用文字摘要充当评论ID')
    return value


def normalize(raw, account, channel, account_name):
    """Keep the complete DOM ID, not an inferred API ID or a local text hash."""
    account = _string(account, 'account', 256)
    if not isinstance(raw, dict) or set(raw) != {'url', 'account_name', 'observed_at', 'items', 'works', 'id_recheck'}:
        raise PublishError('网页采集字段不完整或含未知字段')
    page = urlsplit(_string(raw['url'], 'url', 2000))
    if (page.scheme != 'https' or page.netloc != 'c.alipay.com'
            or page.path != '/page/content-creation/comment'
            or parse_qs(page.query) != {'appId': [channel]} or not re.fullmatch(r'\d{16}', channel)):
        raise PublishError('支付宝评论页与指定生活号不一致')
    if raw['account_name'] != _string(account_name, 'account_name', 256):
        raise PublishError('网页账号名称与指定账号不一致')
    try:
        observed = datetime.fromisoformat(raw['observed_at'].replace('Z', '+00:00'))
        if observed.tzinfo is None or observed > datetime.now(timezone.utc):
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        raise PublishError('观察时间无效') from None
    works, titles = {}, {}
    if not isinstance(raw['works'], list) or not 1 <= len(raw['works']) <= 1000:
        raise PublishError('必须提供实际作品选择器记录')
    for work in raw['works']:
        if not isinstance(work, dict) or set(work) != {'key', 'title'}:
            raise PublishError('作品记录字段无效')
        key, title = work['key'], _string(work['title'], 'work_title', 1000)
        if not isinstance(key, str) or not re.fullmatch(r'\d{8}OB\d{18}', key):
            raise PublishError('缺少作品选择器真实 data-row-key')
        if key in works and works[key] != title:
            raise PublishError('同一作品标识对应不同标题')
        works[key] = title
        titles.setdefault(title, set()).add(key)
    recheck = {}
    if not isinstance(raw['id_recheck'], list) or not 1 <= len(raw['id_recheck']) <= 1000:
        raise PublishError('缺少重新加载后的原评标识复核')
    for row in raw['id_recheck']:
        if not isinstance(row, dict) or set(row) != {'id', 'text'}:
            raise PublishError('复核记录字段无效')
        key, body = _dom_id(row['id']), _string(row['text'], 'body', 20000)
        if key in recheck:
            raise PublishError('复核记录含重复评论标识')
        recheck[key] = body
    if not isinstance(raw['items'], list) or not 1 <= len(raw['items']) <= 200:
        raise PublishError('必须提供非空的可见评论，空输入不代表没有留言')
    items, provenance, handled, seen = [], [], [], set()

    def comment(row, video, title, parent=None):
        fields = {'dom_id', 'body', 'author_name', 'time_text', 'is_own'}
        expected = fields if parent else fields | {'work_title', 'replies_expanded', 'replies'}
        if not isinstance(row, dict) or set(row) != expected:
            raise PublishError('评论字段无效')
        key = _dom_id(row['dom_id'])
        if key in seen:
            raise PublishError('采集含重复评论标识；请只读取当前可见面板')
        seen.add(key)
        body = _string(row['body'], 'body', 20000)
        author = _string(row['author_name'], 'author_name', 1000)
        if not isinstance(row['is_own'], bool) or row['is_own'] != (author == account_name):
            raise PublishError('必须明确核对作者身份；本人名称只用于排除本人留言')
        if not isinstance(row['time_text'], str) or not re.fullmatch(r'\d{2}-\d{2} \d{2}:\d{2}', row['time_text']):
            raise PublishError('缺少页面显示的评论时间')
        try:
            local = observed.astimezone(LOCAL_TZ)
            published = datetime.strptime(f'{local.year}-{row["time_text"]}', '%Y-%m-%d %H:%M').replace(tzinfo=LOCAL_TZ)
            if published > local:
                published = published.replace(year=published.year - 1)
            if local - published > timedelta(days=180):
                raise ValueError()
        except ValueError:
            raise PublishError('评论时间无效') from None
        items.append({'comment_id': key, 'video_id': video, 'thread_id': parent or key,
                      'parent_id': parent, 'body': body, 'author_name': author,
                      'is_own': row['is_own'], 'published_at': published.astimezone(timezone.utc).isoformat()})
        provenance.append((key, title, False))
        return key

    for row in raw['items']:
        if not isinstance(row, dict):
            raise PublishError('评论记录无效')
        title = _string(row.get('work_title'), 'work_title', 1000)
        candidates = titles.get(title, set())
        if len(candidates) != 1:
            raise PublishError('作品标题没有唯一匹配；不能猜测作品归属')
        video = next(iter(candidates))
        if recheck.get(row.get('dom_id')) != row.get('body'):
            raise PublishError('刷新后评论标识或原文不一致')
        if row.get('replies_expanded') is not True or not isinstance(row.get('replies'), list) or len(row['replies']) > 100:
            raise PublishError('先展开当前可见的已有回复；不将折叠误判为没有回复')
        key = comment(row, video, title)
        for reply in row['replies']:
            comment(reply, video, title, key)
        if not row['is_own'] and any(reply['is_own'] for reply in row['replies']):
            handled.append(key)
    provenance = [(key, title, key in handled) for key, title, _ in provenance]
    return {'platform': 'alipay', 'account': account, 'channel_id': channel, 'items': items}, provenance, handled, observed.isoformat()


def ingest(root, raw, *, account, channel, account_name):
    document, provenance, handled, observed = normalize(raw, account, channel, account_name)
    result = import_manual(root, document)
    root = _safe_state_dir(root)
    # If interrupted before provenance is saved, the import remains manual and cannot send.
    with _lock(root), open_db(root) as db:
        db.execute(SCHEMA)
        for key, title, replied in provenance:
            db.execute('''INSERT INTO interaction_web_provenance VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(platform,account,channel_id,comment_id) DO UPDATE SET
                source_url=excluded.source_url,work_title=excluded.work_title,
                observed_at=excluded.observed_at,own_reply_seen=MAX(own_reply_seen,excluded.own_reply_seen)''',
                ('alipay', account, channel, key, 'alipay_dom', raw['url'], title, observed, int(replied)))
    for key in handled:
        mark_manual(root, 'alipay', account, channel, key, 'handled')
    return {**result, 'identity_kind': 'alipay_dom', 'existing_replies_seen': len(handled),
            'api_verified': False, 'social_writes': 0}


def main():
    p = argparse.ArgumentParser(description='校验并接入 Agent 已读取的支付宝网页评论；不联网、不发送')
    p.add_argument('--state-dir', required=True)
    p.add_argument('--input', required=True)
    p.add_argument('--account', required=True)
    p.add_argument('--channel', required=True)
    p.add_argument('--account-name', required=True)
    args = p.parse_args()
    try:
        path = Path(args.input)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 2 * 1024 * 1024:
            raise PublishError('输入须为不超过2MiB的0600私有普通文件')
        print(json.dumps(ingest(args.state_dir, json.loads(path.read_text()), account=args.account,
                              channel=args.channel, account_name=args.account_name), ensure_ascii=False))
    except (PublishError, OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
