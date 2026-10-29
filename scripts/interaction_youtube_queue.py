#!/usr/bin/env python3
"""Bind inbox drafts to existing YouTube plans, grants and one-attempt receipts."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

from environment import EnvironmentError, load_profile
from interaction_drafts import source
from interaction_inbox import _lock, open_db
from interaction_receipts import record_reply
from publish_core import PublishError, digest, now, read_json, write_json
from youtube_adapter import APIClient, config
from youtube_comments_candidate import (
    _account_channel, _checked_plan, _get_one, _plan_path, _private_dir,
    _receipt_key, account_lock, make_intent, reconcile, replies, send,
)

SCHEMA = '''CREATE TABLE IF NOT EXISTS interaction_dispatches (
 draft_key TEXT PRIMARY KEY, platform TEXT NOT NULL, account TEXT NOT NULL,
 channel_id TEXT NOT NULL, comment_id TEXT NOT NULL, source_digest TEXT NOT NULL,
 plan_key TEXT NOT NULL, state TEXT NOT NULL, remote_id TEXT, updated_at TEXT NOT NULL);'''


def _draft(db, key, account, channel):
    if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
        raise PublishError('回复草稿 key 无效')
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='interaction_drafts'").fetchone()
    row = db.execute('SELECT * FROM interaction_drafts WHERE draft_key=?', (key,)).fetchone() if exists else None
    if row is None or (row['platform'], row['account'], row['channel_id']) != ('youtube', account, channel):
        raise PublishError('YouTube 草稿不存在或账号不符')
    if digest({'source': row['source_digest'], 'reply': row['reply']}) != key:
        raise PublishError('草稿正文与封存摘要不符')
    return dict(row)


def _binding(root, key, account, channel):
    with _lock(root), open_db(root) as db:
        db.execute(SCHEMA)
        draft = _draft(db, key, account, channel)
        row = db.execute('SELECT * FROM interaction_dispatches WHERE draft_key=?', (key,)).fetchone()
        if row is None or any(row[k] != draft[k] for k in
                              ('platform', 'account', 'channel_id', 'comment_id', 'source_digest')):
            raise PublishError('草稿尚未绑定发送计划或绑定已改变')
        binding = dict(row)
        original = db.execute('''SELECT * FROM interaction_items WHERE platform='youtube'
            AND account=? AND channel_id=? AND comment_id=?''',
            (account, channel, draft['comment_id'])).fetchone()
    intent = _checked_plan(root, binding['plan_key'], account, channel)
    if (original is None or intent['text'] != draft['reply']
            or intent['video_id'] != original['video_id']
            or intent['parent_thread_id'] != original['thread_id']
            or intent['parent_id'] != (original['parent_id'] or original['comment_id'])):
        raise PublishError('发送计划与回复草稿或原评论目标不符')
    return draft, binding, intent


def prepare(root, key, account, channel):
    root = _private_dir(root)
    with _lock(root), open_db(root) as db:
        draft = _draft(db, key, account, channel)
        row, fingerprint = source(db, 'youtube', account, channel, draft['comment_id'])
        if fingerprint != draft['source_digest']:
            raise PublishError('原评论已变化，须重新核对草稿')
        intent = make_intent(channel, row['video_id'], draft['reply'],
                             parent_id=row['parent_id'] or row['comment_id'], parent_thread_id=row['thread_id'])
        plan_key = digest(intent)
        db.execute(SCHEMA)
        collision = db.execute('SELECT draft_key FROM interaction_dispatches WHERE plan_key=? AND draft_key!=?',
                               (plan_key, key)).fetchone()
        if collision:
            raise PublishError('同一发送计划已绑定另一条原评论，须结合当前对话重新拟稿')
        path = _plan_path(root, plan_key)
        if path.exists():
            _checked_plan(root, plan_key, account, channel)
        else:
            write_json(path, {'account': account, 'intent': intent})
        old = db.execute('SELECT * FROM interaction_dispatches WHERE draft_key=?', (key,)).fetchone()
        if old and old['plan_key'] != plan_key:
            raise PublishError('草稿已绑定其他计划，禁止改绑')
        db.execute('INSERT OR IGNORE INTO interaction_dispatches VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (key, 'youtube', account, channel, row['comment_id'], fingerprint,
                    plan_key, 'prepared', None, now()))
    return {'state': old['state'] if old else 'prepared', 'draft_key': key, 'plan_key': plan_key,
            'authorized': False, 'sent': False, 'reply_parent_id': intent['parent_id']}


def authorize(root, key, account, channel, statement):
    root = _private_dir(root)
    draft, binding, intent = _binding(root, key, account, channel)
    if not isinstance(statement, str) or not statement.strip() or len(statement) > 4000:
        raise PublishError('须提供实际用户授权原话')
    with account_lock(root, channel), _lock(root), open_db(root) as db:
        if (root / (_receipt_key(intent) + '.json')).exists():
            raise PublishError('原计划已有发送尝试，请先只读恢复，不能重新授权覆盖状态')
        _, fingerprint = source(db, 'youtube', account, channel, draft['comment_id'])
        if fingerprint != draft['source_digest']:
            raise PublishError('原评论已变化，授权不能沿用旧草稿')
        # This records an actual user decision; nonempty text is not proof of it.
        write_json(root / ('grant-' + binding['plan_key'] + '.json'), {
            'account': account, 'intent_digest': digest(intent), 'user_statement': statement.strip(),
            'authorized_at': now(), 'draft_key': key, 'source_digest': draft['source_digest'],
        })
        db.execute("UPDATE interaction_dispatches SET state='authorized',updated_at=? WHERE draft_key=?",
                   (now(), key))
    return {'state': 'authorized', 'draft_key': key, 'plan_key': binding['plan_key'], 'sent': False}


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is not None:
            return result
    except (AttributeError, TypeError, ValueError):
        pass
    raise PublishError('评论时间不足以核对后续回复')


def _guard(root, draft, binding, client, intent):
    with _lock(root), open_db(root) as db:
        _, fingerprint = source(db, 'youtube', draft['account'], draft['channel_id'], draft['comment_id'])
        if fingerprint != draft['source_digest']:
            raise PublishError('原评论缓存已变化，停止发送')
        others = db.execute('''SELECT plan_key FROM interaction_dispatches WHERE platform='youtube'
            AND account=? AND channel_id=? AND comment_id=? AND plan_key!=?''',
            (draft['account'], draft['channel_id'], draft['comment_id'], binding['plan_key'])).fetchall()
    for other in others:
        previous = _checked_plan(root, other['plan_key'], draft['account'], draft['channel_id'])
        if (root / (_receipt_key(previous) + '.json')).exists():
            raise PublishError('该原评论已有其他发送尝试，先查询恢复，不能换文案重发')
    # A reply to a child comment still attaches to the top-level comment on YouTube.
    original = _get_one(client, 'comments', draft['comment_id'])
    snippet = original.get('snippet') if isinstance(original, dict) else None
    expected_parent = None if draft['comment_id'] == intent['parent_id'] else intent['parent_id']
    if (not isinstance(snippet, dict) or original.get('id') != draft['comment_id']
            or (snippet.get('parentId') or None) != expected_parent):
        raise PublishError('平台原评论已删除或父级不符')
    author = snippet.get('authorChannelId')
    with _lock(root), open_db(root) as db:
        row, fingerprint = source(db, 'youtube', draft['account'], draft['channel_id'], draft['comment_id'])
    if (fingerprint != draft['source_digest'] or not isinstance(author, dict)
            or not row['author_channel'] or author.get('value') != row['author_channel']
            or author.get('value') == intent['channel_id']
            or snippet.get('textOriginal') != row['body']):
        raise PublishError('平台原评论正文或作者已变化')
    since = _stamp(snippet.get('publishedAt')) if expected_parent else None
    token = None
    for _ in range(10):
        page = replies(client, intent['parent_id'], page_token=token, page_size=100)
        for item in page['items']:
            rs = item.get('snippet') if isinstance(item, dict) else None
            if (not isinstance(rs, dict) or not isinstance(rs.get('authorChannelId'), dict)
                    or rs.get('parentId') != intent['parent_id']):
                raise PublishError('子回复身份不完整，不能排除本人已回复')
            if rs['authorChannelId'].get('value') == intent['channel_id']:
                if since is None or _stamp(rs.get('publishedAt')) >= since:
                    raise PublishError('本人已有后续回复，先核对对话，停止重复发送')
        token = page['next_page_token']
        if not token:
            break
    else:
        raise PublishError('子回复尚未查完，不能排除本人已回复')


def execute(root, key, account, channel, client, *, read_only=False):
    root = _private_dir(root)
    draft, binding, intent = _binding(root, key, account, channel)
    if read_only:
        receipt = reconcile(client, intent, state_dir=root)
    else:
        grant = read_json(root / ('grant-' + binding['plan_key'] + '.json'))
        if (grant.get('account') != account or grant.get('intent_digest') != binding['plan_key']
                or grant.get('draft_key') != key or grant.get('source_digest') != draft['source_digest']
                or not isinstance(grant.get('user_statement'), str) or not grant['user_statement'].strip()):
            raise PublishError('缺少绑定该草稿和原评论的实际授权')
        receipt = send(client, intent, approved_digest=binding['plan_key'], state_dir=root, allow_write=True,
                       before_send=lambda c, i: _guard(root, draft, binding, c, i))
    state = receipt['state']
    if state != 'not_attempted':
        with _lock(root), open_db(root) as db:
            db.execute('UPDATE interaction_dispatches SET state=?,remote_id=?,updated_at=? WHERE draft_key=?',
                       (state, receipt.get('remote_id'), now(), key))
    projection = {'recorded': False}
    if state == 'verified' and receipt.get('reconciliation') not in {'read_failed', 'not_verified'}:
        try:
            projection = record_reply(root, account, intent, receipt, source_comment_id=draft['comment_id'])
        except (PublishError, OSError):
            projection = {'recorded': False, 'reason': 'inbox_projection_failed'}
    return {**receipt, 'draft_key': key, 'plan_key': binding['plan_key'], 'inbox': projection}


def run_cli(argv=None, *, client_factory=APIClient):
    parser = argparse.ArgumentParser(description='YouTube 统一草稿接力；send 公开写入，reconcile 只读')
    for name in ('profile', 'account', 'state-dir'):
        parser.add_argument('--' + name, required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('prepare', 'authorize', 'send', 'reconcile', 'status'):
        sub = commands.add_parser(action)
        sub.add_argument('--key', required=True, help='interaction_drafts 的草稿 key')
        if action == 'authorize':
            sub.add_argument('--user-statement-file', required=True)
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    channel = _account_channel(profile, args.account, writing=args.action == 'send')
    root = _private_dir(args.state_dir)
    if args.action == 'prepare':
        return prepare(root, args.key, args.account, channel)
    if args.action == 'authorize':
        path = Path(args.user_statement_file)
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise PublishError('授权文件须为私有普通文件，权限 0600')
        return authorize(root, args.key, args.account, channel, path.read_text(encoding='utf-8'))
    if args.action == 'status':
        _, binding, intent = _binding(root, args.key, args.account, channel)
        path = root / (_receipt_key(intent) + '.json')
        receipt = read_json(path) if path.exists() else {'state': 'not_attempted'}
        return {**receipt, 'draft_key': args.key, 'plan_key': binding['plan_key']}
    bound, token = config(profile, {'platform': 'youtube', 'account': args.account}, writing=args.action == 'send')
    if bound != channel:
        raise PublishError('YouTube 频道绑定不一致')
    return execute(root, args.key, args.account, channel, client_factory(token), read_only=args.action == 'reconcile')


def main():
    try:
        print(json.dumps(run_cli(), ensure_ascii=False))
    except (PublishError, EnvironmentError, OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
