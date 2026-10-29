#!/usr/bin/env python3
"""Loopback author review desk. Credentials stay in existing backend adapters."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sys
import threading

from environment import load_profile
from interaction_drafts import SCHEMA as DRAFT_SCHEMA, save_review, source
from interaction_import import PLATFORMS, _safe_state_dir, _string
from interaction_inbox import _lock, open_db
from publish_core import PublishError, digest, now
import interaction_youtube_queue as youtube
from youtube_adapter import APIClient, config as youtube_config
from youtube_comments_candidate import _account_channel

IDENTITY = ('platform', 'account', 'channel_id', 'comment_id')
LABELS = {'youtube': 'YouTube', 'x': 'X', 'weibo': '微博', 'bilibili': 'B站',
          'douyin': '抖音', 'wechat': '公众号', 'wechat_channels': '微信视频号',
          'alipay': '支付宝生活号', 'xiaohongshu': '小红书'}
SCHEMA = '''CREATE TABLE IF NOT EXISTS interaction_review_batches (
 batch_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, confirmed_at TEXT NOT NULL, nonce TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS interaction_review_results (
 batch_id TEXT NOT NULL, draft_key TEXT NOT NULL, state TEXT NOT NULL,
 result_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(batch_id,draft_key));
CREATE TABLE IF NOT EXISTS interaction_review_handoffs (
 platform TEXT NOT NULL,account TEXT NOT NULL,channel_id TEXT NOT NULL,comment_id TEXT NOT NULL,
 batch_id TEXT NOT NULL,draft_key TEXT NOT NULL,state TEXT NOT NULL,claimed_at TEXT NOT NULL,
 reply_id TEXT,PRIMARY KEY(platform,account,channel_id,comment_id));'''


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise PublishError('审核身份无效，请刷新')
    return value


class ReviewDesk:
    def __init__(self, root, *, profile=None, account=None, client_factory=APIClient):
        self.root = _safe_state_dir(root)
        if not (self.root / 'interaction-inbox.sqlite3').is_file():
            raise PublishError('请先接入真实互动收件箱')
        if bool(profile) != bool(account):
            raise PublishError('YouTube 的 profile 和 account 须同时指定')
        self.profile_path, self.account = profile, account
        self.client_factory = client_factory
        self.channel = None
        if profile:
            self.channel = _account_channel(load_profile(profile), account, writing=True)
        self.mutation_lock = threading.Lock()
        with _lock(self.root), open_db(self.root) as db:
            db.executescript(DRAFT_SCHEMA + SCHEMA)

    def route(self, platform, account, channel):
        if (platform, account, channel) == ('youtube', self.account, self.channel):
            return True, 'YouTube 官方 API；发送前重新核对原评论与已有回复'
        return False, '尚未接通受控发送；可以确认建议并复制到平台，不能一键发送'

    @staticmethod
    def _handoffable(db, item):
        if item['platform'] != 'alipay':
            return False
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='interaction_web_provenance'").fetchone():
            return False
        return bool(db.execute('''SELECT 1 FROM interaction_web_provenance
            WHERE platform=? AND account=? AND channel_id=? AND comment_id=?
            AND identity_kind='alipay_dom' ''', tuple(item[k] for k in IDENTITY)).fetchone())

    @staticmethod
    def _current_draft(db, item):
        _, fingerprint = source(db, *(item[k] for k in IDENTITY))
        draft = db.execute('''SELECT * FROM interaction_drafts WHERE platform=? AND account=?
            AND channel_id=? AND comment_id=?''', tuple(item[k] for k in IDENTITY)).fetchone()
        if (not draft or fingerprint != item['source_digest'] or
                any(draft[k] != item[k] for k in ('draft_key', 'source_digest', 'reply')) or
                draft['created_at'] != item['draft_created_at']):
            raise PublishError('已确认文案或原留言已变化，请重新确认')

    def snapshot(self):
        rows = []
        with _lock(self.root), open_db(self.root) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            all_items = db.execute('''SELECT * FROM interaction_items WHERE is_own=0
                ORDER BY first_seen_at DESC,comment_id DESC LIMIT 501''').fetchall()
            for item in all_items[:500]:
                identity = tuple(item[k] for k in IDENTITY)
                draft = db.execute('''SELECT * FROM interaction_drafts WHERE platform=?
                    AND account=? AND channel_id=? AND comment_id=?''', identity).fetchone()
                original = {k: item[k] for k in IDENTITY}
                eligible, reason, fingerprint = False, '', None
                try:
                    _, fingerprint = source(db, *identity)
                    eligible = True
                except PublishError as exc:
                    reason = str(exc)
                handled = item['local_status'] == 'handled'
                verified, baseline = False, False
                if 'interaction_reply_receipts' in tables:
                    verified = bool(db.execute('''SELECT 1 FROM interaction_reply_receipts
                        WHERE platform=? AND account=? AND channel_id=? AND parent_id=?''', identity).fetchone())
                    handled = handled or verified
                if 'interaction_baselines' in tables:
                    baseline = bool(db.execute('''SELECT 1 FROM interaction_baselines
                        WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''', identity).fetchone())
                status_label = '已核验回复' if verified else '历史基线' if baseline else '已标记处理' if handled else None
                web = None
                if 'interaction_web_provenance' in tables:
                    web = db.execute('''SELECT * FROM interaction_web_provenance
                        WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''', identity).fetchone()
                    if web and handled and web['own_reply_seen'] and not verified:
                        status_label = '网页已有本人回复'
                        reason = '已在原对话看到本人回复；不是本次发送或官方 API 回执'
                if baseline and not verified:
                    reason = '历史留言仅作去重基线，不自动补回，也不代表已回复'
                if eligible and draft and draft['source_digest'] != fingerprint:
                    reason = '原评论已变化，请核对原文并重新确认建议'
                reply = draft['reply'] if draft else ''
                suggestion = None
                if not draft and fingerprint and 'interaction_suggestions' in tables:
                    saved = db.execute('''SELECT decision_json FROM interaction_suggestions
                        WHERE platform=? AND account=? AND channel_id=? AND comment_id=?
                        AND source_digest=? ORDER BY created_at DESC LIMIT 1''', (*identity, fingerprint)).fetchone()
                    if saved:
                        try:
                            suggestion = json.loads(saved['decision_json'])
                            if not isinstance(suggestion, dict) or not isinstance(suggestion.get('text'), str):
                                raise ValueError()
                            reply = suggestion['text']
                        except (ValueError, TypeError):
                            suggestion, reason = None, '已有建议记录格式无效，请重新拟稿'
                sendable, route_reason = self.route(*identity[:3])
                handoffable = self._handoffable(db, item)
                claim = db.execute('''SELECT state,batch_id FROM interaction_review_handoffs
                    WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''', identity).fetchone()
                if claim and claim['state'] in {'attempting', 'unknown'}:
                    eligible = False
                    reason = 'Agent 已开始发送；检查原结果后再处理，不能修改重发'
                    status_label = ('Agent 正在处理' if claim['state'] == 'attempting'
                                    else 'Agent 需要检查发送结果')
                elif claim and claim['state'] == 'verified':
                    handled = True
                    status_label = '本次回复已发送并核对'
                    reason = '专用浏览器发送后刷新原对话，已核对本人回复及其关联；不是官方 API 回执'
                if handoffable:
                    route_reason = '可交给当前 Agent 在专用浏览器发送；提交后等待执行，不代表已经发出'
                parent = item['parent_id'] or item['comment_id']
                context = [dict(r) for r in db.execute('''SELECT author_name,body,is_own
                    FROM interaction_items WHERE platform=? AND account=? AND channel_id=?
                    AND video_id=? AND (comment_id=? OR parent_id=?) AND comment_id!=?
                    ORDER BY published_at DESC LIMIT 6''',
                    (*identity[:3], item['video_id'], parent, parent, item['comment_id']))]
                rows.append({**original, 'item_key': digest(original), 'video_id': item['video_id'],
                             'author_name': item['author_name'] or '网友', 'comment': item['body'] or '',
                             'reply': reply, 'source_digest': fingerprint,
                             'draft_key': draft['draft_key'] if draft else None,
                             'draft_created_at': draft['created_at'] if draft else None,
                             'source_kind': web['identity_kind'] if web else None,
                             'work_title': web['work_title'] if web else None,
                             'eligible': eligible, 'reason': reason, 'sendable': sendable,
                             'handoffable': handoffable,
                             'handoff_state': claim['state'] if claim else None,
                             'route_reason': route_reason, 'suggestion': suggestion, 'context': context,
                             'published_at': item['published_at'], 'handled': handled, 'status_label': status_label})
            sources = [dict(r) for r in db.execute('''SELECT platform,account,state,last_success_at
                FROM interaction_sources ORDER BY platform,account''')]
            if 'interaction_web_provenance' in tables:
                for src in sources:
                    observed = db.execute('''SELECT MAX(observed_at) FROM interaction_web_provenance
                        WHERE platform=? AND account=?''', (src['platform'], src['account'])).fetchone()[0]
                    if observed:
                        src.update(web_observed_at=observed)
            discovered = 0
            if 'weibo_official_items' in tables:
                discovered = db.execute('''SELECT count(*) FROM weibo_official_items d
                    WHERE is_own=0 AND baseline=0 AND NOT EXISTS (SELECT 1 FROM interaction_items i
                    WHERE i.platform='weibo' AND i.account=d.account AND i.channel_id=d.uid
                    AND i.comment_id=d.comment_id)''').fetchone()[0]
            batches = []
            for b in db.execute('SELECT * FROM interaction_review_batches ORDER BY confirmed_at DESC LIMIT 10'):
                result = [json.loads(r[0]) for r in db.execute('''SELECT result_json
                    FROM interaction_review_results WHERE batch_id=? ORDER BY rowid''', (b['batch_id'],))]
                batches.append({'batch_id': b['batch_id'], 'confirmed_at': b['confirmed_at'],
                                'count': len(json.loads(b['payload_json'])),
                                'items': json.loads(b['payload_json']), 'results': result})
        return {'items': rows, 'truncated': len(all_items) > 500, 'sources': sources,
                'platforms': LABELS, 'pending_context': discovered, 'batches': batches,
                'checked_at': now(), 'note': '本机收件箱快照；刷新不代表重新扫描各平台'}

    def confirm(self, selections):
        if not isinstance(selections, list) or not 1 <= len(selections) <= 20:
            raise PublishError('每批确认须选择 1 至 20 条留言')
        seen, accepted, errors = set(), [], []
        # Validate the entire request shape before changing any local draft.
        for row in selections:
            if not isinstance(row, dict) or set(row) != set(IDENTITY) | {'source_digest', 'draft_key', 'reply'}:
                raise PublishError('审核字段不完整或包含未知字段')
            for k in IDENTITY:
                _string(row[k], k, 256)
            if row['platform'] not in PLATFORMS:
                raise PublishError('不支持的平台')
            _key(row['source_digest'])
            if row['draft_key'] is not None:
                _key(row['draft_key'])
            _string(row['reply'], '回复', 10000)
            identity = tuple(row[k] for k in IDENTITY)
            if identity in seen:
                raise PublishError('同一批不能重复选择相同留言')
            seen.add(identity)
        for row in selections:
            original = {k: row[k] for k in IDENTITY}
            try:
                result = save_review(self.root, *(row[k] for k in IDENTITY), row['reply'],
                                     expected_source=row['source_digest'], expected_draft=row['draft_key'])
                accepted.append({**original, 'source_digest': row['source_digest'],
                                 'draft_key': result['draft_key'], 'reply': row['reply'],
                                 'draft_created_at': result['created_at']})
            except PublishError as exc:
                errors.append({**original, 'state': 'blocked', 'reason': str(exc)})
        batch_id = None
        if accepted:
            # A new confirmation gets its own immutable receipt, even for equal text.
            stamp = now()
            nonce = secrets.token_hex(16)
            batch_id = digest({'items': accepted, 'confirmed_at': stamp, 'nonce': nonce})
            with _lock(self.root), open_db(self.root) as db:
                db.execute('INSERT INTO interaction_review_batches VALUES(?,?,?,?)',
                           (batch_id, json.dumps(accepted, ensure_ascii=False), stamp, nonce))
        sendable = sum(self.route(*(r[k] for k in IDENTITY[:3]))[0] for r in accepted)
        with _lock(self.root), open_db(self.root) as db:
            handoffable = sum(self._handoffable(db, r) for r in accepted)
        return {'batch_id': batch_id, 'confirmed': len(accepted), 'accepted': accepted,
                'sendable': sendable, 'handoffable': handoffable, 'errors': errors, 'social_writes': 0}

    def _batch(self, batch_id, *, for_send=True):
        _key(batch_id)
        with _lock(self.root), open_db(self.root) as db:
            batch = db.execute('SELECT * FROM interaction_review_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if batch is None:
            raise PublishError('未找到已确认的批次')
        items = json.loads(batch['payload_json'])
        if digest({'items': items, 'confirmed_at': batch['confirmed_at'], 'nonce': batch['nonce']}) != batch_id:
            raise PublishError('确认记录与封存内容不一致，停止发送')
        # Confirmation expires; no background sending after days of stale review.
        confirmed = datetime.fromisoformat(batch['confirmed_at'])
        age = (datetime.now(timezone.utc) - confirmed).total_seconds()
        if for_send and not 0 <= age <= 86400:
            raise PublishError('确认已超过24小时，请重新核对后确认')
        return items

    def _result(self, batch_id, item, state, reason, **extra):
        value = {**{k: item[k] for k in IDENTITY}, 'draft_key': item['draft_key'],
                 'state': state, 'reason': reason, **extra}
        with _lock(self.root), open_db(self.root) as db:
            db.execute('''INSERT INTO interaction_review_results VALUES(?,?,?,?,?)
                ON CONFLICT(batch_id,draft_key) DO UPDATE SET state=excluded.state,
                result_json=excluded.result_json,updated_at=excluded.updated_at''',
                (batch_id, item['draft_key'], state, json.dumps(value, ensure_ascii=False), now()))
        return value

    def dispatch(self, batch_id, *, reconcile=False):
        items = self._batch(batch_id, for_send=not reconcile)
        output = []
        for item in items:
            key, account, channel = item['draft_key'], item['account'], item['channel_id']
            can_send, reason = self.route(item['platform'], account, channel)
            if not can_send:
                with _lock(self.root), open_db(self.root) as db:
                    prior = db.execute('''SELECT result_json FROM interaction_review_results
                        WHERE batch_id=? AND draft_key=?''', (batch_id, key)).fetchone()
                    web = self._handoffable(db, item)
                    claim = db.execute('''SELECT state,batch_id,draft_key FROM interaction_review_handoffs WHERE
                        platform=? AND account=? AND channel_id=? AND comment_id=?''',
                        tuple(item[k] for k in IDENTITY)).fetchone()
                    error = None
                    if web and not reconcile and not claim:
                        try:
                            self._current_draft(db, item)
                        except PublishError as exc:
                            error = str(exc)
                if reconcile:
                    output.append(json.loads(prior[0]) if prior else
                        {**{k: item[k] for k in IDENTITY}, 'draft_key': key,
                         'state': 'not_attempted', 'reason': '尚无发送尝试，无需检查平台结果'})
                elif claim:
                    same_attempt = (claim['batch_id'], claim['draft_key']) == (batch_id, key)
                    output.append(self._result(batch_id, item,
                        'handoff_' + claim['state'] if same_attempt else 'blocked',
                        '已有浏览器发送记录；保留原结果，不重复发送' if same_attempt else
                        '原留言已有其他确认批次的发送尝试；不能重发或将其记为本批成功'))
                elif error:
                    output.append(self._result(batch_id, item, 'blocked', error))
                elif web:
                    output.append(self._result(batch_id, item, 'handoff_pending',
                        '已保存到 Agent 待办，等待当前会话在专用浏览器执行；尚未发送'))
                else:
                    output.append(self._result(batch_id, item, 'manual', reason))
                continue
            with _lock(self.root), open_db(self.root) as db:
                prior = db.execute('SELECT state FROM interaction_review_results WHERE batch_id=? AND draft_key=?',
                                   (batch_id, key)).fetchone()
            if not reconcile and prior and prior['state'] in {'attempting', 'unknown', 'verified'}:
                output.append({'draft_key': key, 'state': prior['state'], 'reason': '已有尝试或回执；只核验原结果，不重发'})
                continue
            try:
                # _binding revalidates the immutable draft and plan. The send guard
                # also rechecks after taking the existing channel's send lock.
                if not reconcile:
                    youtube.prepare(self.root, key, account, channel)
                    draft, _, _ = youtube._binding(self.root, key, account, channel)
                    if (any(draft[k] != item[k] for k in (*IDENTITY, 'source_digest', 'reply')) or
                            draft['created_at'] != item['draft_created_at']):
                        raise PublishError('本批确认与当前草稿不一致，请重新审核')
                    youtube.authorize(self.root, key, account, channel,
                        '用户在本机互动审核页确认了本批逐条文案，并点击发送已确认；批次 ' + batch_id,
                        expected_created_at=item['draft_created_at'])
                else:
                    youtube._binding(self.root, key, account, channel)
                profile = load_profile(self.profile_path)
                if _account_channel(profile, account, writing=not reconcile) != channel:
                    raise PublishError('当前 profile 的频道绑定已变化')
                bound, token = youtube_config(profile, {'platform': 'youtube', 'account': account}, writing=not reconcile)
                if bound != channel:
                    raise PublishError('YouTube 频道绑定不一致')
                if not reconcile:
                    self._result(batch_id, item, 'attempting', '已交给受控发送器；中断时先核验，不重发')
                receipt = youtube.execute(self.root, key, account, channel, self.client_factory(token), read_only=reconcile)
                state = 'verified' if (receipt.get('state') == 'verified' and
                    receipt.get('reconciliation') not in {'read_failed', 'not_verified'}) else 'unknown'
                if reconcile and receipt.get('state') == 'not_attempted':
                    state = 'not_attempted'
                output.append(self._result(batch_id, item, state,
                    '平台回复读回匹配' if state == 'verified' else '尚未核验公开；先检查原结果',
                    remote_id=receipt.get('remote_id')))
            except Exception:
                # Transport messages can contain credentials or response bodies.
                with _lock(self.root), open_db(self.root) as db:
                    prior = db.execute('SELECT state FROM interaction_review_results WHERE batch_id=? AND draft_key=?',
                                       (batch_id, key)).fetchone()
                state = 'unknown' if prior and prior['state'] in {'attempting', 'unknown'} else 'blocked'
                output.append(self._result(batch_id, item, state,
                    '执行或核验未完成；刷新后检查原文、草稿、账号权限和原发送回执，不自动重试'))
        return {'batch_id': batch_id, 'results': output, 'reconcile_only': reconcile}

    def handoffs(self):
        """Local pending work only; never wake an Agent or operate a browser."""
        with _lock(self.root), open_db(self.root) as db:
            rows = db.execute('''SELECT batch_id,draft_key,state FROM interaction_review_results
                WHERE state='handoff_pending' ORDER BY updated_at DESC LIMIT 200''').fetchall()
        result, seen = [], set()
        for row in rows:
            try:
                items = self._batch(row['batch_id'])
                item = next(r for r in items if r['draft_key'] == row['draft_key'])
                identity = tuple(item[k] for k in IDENTITY)
                with _lock(self.root), open_db(self.root) as db:
                    self._current_draft(db, item)
                    if not self._handoffable(db, item) or db.execute('''SELECT 1 FROM
                        interaction_review_handoffs WHERE platform=? AND account=? AND channel_id=?
                        AND comment_id=?''', identity).fetchone():
                        continue
                if identity not in seen:
                    result.append({**item, 'batch_id': row['batch_id'], 'state': row['state']})
                    seen.add(identity)
            except (PublishError, StopIteration):
                continue
        return result

    def claim_handoff(self, batch_id, draft_key):
        items = self._batch(batch_id)
        item = next((r for r in items if r['draft_key'] == _key(draft_key)), None)
        if not item:
            raise PublishError('该草稿不在本批确认中')
        identity = tuple(item[k] for k in IDENTITY)
        with _lock(self.root), open_db(self.root) as db:
            self._current_draft(db, item)
            if not self._handoffable(db, item):
                raise PublishError('未接通浏览器接力')
            pending = db.execute('''SELECT state FROM interaction_review_results
                WHERE batch_id=? AND draft_key=?''', (batch_id, draft_key)).fetchone()
            if not pending or pending['state'] != 'handoff_pending':
                raise PublishError('先提交已确认文案到 Agent 待办')
            if db.execute('''SELECT 1 FROM interaction_review_handoffs WHERE platform=?
                AND account=? AND channel_id=? AND comment_id=?''', identity).fetchone():
                raise PublishError('已有浏览器发送尝试；先检查原结果，不能重复发送')
            stamp = now()
            db.execute('INSERT INTO interaction_review_handoffs VALUES(?,?,?,?,?,?,?,?,?)',
                       (*identity, batch_id, draft_key, 'attempting', stamp, None))
        self._result(batch_id, item, 'handoff_attempting', 'Agent 已领取；如中断先核对原对话，不重发')
        return {**item, 'batch_id': batch_id, 'claimed_at': stamp}

    def finish_handoff(self, batch_id, draft_key, *, reply_id=None):
        """Require a fresh imported web reply to mark browser delivery verified."""
        items = self._batch(batch_id, for_send=False)
        item = next((r for r in items if r['draft_key'] == _key(draft_key)), None)
        if not item:
            raise PublishError('该草稿不在本批确认中')
        identity = tuple(item[k] for k in IDENTITY)
        with _lock(self.root), open_db(self.root) as db:
            claim = db.execute('''SELECT * FROM interaction_review_handoffs WHERE platform=?
                AND account=? AND channel_id=? AND comment_id=?''', identity).fetchone()
            if not claim or (claim['batch_id'], claim['draft_key']) != (batch_id, draft_key):
                raise PublishError('没有本批浏览器领取记录')
            if claim['state'] == 'verified':
                if reply_id != claim['reply_id']:
                    raise PublishError('已核验回复身份不一致')
                state = 'verified'
            elif reply_id is None:
                state = 'unknown'
            else:
                if not isinstance(reply_id, str) or not re.fullmatch(r'commentContent_[a-f0-9]{32}', reply_id):
                    raise PublishError('须提供网页实际回复标识')
                child = db.execute('''SELECT * FROM interaction_items WHERE platform=? AND account=?
                    AND channel_id=? AND comment_id=?''', (*identity[:3], reply_id)).fetchone()
                proof = db.execute('''SELECT observed_at FROM interaction_web_provenance WHERE platform=?
                    AND account=? AND channel_id=? AND comment_id=? AND identity_kind='alipay_dom' ''',
                    (*identity[:3], reply_id)).fetchone()
                root = db.execute('''SELECT * FROM interaction_items WHERE platform=? AND account=?
                    AND channel_id=? AND comment_id=?''', identity).fetchone()
                fields = (*IDENTITY, 'video_id', 'thread_id', 'parent_id', 'author_channel', 'body')
                if (not root or digest({k: root[k] for k in fields}) != item['source_digest'] or
                        not child or not child['is_own'] or child['parent_id'] != item['comment_id'] or
                        child['video_id'] != root['video_id'] or child['body'] != item['reply'] or not proof or
                        datetime.fromisoformat(proof['observed_at']) < datetime.fromisoformat(claim['claimed_at']) or
                        (datetime.fromisoformat(claim['claimed_at']) -
                         datetime.fromisoformat(child['published_at'])).total_seconds() > 120):
                    raise PublishError('网页读回未匹配本批原评、本人回复、原文及新观察时间')
                state = 'verified'
            db.execute('''UPDATE interaction_review_handoffs SET state=?,reply_id=? WHERE
                platform=? AND account=? AND channel_id=? AND comment_id=?''', (state, reply_id, *identity))
        return self._result(batch_id, item, 'handoff_' + state,
            '已在原对话独立读回本人回复（网页核验）' if state == 'verified' else
            '发送结果待检查；保留原尝试，不重新发送', remote_id=reply_id)


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, desk, port=0):
        self.desk = desk
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), ReviewHandler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = 'LocalReview'

    def log_message(self, *_):
        pass

    def _respond(self, status, value, content_type='application/json; charset=utf-8'):
        body = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-" +
                         self.server.token + "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _allowed(self, *, write=False):
        if self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            return False
        document_navigation = (self.command == 'GET' and self.path == '/' and
            self.headers.get('Sec-Fetch-Mode') == 'navigate' and self.headers.get('Sec-Fetch-Dest') == 'document')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and not document_navigation:
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            return False
        if write and origin != self.server.origin:
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return self._respond(403, {'error': '只接受本机审核页请求'})
        if self.path == '/':
            template = (Path(__file__).resolve().parents[1] / 'assets' / 'interaction-review.html').read_text()
            return self._respond(200, template.replace('__REVIEW_TOKEN__', self.server.token), 'text/html; charset=utf-8')
        if self.path != '/api/state':
            return self._respond(404, {'error': '未找到页面'})
        if not secrets.compare_digest(self.headers.get('X-Review-Token', ''), self.server.token):
            return self._respond(403, {'error': '请从本机审核页重新打开'})
        try:
            self._respond(200, self.server.desk.snapshot())
        except Exception:
            self._respond(500, {'error': '本机收件箱读取失败，请核对私有状态目录'})

    def do_POST(self):
        if (not self._allowed(write=True) or
                not secrets.compare_digest(self.headers.get('X-Review-Token', ''), self.server.token)):
            return self._respond(403, {'error': '请求未获本机审核页授权'})
        if self.path not in {'/api/confirm', '/api/send', '/api/reconcile'}:
            return self._respond(404, {'error': '未知审核操作'})
        if self.headers.get('Content-Type') != 'application/json':
            return self._respond(415, {'error': '仅接收审核页JSON'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                raise ValueError()
            self.connection.settimeout(15)
            data = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            return self._respond(400, {'error': '审核内容大小或格式无效'})
        if not self.server.desk.mutation_lock.acquire(blocking=False):
            return self._respond(409, {'error': '上一批操作仍在处理，请查看原结果'})
        try:
            if not isinstance(data, dict):
                raise PublishError('审核内容须为对象')
            if self.path == '/api/confirm':
                if set(data) != {'items'}:
                    raise PublishError('确认字段无效')
                result = self.server.desk.confirm(data['items'])
            else:
                if set(data) != {'batch_id'}:
                    raise PublishError('发送须引用已确认批次')
                result = self.server.desk.dispatch(data['batch_id'], reconcile=self.path == '/api/reconcile')
            self._respond(200, result)
        except PublishError as exc:
            self._respond(400, {'error': str(exc)})
        except Exception:
            self._respond(500, {'error': '操作未完成，请刷新核对原批次，不自动重发'})
        finally:
            self.server.desk.mutation_lock.release()


def main():
    p = argparse.ArgumentParser(description='本机互动审核页；只有点击发送已确认才尝试公开回复')
    p.add_argument('--state-dir', required=True)
    p.add_argument('--profile'); p.add_argument('--account')
    p.add_argument('--port', type=int, default=0)
    args = p.parse_args()
    try:
        if not 0 <= args.port <= 65535:
            raise PublishError('本机端口须为0至65535')
        desk = ReviewDesk(args.state_dir, profile=args.profile, account=args.account)
        with ReviewServer(desk, args.port) as server:
            print(json.dumps({'url': server.origin, 'listening': 'loopback_only',
                              'sending': 'only_explicitly_confirmed_youtube_account' if args.profile else 'disabled'}, ensure_ascii=False), flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        print('审核页未启动或已停止；核对本机私有账本和配置，不输出凭据', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
