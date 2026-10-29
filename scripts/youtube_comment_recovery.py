#!/usr/bin/env python3
"""Bounded GET-only search for a lost comment response. Never adopts or resends."""
from datetime import datetime, timedelta, timezone
import argparse
import json
import sys
import uuid

from environment import EnvironmentError, load_profile
from interaction_import import _safe_state_dir
from publish_core import PublishError, digest, now, read_json, write_json
from youtube_adapter import APIClient, config, identity
from youtube_comments_candidate import (COMMENT_ID, SCOPE, _account_channel, _checked_plan,
    _receipt_key, _verified_remote, account_lock, make_intent, replies, threads)


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise PublishError('发送尝试或候选时间无效，不推断恢复身份') from exc


def missing_record(root, intent):
    if not isinstance(intent, dict) or set(intent) != {'channel_id', 'video_id', 'text', 'parent_id', 'parent_thread_id', 'action'}:
        raise PublishError('评论计划格式无效')
    checked = make_intent(intent.get('channel_id'), intent.get('video_id'), intent.get('text'),
                         parent_id=intent.get('parent_id'), parent_thread_id=intent.get('parent_thread_id'))
    if checked != intent:
        raise PublishError('评论计划无效')
    path = root / (_receipt_key(intent) + '.json')
    if not path.exists():
        raise PublishError('没有原发送尝试，不为未发送计划搜索候选')
    receipt = read_json(path)
    if not isinstance(receipt, dict) or receipt.get('intent_digest') != digest(intent) or receipt.get('state') != 'unknown':
        raise PublishError('仅允许原计划结果不明时搜索，保留已有核验记录')
    if receipt.get('remote_id') and receipt.get('thread_id'):
        raise PublishError('已有完整远端身份，使用原 reconcile 入口')
    stamp(receipt.get('attempted_at'))
    return receipt


def search(client, intent, *, state_dir, max_pages=3, clock=None):
    if type(max_pages) is not int or not 1 <= max_pages <= 5:
        raise PublishError('只读恢复页数须为 1 至 5')
    root = _safe_state_dir(state_dir)
    missing_record(root, intent)
    with account_lock(root, intent['channel_id']):
        receipt = missing_record(root, intent)
        attempted = stamp(receipt['attempted_at'])
        current = (clock or (lambda: datetime.now(timezone.utc)))()
        if attempted > current:
            raise PublishError('发送尝试时间在未来，停止恢复')
        # A deliberately narrow search window. Outside it remains unknown.
        lower, upper = attempted - timedelta(seconds=60), attempted + timedelta(minutes=10)
        identity(client, intent['channel_id'])
        if SCOPE not in client.granted_scopes():
            raise PublishError('缺少 YouTube 评论权限')
        token = None
        seen_tokens, seen_ids = set(), set()
        candidates = []
        pages = 0
        probes = 0
        complete = False
        reason = 'page_budget_exhausted'
        reads_failed = False
        try:
            for _ in range(max_pages):
                page = (replies(client, intent['parent_id'], page_token=token, page_size=100)
                        if intent['parent_id'] else
                        threads(client, video_id=intent['video_id'], page_token=token, page_size=100))
                pages += 1
                for row in page['items']:
                    thread_id = intent['parent_thread_id'] if intent['parent_id'] else row.get('id')
                    item = row if intent['parent_id'] else row.get('snippet', {}).get('topLevelComment', {})
                    snippet = item.get('snippet', {})
                    remote = item.get('id')
                    author = snippet.get('authorChannelId', {})
                    if (not isinstance(remote, str) or not COMMENT_ID.fullmatch(remote)
                            or not isinstance(thread_id, str) or not COMMENT_ID.fullmatch(thread_id)
                            or remote in seen_ids or not isinstance(author, dict)
                            or (receipt.get('remote_id') and remote != receipt['remote_id'])
                            or (receipt.get('thread_id') and thread_id != receipt['thread_id'])
                            or author.get('value') != intent['channel_id']
                            or snippet.get('textOriginal') != intent['text']
                            or (snippet.get('parentId') or None) != intent['parent_id']):
                        continue
                    seen_ids.add(remote)
                    published = stamp(snippet.get('publishedAt'))
                    if not lower <= published <= min(upper, current):
                        continue
                    # Read the actual thread and actual comment independently.
                    probe = {'remote_id': remote, 'thread_id': thread_id}
                    probes += 1
                    if _verified_remote(client, intent, probe):
                        candidates.append({**probe, 'published_at': published.isoformat()})
                    if probes >= 5:
                        reason = 'verification_budget_exhausted'
                        break
                if probes >= 5:
                    break
                token = page['next_page_token']
                if token is None:
                    complete, reason = True, 'bounded_pages_complete'
                    break
                if token in seen_tokens:
                    reason = 'repeated_page_token'
                    break
                seen_tokens.add(token)
        except (PublishError, OSError, ValueError, KeyError, TypeError, AttributeError):
            reads_failed, reason = True, 'read_failed'
        result = {'schema': 'youtube-comment-recovery.v1', 'recorded_at': now(),
                  'intent_digest': digest(intent), 'state': 'unknown', 'needs_review': True,
                  'platform_writes': 0, 'receipt_changed': False, 'pages': pages,
                  'verification_probes': probes,
                  'max_pages': max_pages, 'pages_complete': complete, 'reason': reason,
                  'read_failed': reads_failed, 'candidates': candidates,
                  'search_window': {'from': lower.isoformat(), 'to': upper.isoformat()},
                  'note': '候选不证明来自本次尝试；零条、单条或多条均不自动认领，不授权重发'}
        # Preserve the original receipt byte-for-byte. Evidence is a new private file.
        path = root / ('comment-recovery-' + uuid.uuid4().hex + '.json')
        write_json(path, result)
        return {**result, 'evidence_file': str(path)}


def run_cli(argv=None, *, client_factory=APIClient):
    p = argparse.ArgumentParser(description='丢失评论身份后的有限只读候选查询；不认领、不重发')
    for field in ('profile', 'account', 'state-dir', 'key'):
        p.add_argument('--' + field, required=True)
    p.add_argument('--source-account', help='同频道历史计划的原账号别名，仅用于只读')
    p.add_argument('--max-pages', type=int, choices=range(1, 6), default=3)
    a = p.parse_args(argv)
    profile = load_profile(a.profile)
    channel = _account_channel(profile, a.account)
    root = _safe_state_dir(a.state_dir)
    intent = _checked_plan(root, a.key, a.source_account or a.account, channel)
    missing_record(root, intent)  # Reject invalid/unattempted plans before OAuth refresh.
    configured_channel, token = config(profile, {'platform': 'youtube', 'account': a.account}, writing=False)
    if configured_channel != channel:
        raise PublishError('频道绑定不一致')
    return search(client_factory(token), intent, state_dir=root, max_pages=a.max_pages)


def main():
    try:
        print(json.dumps(run_cli(), ensure_ascii=False))
        return 0
    except (PublishError, EnvironmentError, OSError, ValueError):
        print('只读恢复未完成；保留原计划和回执，未发送或重发评论。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
