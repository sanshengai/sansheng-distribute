import random
import statistics
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pacing
from pacing import PacingError, Scheduler, load_config, plan_preview, sample_dwell, sample_interval

START = 1_800_000_000.0  # fixed epoch for reproducibility


def sched(seed=1, override=None):
    return Scheduler.from_config(override, seed)


def test_defaults_load_and_xiaohongshu_paused():
    gap, rules = load_config()
    assert gap == (20.0, 90.0)
    assert rules['xiaohongshu'].paused
    assert rules['x'].interval_floor_minutes == 12 and rules['x'].daily_cap == 6
    with pytest.raises(PacingError):
        sched().add('a', 'xiaohongshu')


def test_invalid_config_rejected():
    for bad in ({'platforms': {'x': {'dwell_seconds': [1, 5]}}},
                {'platforms': {'x': {'interval_floor_minutes': 1}}},
                {'platforms': {'x': {'interval_median_minutes': 200}}},
                {'platforms': {'x': {'daily_cap': 0}}},
                {'switch_gap_seconds': [50, 10]}):
        with pytest.raises(PacingError):
            load_config(bad)


def test_dwell_in_range_and_not_uniform():
    _, rules = load_config()
    rng = random.Random(3)
    xs = [sample_dwell(rules['x'], rng) for _ in range(4000)]
    assert min(xs) >= 30 and max(xs) <= 180
    assert statistics.median(xs) < 105  # skewed toward short, not uniform midpoint
    assert len({round(x) for x in xs}) > 100


def test_interval_lognormal_with_hard_floor():
    _, rules = load_config()
    rng = random.Random(4)
    xs = [sample_interval(rules['x'], rng) for _ in range(4000)]
    assert min(xs) >= 12 * 60 and max(xs) <= 90 * 60
    assert 20 * 60 < statistics.median(xs) < 30 * 60
    assert statistics.pstdev(xs) > 5 * 60  # clearly irregular
    assert statistics.mean(xs) > statistics.median(xs)  # right skew


def test_interleaves_platforms_and_never_sends_right_after_upload():
    s = sched(7)
    for i in range(3):
        for p in ('douyin', 'weibo', 'x', 'bilibili', 'wechat_channels'):
            s.add(f'{p}{i}', p)
    rows = plan_preview(s, start=START)
    log = s.log
    # one action at a time is structural; check alternation: first five uploads hit five platforms
    first = [t for _, t, ph, _ in log if ph == 'upload'][:5]
    assert len({t.rstrip('0123456789') for t in first}) == 5
    by_task = {}
    for ts, tid, ph, _ in log:
        by_task.setdefault(tid, {})[ph] = ts
    rules = s.rules
    for tid, d in by_task.items():
        plat = tid.rstrip('0123456789')
        # log stamps are action end times; send lasts 30 s in preview
        assert (d['send'] - 30) - d['upload'] >= rules[plat].dwell_seconds[0] - 1e-6
    # first round: other platforms act between an upload and its send (later rounds
    # may legitimately be alone because each platform waits its own interval)
    interleaved = 0
    for tid, d in by_task.items():
        if not tid.endswith('0'):
            continue
        between = [x for ts, x, _, _ in log if d['upload'] < ts < d['send']]
        interleaved += bool(between)
    assert interleaved >= 4
    assert len(rows) == 30


def test_same_platform_send_gap_respects_floor_and_varies():
    s = sched(11, {'platforms': {'weibo': {'daily_cap': 10}}})
    for i in range(6):
        s.add(f'w{i}', 'weibo')
    plan_preview(s, start=START)
    sends = [ts for ts, _, ph, _ in s.log if ph == 'send']
    gaps = [b - a for a, b in zip(sends, sends[1:])]
    assert len(gaps) == 5
    assert min(gaps) >= 20 * 60
    assert len({round(g) for g in gaps}) == 5


def test_switch_gap_between_platform_actions():
    s = sched(5)
    s.add('a', 'x'); s.add('b', 'weibo')
    plan_preview(s, upload_seconds=60, send_seconds=10, start=START)
    log = s.log
    ends = {'upload': 60, 'send': 10}
    for (t1, id1, ph1, _), (t2, id2, _, _) in zip(log, log[1:]):
        if id1[0] != id2[0]:
            assert t2 - t1 >= 20  # start-to-start >= switch gap lower bound (action length included)


def test_daily_cap_stops_queue():
    s = sched(2, {'platforms': {'douyin': {'daily_cap': 2}}})
    for i in range(4):
        s.add(f'd{i}', 'douyin')
    plan_preview(s, start=START)
    sends = [tid for _, tid, ph, _ in s.log if ph == 'send']
    # all four may finish only if the simulated run crossed into the next local day
    days = {pacing.Scheduler._day(ts) for ts, _, ph, _ in s.log if ph == 'send'}
    for day in days:
        assert sum(1 for ts, _, ph, _ in s.log if ph == 'send' and pacing.Scheduler._day(ts) == day) <= 2


def test_failure_hard_stops_platform_only():
    s = sched(9)
    s.add('x1', 'x'); s.add('x2', 'x'); s.add('w1', 'weibo')
    now = [START]

    def perform(task, phase):
        now[0] += 30
        return not (task.task_id == 'x1' and phase == 'send')

    pacing.run(s, perform, clock=lambda: now[0], sleep=lambda d: now.__setitem__(0, now[0] + d))
    phases = {t.task_id: t.phase for t in s.tasks}
    assert phases == {'x1': 'stopped', 'x2': 'stopped', 'w1': 'done'}


def test_rejects_unknown_platform_and_duplicates():
    s = sched()
    with pytest.raises(PacingError):
        s.add('a', 'nope')
    s.add('a', 'x')
    with pytest.raises(PacingError):
        s.add('a', 'weibo')


def test_done_must_match_current_action():
    s = sched()
    s.add('a', 'x')
    step = s.next_action(START)
    assert step[0] == 'run'
    with pytest.raises(PacingError):
        s.done(pacing.Task('other', 'x'), START + 1)
    assert s.next_action(START) == ('wait', None)
