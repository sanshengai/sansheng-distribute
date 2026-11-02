"""Cross-platform interleaved web-posting scheduler (local pacing only).

It never guarantees avoiding platform risk control, and it never touches
browsers, cookies, proxies, fingerprints or the ledger. Callers execute the
returned action themselves and report completion. Pure, clock-injectable.

Rules:
- one action at a time globally; switching platform waits a random switch gap;
- a task is upload -> dwell (random, per platform) -> send;
- sends on the same platform are spaced by a truncated log-normal interval
  with a hard floor (floor is never undercut, even if config is edited);
- per-platform daily cap (box-local day); paused platforms never run.
"""
from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS_PATH = Path(__file__).resolve().parents[1] / 'assets' / 'web-pacing.default.json'
HARD_FLOOR_MINUTES = 5      # absolute minimum between sends on one platform
HARD_MIN_DWELL = 10         # seconds; dwell can never be configured below this


class PacingError(ValueError):
    pass


@dataclass
class PlatformRule:
    dwell_seconds: tuple
    interval_median_minutes: float
    interval_sigma: float
    interval_floor_minutes: float
    interval_cap_minutes: float
    daily_cap: int
    paused: bool = False


def _rule(name, raw):
    try:
        lo, hi = raw['dwell_seconds']
        r = PlatformRule((float(lo), float(hi)), float(raw['interval_median_minutes']),
                         float(raw['interval_sigma']), float(raw['interval_floor_minutes']),
                         float(raw['interval_cap_minutes']), int(raw['daily_cap']), bool(raw.get('paused', False)))
    except (KeyError, TypeError, ValueError) as exc:
        raise PacingError(f'{name}: 配置不完整或类型错误（{exc}）')
    if not HARD_MIN_DWELL <= r.dwell_seconds[0] <= r.dwell_seconds[1]:
        raise PacingError(f'{name}: dwell_seconds 须为 [lo, hi] 且 lo ≥ {HARD_MIN_DWELL}')
    if not HARD_FLOOR_MINUTES <= r.interval_floor_minutes <= r.interval_median_minutes <= r.interval_cap_minutes:
        raise PacingError(f'{name}: 须满足 {HARD_FLOOR_MINUTES} ≤ floor ≤ median ≤ cap')
    if not 0 < r.interval_sigma <= 1.5:
        raise PacingError(f'{name}: interval_sigma 须在 (0, 1.5]')
    if r.daily_cap < 1:
        raise PacingError(f'{name}: daily_cap 须 ≥ 1')
    return r


def load_config(override=None, path=DEFAULTS_PATH):
    """Defaults from assets, optionally merged with a profile's `web_pacing` object."""
    base = json.loads(Path(path).read_text(encoding='utf-8'))
    override = override or {}
    if not isinstance(override, dict):
        raise PacingError('web_pacing 必须为对象')
    gap = override.get('switch_gap_seconds', base['switch_gap_seconds'])
    platforms = {k: dict(v) for k, v in base['platforms'].items()}
    for name, patch in (override.get('platforms') or {}).items():
        if not isinstance(patch, dict):
            raise PacingError(f'{name}: 平台配置必须为对象')
        platforms.setdefault(name, {}).update(patch)
    try:
        gap = (float(gap[0]), float(gap[1]))
    except (TypeError, ValueError, IndexError):
        raise PacingError('switch_gap_seconds 须为 [lo, hi]')
    if not 0 <= gap[0] <= gap[1]:
        raise PacingError('switch_gap_seconds 须 0 ≤ lo ≤ hi')
    return gap, {k: _rule(k, v) for k, v in platforms.items()}


def sample_dwell(rule, rng):
    """Non-uniform dwell inside [lo, hi]: triangular skewed toward the lower third."""
    lo, hi = rule.dwell_seconds
    if lo == hi:
        return lo
    return rng.triangular(lo, hi, lo + (hi - lo) / 3)


def sample_interval(rule, rng):
    """Seconds; log-normal around the median, truncated to [floor, cap] by resampling."""
    mu = math.log(rule.interval_median_minutes * 60)
    floor = max(rule.interval_floor_minutes, HARD_FLOOR_MINUTES) * 60
    cap = rule.interval_cap_minutes * 60
    for _ in range(64):
        v = rng.lognormvariate(mu, rule.interval_sigma)
        if floor <= v <= cap:
            return v
    return min(max(v, floor), cap)


@dataclass
class Task:
    task_id: str
    platform: str
    phase: str = 'upload'          # upload -> dwell -> send -> done
    send_ready_at: float = 0.0
    dwell: float = 0.0


@dataclass
class Scheduler:
    switch_gap: tuple
    rules: dict
    rng: random.Random = field(default_factory=random.Random)
    tasks: list = field(default_factory=list)
    busy: Task = None
    busy_phase: str = None
    last_platform: str = None
    last_action_end: float = None
    next_switch_at: float = 0.0
    next_upload_at: dict = field(default_factory=dict)   # platform -> earliest next upload
    last_send_at: dict = field(default_factory=dict)
    sends_by_day: dict = field(default_factory=dict)     # (platform, day) -> count
    log: list = field(default_factory=list)
    max_open_editors: int = 2
    last_task: Task = None

    @classmethod
    def from_config(cls, override=None, seed=None):
        gap, rules = load_config(override)
        n = int((override or {}).get('max_open_editors', 2))
        if not 1 <= n <= 3:
            raise PacingError('max_open_editors 须为 1 到 3')
        return cls(gap, rules, random.Random(seed), max_open_editors=n)

    def add(self, task_id, platform):
        if platform not in self.rules:
            raise PacingError(f'{platform}: 无节奏配置，拒绝排队')
        if self.rules[platform].paused:
            raise PacingError(f'{platform}: 已暂停，拒绝排队')
        if any(t.task_id == task_id for t in self.tasks):
            raise PacingError(f'{task_id}: 重复任务')
        self.tasks.append(Task(task_id, platform))

    @staticmethod
    def _day(ts):
        return time.strftime('%Y-%m-%d', time.localtime(ts))

    def _count(self, platform, ts):
        return self.sends_by_day.get((platform, self._day(ts)), 0)

    def _committed(self, platform, ts):
        """Sends done today + tasks already uploaded (they will send)."""
        pending = sum(1 for t in self.tasks if t.platform == platform and t.phase == 'dwell')
        return self._count(platform, ts) + pending

    def _eligible_at(self, task, now):
        if task.phase == 'dwell':
            ready = task.send_ready_at
            floor = self.last_send_at.get(task.platform)
            if floor is not None:
                ready = max(ready, floor + max(self.rules[task.platform].interval_floor_minutes, HARD_FLOOR_MINUTES) * 60)
        elif task.phase == 'upload':
            if any(t.platform == task.platform and t.phase == 'dwell' for t in self.tasks):
                return None            # one open editor per platform
            if self._committed(task.platform, now) >= self.rules[task.platform].daily_cap:
                return None            # cap reached today; caller defers to tomorrow
            ready = self.next_upload_at.get(task.platform, 0.0)
        else:
            return None
        if self.last_platform is not None and task.platform != self.last_platform:
            ready = max(ready, self.next_switch_at)
        return ready

    def next_action(self, now):
        """Return ('run', task, phase) | ('wait', seconds) | ('idle', reason)."""
        if self.busy is not None:
            return ('wait', None)
        best = None
        open_editors = sum(1 for t in self.tasks if t.phase == 'dwell')
        for t in self.tasks:
            at = self._eligible_at(t, now)
            if at is None:
                continue
            # while fewer than max_open_editors are open, start another platform's upload
            # first (alternate, don't send right after upload); otherwise drain sends.
            prefer = 'upload' if open_editors < self.max_open_editors else 'dwell'
            key = (0 if t.phase == prefer else 1, at, 0 if t.platform != self.last_platform else 1)
            if at > now and t.phase == prefer:
                key = (1,) + key[1:]       # don't idle for a preferred action if another is ready
            if t.phase == 'dwell' and t is self.last_task and len(self.tasks) > 1:
                key = (2,) + key[1:]       # avoid send immediately after its own upload
            if best is None or key < best[0]:
                best = (key, t)
        if best is None:
            left = [t for t in self.tasks if t.phase != 'done']
            return ('idle', 'all_done' if not left else 'daily_cap_or_blocked')
        at, task = self._eligible_at(best[1], now), best[1]
        if at > now:
            return ('wait', at - now)
        phase = 'send' if task.phase == 'dwell' else 'upload'
        self.busy, self.busy_phase = task, phase
        return ('run', task, phase)

    def done(self, task, now, ok=True):
        """Report the end of the action returned by next_action. ok=False stops the platform."""
        if task is not self.busy:
            raise PacingError('完成的动作不是当前动作')
        phase, self.busy, self.busy_phase = self.busy_phase, None, None
        self.next_switch_at = now + self.rng.uniform(*self.switch_gap)
        self.last_platform, self.last_action_end, self.last_task = task.platform, now, task
        if not ok:
            # hard stop: never retry on this platform in this run
            for t in self.tasks:
                if t.platform == task.platform and t.phase != 'done':
                    t.phase = 'stopped'
            self.log.append((now, task.task_id, phase, 'stopped'))
            return
        rule = self.rules[task.platform]
        if phase == 'upload':
            task.dwell = sample_dwell(rule, self.rng)
            task.send_ready_at = now + task.dwell
            task.phase = 'dwell'
        else:
            task.phase = 'done'
            self.last_send_at[task.platform] = now
            key = (task.platform, self._day(now))
            self.sends_by_day[key] = self.sends_by_day.get(key, 0) + 1
            self.next_upload_at[task.platform] = now + sample_interval(rule, self.rng)
        self.log.append((now, task.task_id, phase, 'ok'))


def run(scheduler, perform, clock=time.time, sleep=time.sleep):
    """Drive the scheduler serially. perform(task, phase) -> bool. Returns the log."""
    while True:
        step = scheduler.next_action(clock())
        if step[0] == 'idle':
            return scheduler.log
        if step[0] == 'wait':
            sleep(step[1])
            continue
        _, task, phase = step
        ok = bool(perform(task, phase))
        scheduler.done(task, clock(), ok)


def plan_preview(scheduler, upload_seconds=120, send_seconds=30, start=None):
    """Dry-run with simulated time; returns [(iso_local, task, phase)] for humans to review."""
    now = [start if start is not None else time.time()]
    out = []

    def perform(task, phase):
        out.append((time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now[0])), task.task_id, phase))
        now[0] += upload_seconds if phase == 'upload' else send_seconds
        return True

    def sleep(s):
        now[0] += s

    run(scheduler, perform, clock=lambda: now[0], sleep=sleep)
    return out


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='预览跨平台交错发布节奏（只模拟，不动浏览器）')
    ap.add_argument('task', nargs='+', help='task_id:platform')
    ap.add_argument('--seed', type=int)
    ap.add_argument('--profile', help='含 web_pacing 的私有 profile JSON')
    a = ap.parse_args()
    override = json.loads(Path(a.profile).read_text())['web_pacing'] if a.profile else None
    s = Scheduler.from_config(override, a.seed)
    for item in a.task:
        tid, _, plat = item.rpartition(':')
        s.add(tid, plat)
    for row in plan_preview(s):
        print(*row, sep='\t')
