#!/usr/bin/env python3
"""Finite Bilibili comment read observations; no replies, login or model calls."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import time

from bilibili_comments_candidate import Client, _id, _private_json, sync, validate_config
from interaction_import import _safe_state_dir
from interaction_monitor import Monitor, _open_lock, status
from publish_core import PublishError


@contextmanager
def _watch_lock(root, uid):
    # Account aliases cannot create two watchers for the same physical account.
    key = hashlib.sha256(('bilibili-reader:' + uid).encode()).hexdigest()
    with _open_lock(Path(root) / ('bilibili-reader-' + key + '.lock')) as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishError('该 UID 已有 B站只读观察，不启动第二份') from None
        yield


def _binding(cfg):
    # Freeze scope, cutoff and credential path as well as the account identity.
    return json.dumps({k: v for k, v in cfg.items() if k != 'enabled'},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def watch(root, load_config, *, interval=1800, cycles=1, max_pages=3, max_parents=10,
          max_reply_pages=3, client_factory=Client, sync_fn=sync, sleep_fn=time.sleep,
          clock=None, emit=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True)):
    if (type(interval) is not int or not 1800 <= interval <= 86400
            or type(cycles) is not int or not 1 <= cycles <= 49):
        raise PublishError('B站有限观察间隔须为 1800 至 86400 秒，轮数须为 1 至 49')
    if (type(max_pages) is not int or not 2 <= max_pages <= 10
            or type(max_parents) is not int or not 1 <= max_parents <= 50
            or type(max_reply_pages) is not int or not 2 <= max_reply_pages <= 10):
        raise PublishError('B站有限观察扫描预算无效')
    initial = validate_config(load_config())
    frozen, uid = _binding(initial), _id(initial['uid'])
    root = _safe_state_dir(root)
    with _watch_lock(root, uid), Monitor(root, 'bilibili', initial['account'], uid,
                                       interval=interval, cycles=cycles, clock=clock) as monitor:
        for n in range(1, cycles + 1):
            monitor.begin_cycle()
            try:
                raw = load_config()
                if isinstance(raw, dict) and raw.get('enabled') is False:
                    monitor.finish('stopped', 'reader_config_disabled')
                    return {'state': 'stopped', 'cycles': n - 1, 'writes': 0,
                            'reason': 'reader_config_disabled'}
                current = validate_config(raw)
                if _binding(current) != frozen:
                    monitor.finish('stopped', 'reader_config_changed')
                    return {'state': 'stopped', 'cycles': n - 1, 'writes': 0,
                            'reason': 'reader_config_changed'}
                result = sync_fn(root, current, client_factory(current['credentials']),
                                 max_pages=max_pages, max_parents=max_parents,
                                 max_reply_pages=max_reply_pages)
                if (not isinstance(result, dict) or result.get('state') not in {'complete', 'partial'}
                        or type(result.get('writes')) is not int or result['writes'] != 0
                        or result.get('official_api') is not False
                        or result.get('send_enabled') is not False
                        or result.get('platform') != 'bilibili'
                        or result.get('scope') != 'configured_works'
                        or result.get('works') != len(current['works'])
                        or any(type(result.get(k)) is not int or result[k] < 0 for k in
                               ('works', 'added', 'baseline', 'parents_scanned', 'parents_total'))):
                    raise PublishError('B站只读观察返回的边界未确认')
                emit({'cycle': n, 'monitor_run_id': monitor.data['run_id'],
                      **{k: result[k] for k in ('state', 'works', 'added', 'baseline',
                                               'parents_scanned', 'parents_total')},
                      'scope': 'configured_works', 'writes': 0, 'official_api': False,
                      'send_enabled': False})
                monitor.record_cycle(result['state'])
            except Exception:
                monitor.record_cycle('error'); monitor.finish('failed', 'reader_cycle_error')
                return {'state': 'error', 'cycles': n, 'writes': 0,
                        'reason': '只读观察失败；停止且不自动重试，查看私有状态'}
            if n < cycles:
                monitor.sleep(); sleep_fn(interval)
        monitor.finish('completed', 'cycle_limit_reached')
    return {'state': 'completed', 'cycles': cycles, 'writes': 0, 'send_enabled': False,
            'scope': 'configured_works', 'official_api': False}


def main():
    p = argparse.ArgumentParser(description='B站有限只读观察；不发送、不拟稿、不刷新登录')
    p.add_argument('--config', required=True); p.add_argument('--state-dir', required=True)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    run = sub.add_parser('watch')
    run.add_argument('--interval-seconds', type=int, default=1800)
    run.add_argument('--cycles', type=int, default=1)
    run.add_argument('--max-pages', type=int, default=3)
    run.add_argument('--max-parents', type=int, default=10)
    run.add_argument('--max-reply-pages', type=int, default=3)
    args = p.parse_args()
    try:
        if args.command == 'status':
            cfg = _private_json(args.config)
            # Status remains available after the owner disables the reader.
            if isinstance(cfg, dict) and cfg.get('enabled') is False:
                cfg = dict(cfg, enabled=True)
            cfg = validate_config(cfg)
            result = status(_safe_state_dir(args.state_dir), 'bilibili', cfg['account'], _id(cfg['uid']))
        else:
            result = watch(args.state_dir, lambda: _private_json(args.config),
                           interval=args.interval_seconds, cycles=args.cycles,
                           max_pages=args.max_pages, max_parents=args.max_parents,
                           max_reply_pages=args.max_reply_pages)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get('state') == 'error' else 0
    except (PublishError, OSError, ValueError, TypeError):
        print('B站有限只读观察未启动；核对私有配置、观察锁和范围，未读取或发送评论', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
