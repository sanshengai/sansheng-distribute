import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import interaction_draft_cycle as c
from interaction_import import import_manual
from interaction_inbox import open_db
from publish_core import PublishError


class DraftCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.time = datetime(2026, 10, 29, 7, tzinfo=timezone.utc)
        self.p = dict(schema='interaction-draft-cycle.v1', enabled=True, platform='youtube',
                      account='owner', channel_id='channel', max_source_age_seconds=3600,
                      max_requests_per_cycle=2, max_requests_per_day=3)
        self.model = dict(enabled=True, base_url_env='TEST_CYCLE_BASE', api_key_env='TEST_CYCLE_KEY', model='fixture')
        self.index = {'schema': 'interaction-work-index.v1', 'works': [dict(
            platform='youtube', account='owner', channel_id='channel', video_id='video',
            title='Relay race', summary='An episode about a relay race.')]}
        self.answer = dict(action='reply', category='question', text='可以先跟读一句。', reason='作品为跟读素材。')
        env = patch.dict(os.environ, TEST_CYCLE_BASE='https://model.example.invalid/v1', TEST_CYCLE_KEY='fixture-secret')
        env.start(); self.addCleanup(env.stop)
        self.add('first'); self.fresh()

    def add(self, comment, **extra):
        import_manual(self.root, dict(platform='youtube', account='owner', channel_id='channel', items=[
            dict(comment_id=comment, video_id='video', thread_id='thread-'+comment,
                 body='想练习里面的一句英语。', is_own=False, **extra)]))

    def fresh(self, state='complete', stamp=None):
        with open_db(self.root) as db:
            db.execute('UPDATE interaction_sources SET state=?,last_success_at=?',
                       (state, stamp or self.time.isoformat()))

    def run_cycle(self, client=None, **kw):
        return c.run(self.root, kw.get('policy', self.p), kw.get('index', self.index), self.model,
                     client=client or Mock(return_value=(dict(self.answer), {'total_tokens': 10})),
                     clock=kw.get('clock', lambda: self.time))

    def test_new_comment_drafted_once_and_never_sent(self):
        model = Mock(return_value=(dict(self.answer), {'total_tokens': 10}))
        first = self.run_cycle(model); self.fresh(stamp=(self.time+timedelta(seconds=1)).isoformat())
        self.time += timedelta(seconds=2)
        second = self.run_cycle(model)
        self.assertEqual((first['model_requests'], second['model_requests']), (1, 0))
        self.assertEqual(model.call_count, 1)
        self.assertFalse(first['sent']); self.assertFalse(first['authorized'])
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM interaction_drafts').fetchone()[0], 1)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='interaction_dispatches'").fetchone())

    def test_empty_or_own_inbox_costs_no_requests(self):
        with open_db(self.root) as db: db.execute('UPDATE interaction_items SET is_own=1')
        model = Mock(side_effect=AssertionError('must not request'))
        r = self.run_cycle(model)
        self.assertEqual(r['model_requests'], 0); model.assert_not_called()

    def test_failed_partial_manual_or_missing_source_never_calls_model(self):
        model = Mock(side_effect=AssertionError('must not request'))
        for state in ('error', 'partial', 'running', 'manual'):
            self.fresh(state); r = self.run_cycle(model)
            self.assertEqual(r['state'], 'waiting_for_sync')
        with open_db(self.root) as db: db.execute('DELETE FROM interaction_sources')
        self.assertEqual(self.run_cycle(model)['state'], 'waiting_for_sync')
        model.assert_not_called()

    def test_stale_future_naive_and_empty_success_times_rejected(self):
        model = Mock(side_effect=AssertionError('must not request'))
        for stamp in ((self.time-timedelta(hours=2)).isoformat(), (self.time+timedelta(seconds=1)).isoformat(),
                      '2026-10-29T07:00:00', ''):
            with open_db(self.root) as db: db.execute('UPDATE interaction_sources SET last_success_at=?', (stamp,))
            self.assertEqual(self.run_cycle(model)['state'], 'waiting_for_sync')
        model.assert_not_called()

    def test_empty_index_wrong_binding_and_disabled_policy_rejected_before_model(self):
        model = Mock(side_effect=AssertionError('must not request'))
        for index in ({'schema':'interaction-work-index.v1','works':[]},
                      {'schema':'interaction-work-index.v1','works':[{**self.index['works'][0], 'channel_id':'other'}]}):
            with self.assertRaises(PublishError): self.run_cycle(model, index=index)
        for field, value in [('enabled', False), ('max_requests_per_day', False), ('max_source_age_seconds', 0)]:
            pol = {**self.p, field:value}
            with self.assertRaises(PublishError): self.run_cycle(model, policy=pol)
        model.assert_not_called()

    def test_cycle_and_daily_limits_include_cached_results_without_spending_again(self):
        self.add('second'); self.add('third'); self.fresh()
        self.p.update(max_requests_per_cycle=1, max_requests_per_day=2)
        model = Mock(return_value=(dict(self.answer), {}))
        a = self.run_cycle(model); b = self.run_cycle(model); d = self.run_cycle(model)
        self.assertEqual((a['model_requests'], b['model_requests'], d['model_requests']), (1,1,0))
        self.assertEqual(d['state'], 'budget_exhausted'); self.assertEqual(model.call_count, 2)
        self.time += timedelta(days=1); self.fresh()
        r = self.run_cycle(model)
        self.assertEqual(r['model_requests'],1); self.assertEqual(r['daily_requests'],1)

    def test_failed_request_is_charged_once_and_circuit_survives_restart(self):
        bad = Mock(side_effect=ValueError('fixture-secret response'))
        a = self.run_cycle(bad)
        self.assertEqual(a['state'], 'circuit_open'); self.assertEqual(a['daily_requests'], 1)
        self.assertNotIn('fixture-secret', str(a)); self.assertEqual(bad.call_count, 1)
        good = Mock(return_value=(dict(self.answer), {}))
        b = self.run_cycle(good)
        self.assertEqual(b['state'], 'circuit_open'); good.assert_not_called()
        r = c.reset(self.root, self.p, '已核对提供方问题与缓存')
        self.assertFalse(r['budget_reset'])
        self.assertEqual(self.run_cycle(good)['daily_requests'], 2)

    def test_invalid_model_result_opens_circuit(self):
        r = self.run_cycle(Mock(return_value=({'action':'send'}, {})))
        self.assertEqual(r['state'], 'circuit_open')
        self.assertFalse(r['sent'])

    def test_interrupted_request_remains_charged_and_frozen(self):
        def killed(*args): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_cycle(killed)
        r = self.run_cycle(Mock(side_effect=AssertionError('no retry')))
        self.assertEqual(r['state'],'circuit_open'); self.assertEqual(r['daily_requests'],1)
        with open_db(self.root) as db:
            row = db.execute("SELECT state FROM interaction_draft_cycle_runs WHERE model_requests=1").fetchone()
            self.assertEqual(row[0], 'interrupted')

    def test_model_time_comment_change_does_not_create_stale_draft(self):
        def changed(*args):
            with open_db(self.root) as db: db.execute("UPDATE interaction_items SET body='changed'")
            return dict(self.answer), {}
        r = self.run_cycle(changed)
        self.assertEqual(r['model_requests'],1)
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM interaction_drafts').fetchone()[0],0)

    def test_source_rechecked_immediately_before_model_request(self):
        n = 0
        def clock():
            nonlocal n
            n += 1
            if n == 2: self.fresh('error')
            return self.time
        model = Mock(side_effect=AssertionError('no request'))
        r = self.run_cycle(model, clock=clock)
        self.assertEqual(r['state'], 'waiting_for_sync'); self.assertEqual(r['model_requests'],0)
        model.assert_not_called()

    def test_beijing_midnight_request_uses_actual_request_day(self):
        start = datetime(2026,10,29,15,59,59,tzinfo=timezone.utc)
        finish = start + timedelta(seconds=2)
        self.time = start; self.fresh()
        clock = Mock(side_effect=[start, finish, finish])
        r = self.run_cycle(clock=clock)
        self.assertEqual(r['beijing_day'], '2026-10-30')
        self.assertEqual(r['daily_requests'],1)
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT beijing_day FROM interaction_draft_cycle_budget').fetchone()[0], '2026-10-30')

    def test_physical_channel_lock_blocks_aliases_and_reset_during_run(self):
        with c._cycle_lock(self.root, 'youtube', 'channel'):
            with self.assertRaises(PublishError): self.run_cycle()
            with self.assertRaises(PublishError): c.reset(self.root, self.p, 'check')

    def test_finite_watch_reuses_cache_and_does_not_collide_with_read_monitor(self):
        from interaction_monitor import Monitor, status
        model = Mock(return_value=(dict(self.answer), {})); rows = []
        with Monitor(self.root, 'youtube', 'owner', 'channel', interval=1800, cycles=3, clock=lambda:self.time):
            r = c.watch(self.root, lambda:self.p, lambda:self.index, lambda:self.model,
                        client=model, cycles=3, sleep_fn=lambda _:None,
                        clock=lambda:self.time, emit=rows.append)
        self.assertEqual(r['cycles'],3); self.assertEqual(r['model_requests'],1)
        self.assertEqual([row['model_requests'] for row in rows],[1,0,0])
        self.assertFalse(r['sent'])
        s = status(self.root,'draft-youtube','owner','channel',clock=lambda:self.time)
        self.assertEqual(s['state'],'completed'); self.assertEqual(s['cycles'],3)

    def test_watch_stops_on_model_failure_and_reloads_disable_policy(self):
        bad = Mock(side_effect=ValueError('provider secret'))
        r = c.watch(self.root, lambda:self.p, lambda:self.index, lambda:self.model,
                    client=bad, cycles=49, sleep_fn=lambda _:self.fail('must stop'),
                    clock=lambda:self.time, emit=lambda _:None)
        self.assertEqual(r['state'],'circuit_open'); self.assertEqual(r['cycles'],1)
        c.reset(self.root,self.p,'已核对')
        pol = Mock(side_effect=[self.p,self.p,self.p,{**self.p,'enabled':False}])
        good = Mock(return_value=(dict(self.answer),{}))
        r = c.watch(self.root, pol, lambda:self.index, lambda:self.model,
                    client=good, cycles=3, sleep_fn=lambda _:None,
                    clock=lambda:self.time, emit=lambda _:None)
        self.assertEqual(r['state'],'stopped'); self.assertEqual(good.call_count,1)

    def test_policy_disable_between_requests_stops_remaining_budget(self):
        self.add('second'); self.fresh(); current = dict(self.p)
        def model(*args):
            current['enabled'] = False
            return dict(self.answer), {}
        r = c.run(self.root,self.p,self.index,self.model,client=model,clock=lambda:self.time,
                  policy_reader=lambda:current)
        self.assertEqual(r['state'],'policy_changed')
        self.assertEqual(r['model_requests'],1)

    def test_dashboard_distinguishes_draft_pause_from_successful_read(self):
        from interaction_dashboard import render
        self.run_cycle(Mock(side_effect=ValueError('secret provider body')))
        page = render(self.root/'interaction-inbox.sqlite3')
        self.assertIn('拟稿暂停',page)
        self.assertIn('本轮模型请求 1 次',page)
        self.assertIn('拟稿不代表发送',page)
        self.assertIn('模型失败；持续拟稿已暂停',page)
        self.assertNotIn('secret provider body',page)


if __name__ == '__main__':
    unittest.main()
