#!/usr/bin/env python3
"""Bounded drafting from a fresh inbox. Never authorizes, sends, polls or installs jobs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
import stat
import sys
import time
import uuid
from zoneinfo import ZoneInfo

from interaction_context import load_content
from interaction_import import PLATFORMS, _safe_state_dir, _string
from interaction_inbox import _lock, open_db
from interaction_suggest import chat, decision, prepare_batch, settings, validate_work_index
from publish_core import PublishError, digest

FIELDS = {'schema', 'enabled', 'platform', 'account', 'channel_id', 'max_source_age_seconds',
          'max_requests_per_cycle', 'max_requests_per_day'}
SCHEMA = '''CREATE TABLE IF NOT EXISTS interaction_draft_cycle_state (
 platform TEXT NOT NULL, channel_id TEXT NOT NULL, circuit_open INTEGER NOT NULL,
 in_flight INTEGER NOT NULL, run_id TEXT, reason TEXT,
 PRIMARY KEY(platform,channel_id));
CREATE TABLE IF NOT EXISTS interaction_draft_cycle_budget (
 platform TEXT NOT NULL, channel_id TEXT NOT NULL, beijing_day TEXT NOT NULL, requests INTEGER NOT NULL,
 PRIMARY KEY(platform,channel_id,beijing_day));
CREATE TABLE IF NOT EXISTS interaction_draft_cycle_runs (
 run_id TEXT PRIMARY KEY, platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL, model_requests INTEGER NOT NULL,
 reason TEXT);'''


def policy(raw, *, allow_disabled=False):
    if (not isinstance(raw, dict) or set(raw) != FIELDS
            or raw['schema'] != 'interaction-draft-cycle.v1'
            or raw['enabled'] is not True and not (allow_disabled and raw['enabled'] is False)):
        raise PublishError('持续拟稿须由完整私有策略显式启用')
    out = dict(raw)
    if out['platform'] not in PLATFORMS:
        raise PublishError('拟稿平台无效')
    for key in ('account', 'channel_id'):
        out[key] = _string(out[key], key, 256)
    for key, low, high in [('max_source_age_seconds', 60, 86400),
                           ('max_requests_per_cycle', 1, 20), ('max_requests_per_day', 1, 100)]:
        if type(out[key]) is not int or not low <= out[key] <= high:
            raise PublishError('拟稿时效或请求预算无效')
    return out


def _clock(clock):
    t = clock()
    if not isinstance(t, datetime) or t.tzinfo is None:
        raise PublishError('拟稿时钟须带时区')
    return t.astimezone(timezone.utc)


def _cycle_lock(root, platform, channel):
    # Physical channel, rather than a local alias, owns the budget and circuit.
    p = root / ('interaction-draft-cycle-' + digest((platform, channel)) + '.lock')
    fd = os.open(p, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd); raise PublishError('持续拟稿锁不安全')
    f = os.fdopen(fd, 'r+')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close(); raise PublishError('同频道已有拟稿周期；查看原任务') from None
    return f


def _init(db, identity):
    db.executescript(SCHEMA)
    db.execute('INSERT OR IGNORE INTO interaction_draft_cycle_state VALUES (?,?,0,0,NULL,NULL)', identity)


def _source_guard(db, p, time):
    row = db.execute('''SELECT state,last_success_at FROM interaction_sources
        WHERE platform=? AND account=? AND channel_id=?''',
        (p['platform'], p['account'], p['channel_id'])).fetchone()
    if row is None or row['state'] != 'complete':
        return '同步缺失、不完整或失败；本轮不调用模型'
    try:
        stamp = datetime.fromisoformat(row['last_success_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None or not 0 <= (time - stamp).total_seconds() <= p['max_source_age_seconds']:
            raise ValueError()
    except (TypeError, AttributeError, ValueError):
        return '同步时间过旧、无时区或在未来；本轮不调用模型'
    return None


def run(root, raw_policy, index, model_config, *, client=chat, clock=lambda: datetime.now(timezone.utc), policy_reader=None):
    p, model_config = policy(raw_policy), settings(model_config)
    validate_work_index(index, p['platform'], p['account'], p['channel_id'])
    root, time = _safe_state_dir(root), _clock(clock)
    identity = (p['platform'], p['channel_id'])
    run_id, started = uuid.uuid4().hex, time.isoformat()
    day = time.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    base = {'schema': 'interaction-draft-cycle-result.v1', 'run_id': run_id,
            'platform': p['platform'], 'account': p['account'], 'channel_id': p['channel_id'],
            'beijing_day': day, 'model_requests': 0, 'authorized': False, 'sent': False}
    with _cycle_lock(root, *identity):
        with _lock(root), open_db(root) as db:
            _init(db, identity)
            previous = db.execute('SELECT * FROM interaction_draft_cycle_state WHERE platform=? AND channel_id=?', identity).fetchone()
            if previous['in_flight']:
                db.execute('UPDATE interaction_draft_cycle_runs SET finished_at=?,state=?,reason=? WHERE run_id=? AND finished_at IS NULL',
                           (started, 'interrupted', '模型请求后进程中断，结果未确认', previous['run_id']))
                db.execute('UPDATE interaction_draft_cycle_state SET circuit_open=1,reason=? WHERE platform=? AND channel_id=?',
                           ('此前拟稿周期在模型请求后中断，先检查缓存与请求记录', *identity))
            state = db.execute('SELECT * FROM interaction_draft_cycle_state WHERE platform=? AND channel_id=?', identity).fetchone()
            reason = state['reason'] if state['circuit_open'] else _source_guard(db, p, time)
            budget = db.execute('SELECT requests FROM interaction_draft_cycle_budget WHERE platform=? AND channel_id=? AND beijing_day=?', (*identity, day)).fetchone()
            spent = budget[0] if budget else 0
            remaining = max(0, p['max_requests_per_day'] - spent)
            if reason is None and not remaining:
                reason = '本执行器今日模型请求预算已用完；评论采集仍可继续'
            state_name = ('circuit_open' if state['circuit_open'] else
                          'budget_exhausted' if reason and remaining == 0 else
                          'waiting_for_sync' if reason else 'running')
            db.execute('INSERT INTO interaction_draft_cycle_runs VALUES (?,?,?,?,?,?,?,?,?)',
                       (run_id, p['platform'], p['account'], p['channel_id'], started,
                        started if reason else None, state_name, 0, reason))
            if not reason:
                db.execute('UPDATE interaction_draft_cycle_state SET run_id=?,reason=NULL WHERE platform=? AND channel_id=?', (run_id, *identity))
        if reason:
            return {**base, 'state': state_name, 'reason': reason, 'daily_requests': spent}
        provider_failed, reserved, blocked_reason, blocked_state = False, 0, None, None
        def bounded(config, packet):
            nonlocal provider_failed, reserved, blocked_reason, blocked_state
            if policy_reader is not None:
                try:
                    unchanged = policy(policy_reader()) == p
                except (PublishError, OSError, ValueError, TypeError):
                    unchanged = False
                if not unchanged:
                    blocked_reason, blocked_state = '拟稿策略已停用或变化；停止本轮后续请求', 'policy_changed'
                    raise PublishError(blocked_reason)
            request_time = _clock(clock)
            request_day = request_time.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
            with _lock(root), open_db(root) as db:
                _init(db, identity)
                blocked_reason = _source_guard(db, p, request_time)
                if blocked_reason:
                    blocked_state = 'waiting_for_sync'
                    raise PublishError(blocked_reason)
                used = db.execute('SELECT requests FROM interaction_draft_cycle_budget WHERE platform=? AND channel_id=? AND beijing_day=?', (*identity, request_day)).fetchone()
                if used and used[0] >= p['max_requests_per_day']:
                    blocked_reason, blocked_state = '本执行器今日模型请求预算已用完', 'budget_limited'
                    raise PublishError('模型请求预算已到上限')
                db.execute('''INSERT INTO interaction_draft_cycle_budget VALUES (?,?,?,1)
                    ON CONFLICT(platform,channel_id,beijing_day) DO UPDATE SET requests=requests+1''', (*identity, request_day))
                db.execute('UPDATE interaction_draft_cycle_runs SET model_requests=model_requests+1 WHERE run_id=?', (run_id,))
                db.execute('UPDATE interaction_draft_cycle_state SET in_flight=1 WHERE platform=? AND channel_id=?', identity)
            reserved += 1
            try:
                value, usage = client(config, packet)
                return decision(value), usage
            except Exception:
                provider_failed = True
                raise PublishError('模型请求或结果无效；不自动重试，不输出原响应') from None
        try:
            batch = prepare_batch(root, p['platform'], p['account'], p['channel_id'], index,
                                  model_config, limit=min(p['max_requests_per_cycle'], remaining), client=bounded)
            result_state = 'circuit_open' if provider_failed else blocked_state or batch['state']
            reason = '模型失败；持续拟稿已暂停，评论采集不受影响' if provider_failed else blocked_reason
        except (PublishError, OSError, ValueError):
            batch = None
            result_state, reason = 'error', '拟稿周期未完成；保留原队列，不发送'
            provider_failed = provider_failed or reserved > 0
        # A process kill before this transaction leaves in_flight=1 for the next cycle.
        finished = _clock(clock)
        reported_day = finished.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with _lock(root), open_db(root) as db:
            db.execute('UPDATE interaction_draft_cycle_runs SET finished_at=?,state=?,reason=? WHERE run_id=?',
                       (finished.isoformat(), result_state, reason, run_id))
            db.execute('UPDATE interaction_draft_cycle_state SET circuit_open=?,in_flight=0,reason=? WHERE platform=? AND channel_id=?',
                       (int(provider_failed), reason, *identity))
            spent = db.execute('SELECT requests FROM interaction_draft_cycle_budget WHERE platform=? AND channel_id=? AND beijing_day=?', (*identity, reported_day)).fetchone()
        return {**base, 'state': result_state, 'reason': reason, 'model_requests': reserved,
                'beijing_day': reported_day, 'daily_requests': spent[0] if spent else 0, 'batch': batch}


def reset(root, raw_policy, reason):
    p = policy(raw_policy)
    reason = _string(reason, 'reason', 1000)
    root = _safe_state_dir(root); identity = (p['platform'], p['channel_id'])
    with _cycle_lock(root, *identity), _lock(root), open_db(root) as db:
        _init(db, identity)
        db.execute('UPDATE interaction_draft_cycle_state SET circuit_open=0,in_flight=0,reason=? WHERE platform=? AND channel_id=?',
                   ('显式恢复：' + reason, *identity))
    return {'state': 'reset', 'budget_reset': False, 'authorized': False, 'sent': False}


def watch(root, load_policy, load_index, load_model, *, interval=1800, cycles=1, client=chat,
          clock=lambda: datetime.now(timezone.utc), sleep_fn=time.sleep, emit=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True)):
    from interaction_monitor import Monitor
    if type(interval) is not int or not 300 <= interval <= 86400 or type(cycles) is not int or not 1 <= cycles <= 10000:
        raise PublishError('拟稿观察间隔须至少 300 秒，轮数须为 1 至 10000')
    initial = policy(load_policy())
    binding = tuple(initial[k] for k in ('platform', 'account', 'channel_id'))
    total = 0
    with Monitor(root, 'draft-' + binding[0], binding[1], binding[2], interval=interval, cycles=cycles, clock=clock) as monitor:
        for n in range(1, cycles + 1):
            monitor.begin_cycle()
            try:
                raw_current = load_policy()
                if isinstance(raw_current, dict) and raw_current.get('enabled') is False:
                    monitor.finish('stopped', 'draft_policy_disabled')
                    return {'state': 'stopped', 'cycles': n - 1, 'model_requests': total,
                            'reason': '私有策略已停用持续拟稿', 'sent': False, 'authorized': False}
                current = policy(raw_current)
                if tuple(current[k] for k in ('platform', 'account', 'channel_id')) != binding:
                    raise PublishError('拟稿观察期间账号绑定变化')
                result = run(root, current, load_index(), load_model(), client=client, clock=clock, policy_reader=load_policy)
            except Exception:
                monitor.record_cycle('error'); monitor.finish('failed', 'draft_cycle_error')
                return {'state': 'error', 'cycles': n, 'model_requests': None,
                        'reason': '拟稿观察配置或运行失败，查看私有记录；未自动重试', 'sent': False, 'authorized': False}
            total += result['model_requests']
            state = result['state']
            monitor.record_cycle('complete' if state == 'prepared' else 'error' if state in {'error', 'circuit_open'} else 'partial')
            emit({'cycle': n, 'monitor_run_id': monitor.data['run_id'], 'state': state,
                  'model_requests': result['model_requests'], 'daily_requests': result['daily_requests'],
                  'reason': result['reason'], 'authorized': False, 'sent': False})
            if state in {'error', 'circuit_open'}:
                monitor.finish('failed', 'draft_circuit_or_cycle_error')
                return {'state': state, 'cycles': n, 'model_requests': total, 'sent': False, 'authorized': False}
            if state == 'policy_changed':
                monitor.finish('stopped', 'draft_policy_changed')
                return {'state': state, 'cycles': n, 'model_requests': total, 'sent': False, 'authorized': False}
            if n < cycles:
                monitor.sleep(); sleep_fn(interval)
        monitor.finish('completed', 'cycle_limit_reached')
    return {'state': state, 'cycles': cycles, 'model_requests': total, 'sent': False, 'authorized': False}


def main():
    p = argparse.ArgumentParser(description='近期同步后的有限拟稿；不采集、不授权、不发送、不安装定时任务')
    p.add_argument('--state-dir', required=True); p.add_argument('--policy', required=True)
    sub = p.add_subparsers(dest='action', required=True)
    run_p = sub.add_parser('run'); run_p.add_argument('--work-index', required=True); run_p.add_argument('--model-config', required=True)
    reset_p = sub.add_parser('reset'); reset_p.add_argument('--reason', required=True)
    sub.add_parser('status')
    watch_p = sub.add_parser('watch'); watch_p.add_argument('--work-index', required=True); watch_p.add_argument('--model-config', required=True)
    watch_p.add_argument('--interval-seconds', type=int, default=1800); watch_p.add_argument('--cycles', type=int, default=1)
    a = p.parse_args()
    try:
        pol = load_content(a.policy)
        if a.action == 'reset':
            result = reset(a.state_dir, pol, a.reason)
        elif a.action == 'status':
            from interaction_monitor import status
            checked = policy(pol, allow_disabled=True)
            result = status(a.state_dir, 'draft-' + checked['platform'], checked['account'], checked['channel_id'])
        elif a.action == 'watch':
            result = watch(a.state_dir, lambda: load_content(a.policy), lambda: load_content(a.work_index),
                           lambda: load_content(a.model_config), interval=a.interval_seconds, cycles=a.cycles)
        else:
            result = run(a.state_dir, pol, load_content(a.work_index), load_content(a.model_config), policy_reader=lambda:load_content(a.policy))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if a.action == 'status' or result['state'] in {'prepared', 'budget_limited', 'reset'} else 3
    except (PublishError, OSError, ValueError, TypeError):
        print('持续拟稿未完成；检查私有策略与运行记录，不自动重试', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
