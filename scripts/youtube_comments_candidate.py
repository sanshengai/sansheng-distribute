#!/usr/bin/env python3
"""YouTube comment plans, explicit sends and read-only reconciliation.

The caller must bind a real user decision to an intent digest. Only use with
an OAuth token for the intended channel and a private, shared state directory.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import argparse
import sys
import urllib.parse
from contextlib import contextmanager

from environment import EnvironmentError, load_profile
from publish_core import PublishError, digest, now, read_json, write_json
from youtube_adapter import API, CHANNEL_ID, VIDEO_ID, APIClient, config, identity, json_body

SCOPE = 'https://www.googleapis.com/auth/youtube.force-ssl'
COMMENT_ID = re.compile(r'^[A-Za-z0-9_.-]{1,200}$')


def _id(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PublishError(f'{label}无效')
    return value


def _page(client, path, params):
    status, _, body = client.call('GET', API + path + '?' + urllib.parse.urlencode(params))
    if status != 200:
        raise PublishError('YouTube 评论读取失败')
    data = json_body(body)
    if not isinstance(data.get('items'), list):
        raise PublishError('YouTube 评论响应缺少列表')
    token = data.get('nextPageToken')
    if token is not None and (not isinstance(token, str) or not token or len(token) > 512):
        raise PublishError('YouTube 分页令牌无效')
    return {'items': data['items'], 'next_page_token': token}


def threads(client, *, channel_id=None, video_id=None, page_token=None, page_size=50):
    """Read one page; caller must follow next_page_token until absent."""
    if (channel_id is None) == (video_id is None):
        raise PublishError('必须且只能选择频道或视频')
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise PublishError('分页大小无效')
    params = {'part': 'snippet', 'maxResults': page_size, 'textFormat': 'plainText'}
    if channel_id is not None:
        params['allThreadsRelatedToChannelId'] = _id(channel_id, CHANNEL_ID, '频道 ID')
    else:
        params['videoId'] = _id(video_id, VIDEO_ID, '视频 ID')
    if page_token is not None:
        if not isinstance(page_token, str) or not page_token or len(page_token) > 512:
            raise PublishError('分页令牌无效')
        params['pageToken'] = page_token
    return _page(client, '/youtube/v3/commentThreads', params)


def replies(client, parent_id, *, page_token=None, page_size=50):
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise PublishError('分页大小无效')
    params = {'part': 'snippet', 'parentId': _id(parent_id, COMMENT_ID, '评论 ID'),
              'maxResults': page_size, 'textFormat': 'plainText'}
    if page_token is not None:
        if not isinstance(page_token, str) or not page_token or len(page_token) > 512:
            raise PublishError('分页令牌无效')
        params['pageToken'] = page_token
    return _page(client, '/youtube/v3/comments', params)


def make_intent(channel_id, video_id, text, *, parent_id=None, parent_thread_id=None):
    """Create reviewable intent. Digest must be approved for this exact intent."""
    _id(channel_id, CHANNEL_ID, '频道 ID'); _id(video_id, VIDEO_ID, '视频 ID')
    if (parent_id is None) != (parent_thread_id is None):
        raise PublishError('回复须同时提供父评论 ID 和所属线程 ID')
    if parent_id is not None:
        _id(parent_id, COMMENT_ID, '评论 ID')
        _id(parent_thread_id, COMMENT_ID, '评论线程 ID')
    if not isinstance(text, str) or not text.strip() or len(text) > 10000 or '\x00' in text:
        raise PublishError('评论内容无效')
    return {'channel_id': channel_id, 'video_id': video_id, 'parent_id': parent_id,
            'parent_thread_id': parent_thread_id,
            'text': text, 'action': 'reply' if parent_id else 'comment'}


def _get_one(client, resource, item_id, part='snippet'):
    result = _page(client, '/youtube/v3/' + resource,
                   {'part': part, 'id': item_id, 'textFormat': 'plainText'} if resource != 'videos'
                   else {'part': part, 'id': item_id})
    return result['items'][0] if len(result['items']) == 1 else None


def _preflight(client, intent):
    if SCOPE not in client.granted_scopes():
        raise PublishError('缺少 YouTube 评论权限')
    identity(client, intent['channel_id'])
    video = _get_one(client, 'videos', intent['video_id'])
    video_snippet = video.get('snippet') if isinstance(video, dict) else None
    if not isinstance(video_snippet, dict) or video_snippet.get('channelId') != intent['channel_id']:
        raise PublishError('视频不属于目标频道')
    if intent['parent_id']:
        thread = _get_one(client, 'commentThreads', intent['parent_thread_id'])
        snippet = thread.get('snippet') if isinstance(thread, dict) else None
        if not isinstance(snippet, dict):
            raise PublishError('目标评论线程响应无效')
        top = snippet.get('topLevelComment', {})
        if (not isinstance(thread, dict) or thread.get('id') != intent['parent_thread_id']
                or snippet.get('channelId') != intent['channel_id']
                or snippet.get('videoId') != intent['video_id'] or not isinstance(top, dict)
                or top.get('id') != intent['parent_id']
                or snippet.get('canReply') is not True):
            raise PublishError('目标评论与视频不匹配或不允许回复')


def _private_dir(path):
    root = Path(path)
    if not root.is_absolute() or root.is_symlink():
        raise PublishError('评论状态目录须为本机绝对路径')
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise PublishError('评论状态目录权限须为 0700')
    return root


def _receipt_key(intent):
    return hashlib.sha256(json.dumps(intent, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@contextmanager
def account_lock(root, channel):
    """Serialize comment writes for one channel in the shared local ledger."""
    _id(channel, CHANNEL_ID, '频道 ID')
    path = root / ('channel-' + channel + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _verified_remote(client, intent, receipt):
    remote, thread_id = receipt.get('remote_id'), receipt.get('thread_id')
    if (not isinstance(remote, str) or not COMMENT_ID.fullmatch(remote)
            or not isinstance(thread_id, str) or not COMMENT_ID.fullmatch(thread_id)):
        return False
    if intent['parent_id'] and thread_id != intent['parent_thread_id']:
        return False
    thread = _get_one(client, 'commentThreads', thread_id)
    ts = thread.get('snippet') if isinstance(thread, dict) else None
    item = _get_one(client, 'comments', remote)
    cs = item.get('snippet') if isinstance(item, dict) else None
    if not isinstance(ts, dict) or not isinstance(cs, dict):
        return False
    top, author = ts.get('topLevelComment'), cs.get('authorChannelId')
    return (thread.get('id') == thread_id and ts.get('channelId') == intent['channel_id']
            and ts.get('videoId') == intent['video_id'] and isinstance(top, dict)
            and top.get('id') == (intent['parent_id'] or remote)
            and item.get('id') == remote and cs.get('textOriginal') == intent['text']
            and (cs.get('parentId') or None) == intent['parent_id']
            and isinstance(author, dict) and author.get('value') == intent['channel_id'])


def reconcile(client, intent, *, state_dir):
    """GET only; missing IDs or failed readback never authorize another POST."""
    root = _private_dir(state_dir)
    path = root / (_receipt_key(intent) + '.json')
    with account_lock(root, intent['channel_id']):
        if not path.exists():
            return {'state': 'not_attempted', 'intent_digest': digest(intent)}
        receipt = read_json(path)
        if receipt.get('intent_digest') != digest(intent):
            raise PublishError('评论回执与计划不符')
        identity(client, intent['channel_id'])
        if SCOPE not in client.granted_scopes():
            raise PublishError('缺少 YouTube 评论权限')
        if not receipt.get('remote_id') or not receipt.get('thread_id'):
            return {**receipt, 'reconciliation': 'missing_remote_identity', 'currently_verified': None}
        try:
            verified = _verified_remote(client, intent, receipt)
        except (PublishError, OSError, ValueError, TypeError, KeyError, AttributeError):
            return {**receipt, 'reconciliation': 'read_failed', 'currently_verified': None}
        if verified:
            receipt.update(state='verified', last_verified_at=now())
            write_json(path, receipt)
            return {**receipt, 'reconciliation': 'verified', 'currently_verified': True}
        # Preserve historical success, but do not report it as current visibility.
        return {**receipt, 'reconciliation': 'not_verified', 'currently_verified': False}


def send(client, intent, *, approved_digest, state_dir, allow_write=False, before_send=None):
    """One attempt only. Uncertain outcomes remain blocked, never auto-retry.

    This digest is only a binding, not proof of user approval; caller must obtain
    actual approval and pass its digest. State must be shared by all executors.
    """
    if allow_write is not True:
        raise PublishError('评论写入未启用')
    if not isinstance(intent, dict) or set(intent) != {'channel_id','video_id','text','parent_id','parent_thread_id','action'}:
        raise PublishError('评论交接格式无效')
    expected = make_intent(intent['channel_id'], intent['video_id'], intent['text'],
                           parent_id=intent['parent_id'], parent_thread_id=intent['parent_thread_id'])
    if intent != expected or not isinstance(approved_digest, str) or approved_digest != digest(intent):
        raise PublishError('评论内容与授权不一致')
    root = _private_dir(state_dir)
    key = _receipt_key(intent)
    receipt_path = root / (key + '.json')
    lock_path = root / (key + '.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'a+') as lock, account_lock(root, intent['channel_id']):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if receipt_path.exists():
                receipt = read_json(receipt_path)
                if receipt.get('intent_digest') != digest(intent):
                    raise PublishError('评论回执与计划不符')
                return receipt
            _preflight(client, intent)
            if before_send is not None:
                before_send(client, intent)
            receipt = {'state': 'unknown', 'intent_digest': digest(intent), 'attempted_at': now()}
            write_json(receipt_path, receipt)
            # Ensure the write-ahead record reaches the directory before POST.
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            if intent['parent_id']:
                endpoint = '/youtube/v3/comments?part=snippet'
                body = {'snippet': {'parentId': intent['parent_id'], 'textOriginal': intent['text']}}
            else:
                endpoint = '/youtube/v3/commentThreads?part=snippet'
                body = {'snippet': {'channelId': intent['channel_id'], 'videoId': intent['video_id'],
                                    'topLevelComment': {'snippet': {'textOriginal': intent['text']}}}}
            try:
                status, _, data = client.call('POST', API + endpoint,
                    headers={'Content-Type': 'application/json'},
                    data=json.dumps(body, ensure_ascii=False).encode('utf-8'))
                result = json_body(data)
                if status not in (200, 201): return receipt
                comment = result if intent['parent_id'] else result.get('snippet', {}).get('topLevelComment', {})
                remote = comment.get('id') if isinstance(comment, dict) else None
                thread_id = intent['parent_thread_id'] if intent['parent_id'] else result.get('id')
                if (not isinstance(remote, str) or not COMMENT_ID.fullmatch(remote)
                        or not isinstance(thread_id, str) or not COMMENT_ID.fullmatch(thread_id)):
                    return receipt
                receipt['remote_id'] = remote
                receipt['thread_id'] = thread_id
                write_json(receipt_path, receipt)
                if _verified_remote(client, intent, receipt):
                    receipt['state'] = 'verified'
                    write_json(receipt_path, receipt)
            except (PublishError, OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
            return receipt
    except OSError as exc:
        raise PublishError('评论账本不可用') from None


def _account_channel(profile, account, *, writing=False):
    accounts = profile.get('accounts')
    binding = accounts.get(account) if isinstance(accounts, dict) else None
    if not isinstance(binding, dict) or 'youtube' not in binding.get('platforms', []):
        raise PublishError('YouTube 账号未绑定')
    if writing and binding.get('restricted'):
        raise PublishError('账号受限，仅允许只读核验')
    return _id(binding.get('public_identities', {}).get('youtube'), CHANNEL_ID, '频道 ID')


def _plan_path(root, key):
    if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
        raise PublishError('评论计划 key 无效')
    return root / ('plan-' + key + '.json')


def _checked_plan(root, key, account, channel):
    plan = read_json(_plan_path(root, key))
    intent = plan.get('intent')
    if plan.get('account') != account or not isinstance(intent, dict):
        raise PublishError('评论计划账号不符')
    if intent.get('channel_id') != channel or digest(intent) != key:
        raise PublishError('评论计划内容或频道已改变')
    checked = make_intent(intent.get('channel_id'), intent.get('video_id'), intent.get('text'),
                          parent_id=intent.get('parent_id'), parent_thread_id=intent.get('parent_thread_id'))
    if checked != intent:
        raise PublishError('评论计划内容无效')
    return intent


def _cli_parser():
    p = argparse.ArgumentParser(description='YouTube 评论候选；实际发送必须事先获得用户批准')
    p.add_argument('--profile', required=True, help='现有分发私有 profile 路径')
    p.add_argument('--account', required=True, help='profile 中绑定 YouTube 的账号名称')
    p.add_argument('--state-dir', required=True, help='私有评论状态目录，须 0700；所有执行者必须共用')
    sub = p.add_subparsers(dest='action', required=True)
    read = sub.add_parser('threads', help='读取频道或指定视频的一页评论')
    read.add_argument('--video'); read.add_argument('--page-token'); read.add_argument('--page-size', type=int, default=50)
    read = sub.add_parser('replies', help='读取指定评论的一页子回复')
    read.add_argument('--parent', required=True); read.add_argument('--page-token'); read.add_argument('--page-size', type=int, default=50)
    prepare = sub.add_parser('prepare', help='从文本文件封存一条待审核评论，不连接平台')
    prepare.add_argument('--video', required=True); prepare.add_argument('--parent'); prepare.add_argument('--parent-thread')
    prepare.add_argument('--text-file', required=True)
    approve = sub.add_parser('authorize', help='记录用户真实授权原话；摘要本身不是授权')
    approve.add_argument('--key', required=True); approve.add_argument('--user-statement-file', required=True)
    submit = sub.add_parser('send', help='只提交已授权计划一次；结果未知禁止自动重发')
    submit.add_argument('--key', required=True)
    status = sub.add_parser('status', help='只读查看本地一次性发送记录，不查询平台')
    status.add_argument('--key', required=True)
    check = sub.add_parser('reconcile', help='只读重新查询已提交评论，不重新发送')
    check.add_argument('--key', required=True)
    check.add_argument('--source-account', help='历史计划中的原账号别名，仅用于只读核验同一频道')
    return p


def run_cli(argv=None, *, client_factory=APIClient):
    args = _cli_parser().parse_args(argv)
    profile = load_profile(args.profile)
    channel = _account_channel(profile, args.account, writing=args.action == 'send')
    root = _private_dir(args.state_dir)
    if args.action == 'prepare':
        try:
            text = Path(args.text_file).read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            raise PublishError('无法读取评论文本文件') from None
        intent = make_intent(channel, args.video, text, parent_id=args.parent, parent_thread_id=args.parent_thread)
        key = digest(intent)
        plan_path = _plan_path(root, key)
        if plan_path.exists():
            _checked_plan(root, key, args.account, channel)
        else:
            write_json(plan_path, {'account': args.account, 'intent': intent})
        return {'state': 'prepared', 'key': key, 'intent': intent}
    if args.action == 'authorize':
        intent = _checked_plan(root, args.key, args.account, channel)
        try:
            statement = Path(args.user_statement_file).read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            raise PublishError('无法读取实际用户授权原话') from None
        if not statement or len(statement) > 4000:
            raise PublishError('须提供明确的实际用户授权原话')
        # The caller must only create this record after the user has actually
        # approved this exact prepared intent. Neither this statement nor its
        # digest independently proves that the user granted permission.
        write_json(root / ('grant-' + args.key + '.json'),
                   {'account': args.account, 'intent_digest': digest(intent),
                    'user_statement': statement, 'authorized_at': now()})
        return {'state': 'authorized', 'key': args.key}
    if args.action == 'status':
        intent = _checked_plan(root, args.key, args.account, channel)
        path = root / (_receipt_key(intent) + '.json')
        return read_json(path) if path.exists() else {'state': 'not_attempted', 'key': args.key}
    intent = grant = None
    if args.action in {'send', 'reconcile'}:
        source_account = (args.source_account or args.account) if args.action == 'reconcile' else args.account
        intent = _checked_plan(root, args.key, source_account, channel)
        if args.action == 'send':
            grant = read_json(root / ('grant-' + args.key + '.json'))
            if (grant.get('account') != args.account or grant.get('intent_digest') != args.key
                    or not isinstance(grant.get('user_statement'), str) or not grant['user_statement'].strip()):
                raise PublishError('缺少绑定该评论的实际用户授权')
    target = {'platform': 'youtube', 'account': args.account}
    configured_channel, token = config(profile, target, writing=args.action == 'send')
    if configured_channel != channel:
        raise PublishError('频道绑定不一致')
    client = client_factory(token)
    if args.action in {'threads', 'replies'}:
        identity(client, channel)
        if SCOPE not in client.granted_scopes():
            raise PublishError('缺少 YouTube 评论权限')
        if args.action == 'threads':
            return threads(client, video_id=args.video, page_token=args.page_token, page_size=args.page_size) if args.video else \
                   threads(client, channel_id=channel, page_token=args.page_token, page_size=args.page_size)
        return replies(client, args.parent, page_token=args.page_token, page_size=args.page_size)
    if args.action == 'reconcile':
        result = reconcile(client, intent, state_dir=root)
        return {**result, 'source_account': source_account, 'execution_account': args.account}
    receipt = send(client, intent, approved_digest=args.key, state_dir=root, allow_write=True)
    if receipt.get('state') == 'verified' and intent.get('parent_id'):
        from interaction_receipts import record_reply
        try:
            projection = record_reply(root, args.account, intent, receipt)
        except (PublishError, OSError):
            projection = {'recorded': False, 'reason': 'inbox_projection_failed'}
        return {**receipt, 'inbox': projection}
    return receipt


def main():
    try:
        print(json.dumps(run_cli(), ensure_ascii=False))
    except (PublishError, EnvironmentError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
