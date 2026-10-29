#!/usr/bin/env python3
"""Finite official Weibo reads; unknown coverage remains partial, never replies."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import time

from interaction_import import _safe_state_dir
from interaction_monitor import Monitor, _open_lock, status
from publish_core import PublishError
from weibo_official_inbox import sync, validate_config
from weibo_comments_capture import _private_json


@contextmanager
def _watch_lock(root, uid):
    key = hashlib.sha256(('weibo-official-reader:' + uid).encode()).hexdigest()
    with _open_lock(Path(root) / ('weibo-official-reader-' + key + '.lock')) as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishError('该 UID 已有微博官方只读观察，不启动第二份') from None
        yield


def _binding(config):
    return json.dumps({k: v for k, v in config.items() if k != 'enabled'},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def watch(root, load_config, *, interval=1800, cycles=1, sync_fn=sync,
          sleep_fn=time.sleep, clock=None,
          emit=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True)):
    if (type(interval) is not int or not 1800 <= interval <= 86400 or
            type(cycles) is not int or not 1 <= cycles <= 49):
        raise PublishError('微博有限观察间隔须为 1800 至 86400 秒，轮数须为 1 至 49')
    initial = validate_config(load_config())
    frozen, uid = _binding(initial), initial['uid']
    root = _safe_state_dir(root)
    with _watch_lock(root, uid), Monitor(root, 'weibo', initial['account'], uid,
                                       interval=interval, cycles=cycles, clock=clock) as monitor:
        for n in range(1, cycles + 1):
            monitor.begin_cycle()
            try:
                raw = load_config()
                if isinstance(raw, dict) and raw.get('enabled') is False:
                    monitor.finish('stopped', 'reader_config_disabled')
                    return {'state': 'stopped', 'cycles': n - 1, 'social_writes': 0,
                            'reason': 'reader_config_disabled'}
                current = validate_config(raw)
                if _binding(current) != frozen:
                    monitor.finish('stopped', 'reader_config_changed')
                    return {'state': 'stopped', 'cycles': n - 1, 'social_writes': 0,
                            'reason': 'reader_config_changed'}
                result = sync_fn(root, current)
                if (not isinstance(result, dict) or result.get('state') != 'partial' or
                        any(type(result.get(k)) is not int or result[k] != 0 for k in
                            ('social_writes', 'canonical_imports')) or
                        any(type(result.get(k)) is not int or result[k] < 0 for k in
                            ('returned_rows', 'distinct_comments', 'new_local_candidates')) or
                        result['returned_rows'] < result['distinct_comments'] or
                        result['new_local_candidates'] > result['distinct_comments']):
                    raise PublishError('微博只读观察返回的边界未确认')
                emit({'cycle': n, 'monitor_run_id': monitor.data['run_id'], 'state': 'partial',
                      **{k: result[k] for k in ('returned_rows', 'distinct_comments', 'new_local_candidates')},
                      'scope': 'bounded_received_and_configured_works', 'social_writes': 0,
                      'canonical_imports': 0, 'send_enabled': False})
                monitor.record_cycle('partial')
            except Exception:
                monitor.record_cycle('error'); monitor.finish('failed', 'reader_cycle_error')
                return {'state': 'error', 'cycles': n, 'social_writes': 0,
                        'reason': '官方只读观察失败；停止且不自动重试，查看私有状态'}
            if n < cycles:
                monitor.sleep(); sleep_fn(interval)
        monitor.finish('completed', 'cycle_limit_reached')
    return {'state': 'completed', 'cycles': cycles, 'social_writes': 0,
            'coverage': 'partial', 'canonical_imports': 0, 'send_enabled': False}


def main():
    p = argparse.ArgumentParser(description='微博官方有限只读观察；不发送、不拟稿、不接CLI')
    p.add_argument('--config', required=True); p.add_argument('--state-dir', required=True)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    run = sub.add_parser('watch')
    run.add_argument('--interval-seconds', type=int, default=1800)
    run.add_argument('--cycles', type=int, default=1)
    args = p.parse_args()
    try:
        if args.command == 'status':
            cfg = _private_json(args.config)
            if isinstance(cfg, dict) and cfg.get('enabled') is False:
                cfg = dict(cfg, enabled=True)
            cfg = validate_config(cfg)
            result = status(_safe_state_dir(args.state_dir), 'weibo', cfg['account'], cfg['uid'])
        else:
            result = watch(args.state_dir, lambda: _private_json(args.config),
                           interval=args.interval_seconds, cycles=args.cycles)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get('state') == 'error' else 0
    except (PublishError, OSError, ValueError, TypeError):
        print('微博有限只读观察未启动；核对私有配置、观察锁和范围，未发送评论', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
