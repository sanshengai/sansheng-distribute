import copy
import json
import stat
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

import test_campaign as fixture

from campaign import prepare_campaign
from publish_core import PublishError, validate, write_json
from series_report import render_series_report, write_series_report


class _HTMLAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


class SeriesReportTests(unittest.TestCase):
    setUp = fixture.CampaignTests.setUp
    calls = fixture.CampaignTests.calls

    def make_campaign(self):
        self.manifest['job_id'] = 'series-s2-approved'
        first, second = self.manifest['targets']
        first.update(id='S2E01-x', title='S2E01 | first')
        second.update(id='S2E01-weibo', title='S2E01 | first')
        third = copy.deepcopy(first)
        third.update(id='S2E02-x', title='S2E02 | second')
        self.manifest['targets'].append(third)
        validate(self.manifest, sealing=True)
        write_json(self.path, self.manifest)
        self.cid = prepare_campaign(self.path, self.store)['campaign_id']
        with self.store.db:
            for target, state in zip(self.manifest['targets'], ('published', 'prepared', 'submission_unknown')):
                self.store.db.execute('UPDATE jobs SET state=? WHERE key=?',
                                      (state, self.store.key(self.manifest, target)))
        self.spec = {
            'title': '第二季', 'series_job_prefix': 'series-s2-', 'series_code': 'S2',
            'episodes': [1, 2, 3],
            'platforms': [{'platform': 'x', 'account': 'social'},
                          {'platform': 'weibo', 'account': 'social'},
                          {'platform': 'youtube', 'account': '', 'note': '待确认登录'}],
            'targets': [{'campaign_id': self.cid, 'target_id': t['id'], 'episode': e}
                        for t, e in zip(self.manifest['targets'], (1, 1, 2))],
        }

    def make_single_job(self):
        m = copy.deepcopy(self.manifest)
        m['job_id'] = 'series-s2-single'
        m['targets'] = [copy.deepcopy(m['targets'][0])]
        m['targets'][0].update(id='historic-alias', title='旧作品 S2E03 | third')
        validate(m, sealing=True)
        row = self.store.prepare(m, 'historic-alias')
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET state='published' WHERE key=?", (row['key'],))
        return row['key']

    def test_snapshot_reports_each_platform_once_and_distinguishes_missing(self):
        self.make_campaign()
        before = self.store.status()
        result = render_series_report(self.spec, self.store)
        self.assertEqual(result.count('<tr>'), 4)  # header plus three accounts
        for value in ['第 1 集', '第 2 集', '第 3 集', '已发布', '账本待执行', '结果不明', '无记录']:
            self.assertIn(value, result)
        self.assertIn('已记录发布', result)
        self.assertIn('1 / 3', result)
        self.assertIn('尚无对应账本', result)
        self.assertIn('人工备注：待确认登录', result)
        self.assertIn('无记录不代表未发布', result)
        self.assertNotIn('S2E01 | first', result)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.store.status(), before)

    def test_known_single_job_and_snapshot_integrity(self):
        self.make_campaign()
        key = self.make_single_job()
        self.spec['targets'].append({'job_key': key, 'episode': 3})
        self.assertIn('2 / 3', render_series_report(self.spec, self.store))
        snapshot = self.store.directory / 'snapshots' / f'{key}.json'
        value = json.loads(snapshot.read_text())
        value['targets'][0]['title'] = 'S2E03 changed'
        write_json(snapshot, value)
        with self.assertRaises(PublishError):
            render_series_report(self.spec, self.store)

    def test_ambiguous_mapping_wrong_series_and_account_fail_closed(self):
        self.make_campaign()
        cases = []
        x = copy.deepcopy(self.spec)
        x['targets'].append(copy.deepcopy(x['targets'][0]))
        cases.append(x)
        x = copy.deepcopy(self.spec)
        x['episodes'] = [1, 1, 2]
        cases.append(x)
        x = copy.deepcopy(self.spec)
        x['targets'][0]['episode'] = 2
        cases.append(x)
        x = copy.deepcopy(self.spec)
        x['series_job_prefix'] = 'different-series-'
        cases.append(x)
        x = copy.deepcopy(self.spec)
        x['series_code'] = 'S12'
        cases.append(x)
        x = copy.deepcopy(self.spec)
        x['platforms'][0]['account'] = 'wrong'
        cases.append(x)
        x = copy.deepcopy(self.spec)
        x['platforms'][0]['account'] = ''
        cases.append(x)
        x = copy.deepcopy(self.spec)
        x['targets'][0]['target_id'] = 'missing'
        cases.append(x)
        x = copy.deepcopy(self.spec)
        x['targets'][0]['job_key'] = '0' * 64
        cases.append(x)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(PublishError):
                    render_series_report(case, self.store)

    def test_rejects_different_episode_in_identifier_and_title(self):
        self.make_campaign()
        m = copy.deepcopy(self.manifest)
        m['job_id'] = 'series-s2-ambiguous'
        m['targets'] = [copy.deepcopy(m['targets'][0])]
        m['targets'][0]['title'] = 'S2E02 is different'
        validate(m, sealing=True)
        write_json(self.path, m)
        cid = prepare_campaign(self.path, self.store)['campaign_id']
        self.spec['targets'] = [{'campaign_id': cid, 'target_id': 'S2E01-x', 'episode': 1}]
        with self.assertRaises(PublishError):
            render_series_report(self.spec, self.store)

    def test_html_escaping_private_create_and_no_overwrite(self):
        self.make_campaign()
        self.spec['title'] = '<script>alert(1)</script>'
        self.spec['platforms'][2]['note'] = '<img src=x onerror=alert(2)>'
        self.spec['platforms'][2]['reason'] = '<svg onload=alert(3)>'
        self.spec['platforms'][2]['action'] = '<iframe src=x>'
        rendered = render_series_report(self.spec, self.store)
        parser = _HTMLAudit()
        parser.feed(rendered)
        self.assertNotIn('script', parser.tags)
        self.assertNotIn('img', parser.tags)
        self.assertNotIn('svg', parser.tags)
        self.assertNotIn('iframe', parser.tags)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('&lt;img', rendered)
        self.assertIn('&lt;svg', rendered)
        self.assertIn('&lt;iframe', rendered)
        spec_path = self.root / 'private-series.json'
        write_json(spec_path, self.spec)
        out = self.root / 'series.html'
        result = write_series_report(spec_path, self.store, out)
        self.assertFalse(result['remote_checked'])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        content = out.read_bytes()
        with self.assertRaises(PublishError):
            write_series_report(spec_path, self.store, out)
        self.assertEqual(out.read_bytes(), content)

    def test_cli_reads_existing_ledger_without_remote_activity(self):
        self.make_campaign()
        spec_path = self.root / 'series-cli.json'
        write_json(spec_path, self.spec)
        out = self.root / 'series-cli.html'
        cli = Path(__file__).resolve().parents[1] / 'scripts' / 'publish.py'
        before = self.store.status()
        result = subprocess.run([sys.executable, str(cli), '--state-dir', str(self.store.directory),
                                 'series-report', str(spec_path), '--output', str(out)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['result']['remote_checked'])
        self.assertIn('结果不明', out.read_text())
        self.assertIn('aria-label="第 2 集：无记录，', out.read_text())
        self.assertIn('aria-label="第 3 集：无记录，', out.read_text())
        self.assertEqual(self.store.status(), before)
        self.assertEqual(self.calls(), [])

    def test_each_episode_has_its_own_cell_even_when_states_match(self):
        self.make_campaign()
        self.spec['platforms'][0].update(reason='旧账本待查', action='先核对平台')
        rendered = render_series_report(self.spec, self.store)
        # X has published / uncertain / missing: one cell per episode.
        self.assertIn('aria-label="第 1 集：已发布，', rendered)
        self.assertIn('aria-label="第 2 集：结果不明，', rendered)
        # Weibo has a prepared target followed by two unmapped episodes.
        self.assertIn('aria-label="第 1 集：账本待执行，', rendered)
        for ep in (1, 2, 3):
            self.assertIn(f'aria-label="第 {ep} 集：无记录，', rendered)
        self.assertNotIn('colspan=', rendered)
        self.assertIn('<b>原因：</b>旧账本待查', rendered)
        self.assertIn('<b>建议：</b>先核对平台', rendered)
        # Even when every episode is published, every one has its own cell.
        key = self.make_single_job()
        self.spec['targets'].append({'job_key': key, 'episode': 3})
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET state='published' WHERE key=?",
                                  (self.store.key(self.manifest, self.manifest['targets'][2]),))
        rendered = render_series_report(self.spec, self.store)
        for ep in (1, 2, 3):
            self.assertIn(f'aria-label="第 {ep} 集：已发布，', rendered)
        self.assertNotIn('colspan=', rendered)

    def test_external_observations_are_separate_and_do_not_mutate_ledger(self):
        self.make_campaign()
        self.spec['platforms'][2]['account'] = 'yt-account'
        self.spec['external_observations'] = [
            {'platform': 'youtube', 'account': 'yt-account', 'episode': 1,
             'state': 'published', 'evidence': '<img src=x onerror=alert(1)>', 'observed_at': '2026-10-28'},
            {'platform': 'youtube', 'account': 'yt-account', 'episode': 2,
             'state': 'published', 'evidence': '作品管理页人工核对', 'observed_at': '2026-10-28'},
        ]
        before = self.store.status()
        spec_path = self.root / 'external-series.json'
        write_json(spec_path, self.spec)
        out = self.root / 'external-report.html'
        cli = Path(__file__).resolve().parents[1] / 'scripts' / 'publish.py'
        result = subprocess.run([sys.executable, str(cli), '--state-dir', str(self.store.directory),
                                 'series-report', str(spec_path), '--output', str(out)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rendered = out.read_text()
        self.assertIn('第 1 集：已发布，人工核验 2026-10-28', rendered)
        self.assertIn('第 2 集：已发布，人工核验 2026-10-28', rendered)
        self.assertIn('第 3 集：无记录，无记录', rendered)
        self.assertEqual(rendered.count('<span class="source">人工</span>'), 2)
        self.assertIn('并计入已记录发布数', rendered)
        self.assertIn('2 / 3', rendered)
        self.assertIn('&lt;img', rendered)
        parser = _HTMLAudit()
        parser.feed(rendered)
        self.assertNotIn('img', parser.tags)
        self.assertEqual(self.store.status(), before)
        self.assertEqual(self.calls(), [])

    def test_external_invalid_or_conflicting_records_fail_closed(self):
        self.make_campaign()
        valid = {'platform': 'x', 'account': 'social', 'episode': 3, 'state': 'published',
                 'evidence': '作品管理页', 'observed_at': '2026-10-28'}
        cases = []
        for changes in ({'episode': 1}, {'state': 'made_up'}, {'state': None},
                        {'account': ''}, {'platform': 'wrong'}, {'episode': True},
                        {'evidence': ''}, {'observed_at': '2026-02-30'},
                        {'observed_at': '2026-10-28T12:00:00'}, {'extra': 'field'}):
            item = {**valid, **changes}
            cases.append([item])
        cases.append([valid, copy.deepcopy(valid)])
        for observations in cases:
            spec = copy.deepcopy(self.spec)
            spec['external_observations'] = observations
            with self.subTest(observations=observations), self.assertRaises(PublishError):
                render_series_report(spec, self.store)

    def test_external_review_and_recommendation_states_are_not_conflated(self):
        self.make_campaign()
        self.spec['platforms'].append({'platform': 'alipay', 'account': 'creator'})
        self.spec['external_observations'] = [
            {'platform': 'alipay', 'account': 'creator', 'episode': ep, 'state': state,
             'evidence': '后台逐集核对', 'observed_at': '2026-10-28'}
            for ep, state in enumerate(('published_limited', 'reviewing', 'rejected'), 1)
        ]
        before = self.store.status()
        rendered = render_series_report(self.spec, self.store)
        self.assertIn('第 1 集：已发布·限推，人工核验', rendered)
        self.assertIn('第 2 集：审核中，人工核验', rendered)
        self.assertIn('第 3 集：审核未通过，人工核验', rendered)
        self.assertIn('1 / 3', rendered)
        self.assertEqual(self.store.status(), before)
        self.assertEqual(self.calls(), [])

    def test_explicit_not_submitted_needs_evidence_and_does_not_count_as_published(self):
        self.make_campaign()
        self.spec['platforms'][2]['account'] = 'yt-account'
        item = {'platform': 'youtube', 'account': 'yt-account', 'episode': 1,
                'state': 'not_submitted', 'evidence': '本人确认还没上传', 'observed_at': '2026-10-28'}
        self.spec['external_observations'] = [item]
        rendered = render_series_report(self.spec, self.store)
        self.assertIn('第 1 集：未提交，人工核验', rendered)
        self.assertIn('第 2 集：无记录，无记录', rendered)
        self.assertIn('0 / 3', rendered)
        item['evidence'] = ''
        with self.assertRaises(PublishError):
            render_series_report(self.spec, self.store)

    def test_rejects_invalid_reason_or_action(self):
        self.make_campaign()
        for field, value in [('reason', None), ('action', 'x' * 161)]:
            spec = copy.deepcopy(self.spec)
            spec['platforms'][0][field] = value
            with self.subTest(field=field), self.assertRaises(PublishError):
                render_series_report(spec, self.store)


if __name__ == '__main__':
    unittest.main()
