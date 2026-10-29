#!/usr/bin/env python3
"""Same-host watch lock and private run evidence; never discovers or sends itself."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import uuid

from publish_core import PublishError, digest, write_json
from youtube_comments_candidate import _private_dir

ACTIVE = {'running', 'sleeping'}
TERMINAL = {'completed', 'failed', 'stopped'}
KEYS = {'schema', 'run_id', 'platform', 'account', 'channel_id', 'state', 'pid',
        'started_at', 'updated_at', 'finished_at', 'reason', 'interval_seconds',
        'requested_cycles', 'cycles', 'complete_cycles', 'partial_cycles', 'error_cycles',
        'last_cycle_state', 'last_started_at', 'next_due_at', 'max_start_gap_seconds'}


def _time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError('timezone required')
        return stamp
    except (TypeError, ValueError) as exc:
        raise PublishError('巡检记录时间无效') from exc


def _paths(root, platform, account, channel):
    if any(not isinstance(v, str) or not v or len(v) > 256 for v in (platform, account, channel)):
        raise PublishError('巡检账号身份无效')
    root = _private_dir(root)
    key = digest({'platform': platform, 'account': account, 'channel_id': channel})
    return root, root / ('interaction-watch-' + key + '.lock'), root / ('interaction-watch-' + key + '.json')


def _open_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise PublishError('巡检锁文件权限或类型无效')
    return os.fdopen(fd, 'r+')


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 32768):
            raise PublishError('巡检记录权限、类型或大小无效')
        raw = file.read(32769)
        if len(raw) > 32768:
            raise PublishError('巡检记录过大')
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise PublishError('巡检记录损坏；保留证据，不覆盖') from exc
    if (not isinstance(data, dict) or set(data) != KEYS or data.get('schema') != 'interaction-watch.v1'
            or not isinstance(data.get('state'), str)
            or data.get('state') not in ACTIVE | TERMINAL):
        raise PublishError('巡检记录格式无效；保留证据')
    run_id = data['run_id']
    if (not isinstance(run_id, str) or len(run_id) != 32
            or any(c not in '0123456789abcdef' for c in run_id)):
        raise PublishError('巡检记录身份无效')
    for key in ('platform', 'account', 'channel_id'):
        if not isinstance(data[key], str) or not data[key] or len(data[key]) > 256:
            raise PublishError('巡检记录账号无效')
    for key in ('pid', 'interval_seconds', 'requested_cycles', 'cycles',
                'complete_cycles', 'partial_cycles', 'error_cycles'):
        if type(data[key]) is not int or data[key] < (1 if key == 'pid' else 0):
            raise PublishError('巡检记录计数无效')
    if (data['interval_seconds'] < 300 or data['cycles'] != sum(data[k] for k in
            ('complete_cycles', 'partial_cycles', 'error_cycles'))):
        raise PublishError('巡检记录计数不一致')
    for key in ('started_at', 'updated_at'):
        _time(data[key])
    for key in ('finished_at', 'last_started_at', 'next_due_at'):
        if data[key] is not None:
            _time(data[key])
    gap = data['max_start_gap_seconds']
    if (type(gap) not in (int, float) or not math.isfinite(gap) or gap < 0
            or (data['last_cycle_state'] is not None and not isinstance(data['last_cycle_state'], str))
            or data['last_cycle_state'] not in {None, 'complete', 'partial', 'error'}
            or (data['reason'] is not None and (not isinstance(data['reason'], str) or len(data['reason']) > 256))):
        raise PublishError('巡检记录结果无效')
    return data


class Monitor:
    def __init__(self, root, platform, account, channel, *, interval, cycles, clock=None):
        if type(interval) is not int or interval < 300 or type(cycles) is not int or cycles < 0:
            raise PublishError('巡检间隔或轮数无效')
        self.root, self.lock_path, self.path = _paths(root, platform, account, channel)
        self.identity = dict(platform=platform, account=account, channel_id=channel)
        self.interval, self.cycles = interval, cycles
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.file = None
        self.data = None
        self.history = None

    def _save(self):
        self.data['updated_at'] = self.clock().isoformat()
        write_json(self.history, self.data)
        write_json(self.path, self.data)

    def __enter__(self):
        self.file = _open_lock(self.lock_path)
        try:
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PublishError('同一账号已有巡检持有锁，不启动第二个巡检') from exc
            previous = _read(self.path)
            history_root = _private_dir(self.root / 'interaction-watch-runs')
            if previous and any(previous[k] != v for k, v in self.identity.items()):
                raise PublishError('历史巡检账号身份不符，不覆盖')
            if previous and previous['state'] in ACTIVE:
                run_id = previous.get('run_id')
                if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id):
                    raise PublishError('历史巡检身份无效，不覆盖')
                previous.update(state='stopped', reason='previous_lock_released_without_terminal_record',
                                finished_at=None)
                write_json(history_root / (run_id + '.json'), previous)
            stamp = self.clock().isoformat()
            run_id = uuid.uuid4().hex
            self.history = history_root / (run_id + '.json')
            self.data = dict(schema='interaction-watch.v1', run_id=run_id,
                **self.identity, state='running', pid=os.getpid(), started_at=stamp,
                updated_at=stamp, finished_at=None, reason=None, interval_seconds=self.interval,
                requested_cycles=self.cycles, cycles=0, complete_cycles=0, partial_cycles=0,
                error_cycles=0, last_cycle_state=None, last_started_at=None,
                next_due_at=None, max_start_gap_seconds=0.0)
            self._save()
            return self
        except BaseException:
            self.file.close()
            self.file = None
            raise

    def begin_cycle(self):
        stamp = self.clock()
        if self.data['last_started_at']:
            gap = max(0.0, (stamp - _time(self.data['last_started_at'])).total_seconds())
            self.data['max_start_gap_seconds'] = max(self.data['max_start_gap_seconds'], gap)
        self.data.update(state='running', last_started_at=stamp.isoformat(), next_due_at=None)
        self._save()

    def record_cycle(self, state):
        if not isinstance(state, str) or state not in {'complete', 'partial', 'error'}:
            raise PublishError('不支持的巡检结果')
        self.data['cycles'] += 1
        self.data[state + '_cycles'] += 1
        self.data['last_cycle_state'] = state
        self._save()

    def sleep(self):
        self.data.update(state='sleeping', next_due_at=(self.clock() + timedelta(seconds=self.interval)).isoformat())
        self._save()

    def finish(self, state, reason):
        if not isinstance(state, str) or state not in TERMINAL:
            raise PublishError('无效的巡检终止状态')
        self.data.update(state=state, reason=reason, finished_at=self.clock().isoformat(), next_due_at=None)
        self._save()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self.data and self.data['state'] in ACTIVE:
                self.finish('stopped' if exc_type is KeyboardInterrupt else 'failed',
                            'interrupted' if exc_type is KeyboardInterrupt else 'unexpected_exit')
        finally:
            self.file.close()
            self.file = None


def status(root, platform, account, channel, *, clock=None):
    _, lock_path, path = _paths(root, platform, account, channel)
    with _open_lock(lock_path) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = False
        except BlockingIOError:
            held = True
        data = _read(path)
    if data is None:
        return {'state': 'not_started', 'lock_held': held, 'health': 'unknown' if held else 'not_running'}
    if any(data.get(k) != v for k, v in dict(platform=platform, account=account, channel_id=channel).items()):
        raise PublishError('巡检记录账号身份不符')
    active = data['state'] in ACTIVE
    current = (clock or (lambda: datetime.now(timezone.utc)))()
    deadline = _time(data['next_due_at'] or data['updated_at'])
    tolerance = max(60, min(int(data['interval_seconds']), 300))
    overdue = active and current > deadline + timedelta(seconds=tolerance)
    health = ('stopped_unexpectedly' if active and not held else
              'overdue' if active and overdue else
              'lock_observed' if active and held else 'terminal')
    return {**data, 'lock_held': held, 'health': health,
            'elapsed_seconds': max(0.0, (_time(data['finished_at']) if data['finished_at'] else current
                                        ).timestamp() - _time(data['started_at']).timestamp()),
            'note': '同机锁和时间证据，不证明平台全覆盖、模型拟稿、发送或云端全天在线'}
