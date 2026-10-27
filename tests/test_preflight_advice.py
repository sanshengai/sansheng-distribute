import json
import subprocess
import sys
import unittest
from pathlib import Path

import test_campaign as fixture
import test_platform_pack as pack_fixture
from campaign import preflight_campaign
from preflight_advice import explain_known_blocker
from publish_core import write_json


class PreflightAdviceTests(unittest.TestCase):
    setUp = fixture.CampaignTests.setUp
    calls = fixture.CampaignTests.calls

    def test_batch_and_cli_report_length_without_exposing_text_or_writing(self):
        self.profile['platform_policies'] = {'x': {'max_lengths': {'text': 3}}}
        before = self.store.status()
        report = preflight_campaign(self.path, self.profile)
        x, weibo = report['targets']
        self.assertFalse(x['local_ready'])
        self.assertEqual(x['issue']['category'], 'length')
        self.assertEqual(x['issue']['field'], 'text')
        self.assertEqual(x['issue']['actual_count'], len('原定稿文字'))
        self.assertEqual(x['issue']['allowed_max'], 3)
        self.assertNotIn('原定稿文字', json.dumps(x['issue'], ensure_ascii=False))
        self.assertTrue(weibo['local_ready'])
        self.assertNotIn('issue', weibo)
        self.assertEqual(self.store.status(), before)
        self.assertEqual(self.calls(), [])

        config = self.root / 'profile-for-cli.json'
        write_json(config, self.profile)
        unused_state = self.root / 'not-created-by-preflight'
        cli = Path(__file__).resolve().parents[1] / 'scripts' / 'publish.py'
        result = subprocess.run([sys.executable, str(cli), '--profile', str(config),
                                 '--state-dir', str(unused_state), 'batch-preflight',
                                 str(self.path)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['result']['blocked_count'], 1)
        self.assertEqual(payload['result']['targets'][0]['issue']['allowed_max'], 3)
        self.assertFalse(unused_state.exists())
        self.assertEqual(self.calls(), [])

    def test_unknown_errors_keep_original_error_without_invented_advice(self):
        self.profile['adapters']['x']['enabled_capabilities'] = []
        row = preflight_campaign(self.path, self.profile)['targets'][0]
        self.assertFalse(row['local_ready'])
        self.assertIn('尚未启用', row['error'])
        self.assertNotIn('issue', row)
        self.assertIsNone(explain_known_blocker('secret=abc 文件格式不符合配置', {}, {}, {}))
        self.assertIsNone(explain_known_blocker('image 文件格式不符合配置 secret=abc', {}, {}, {}))

    def test_known_media_errors_do_not_copy_private_values(self):
        target = {'platform': 'x', 'asset_ids': ['private-a', 'private-b']}
        manifest = {'assets': [{'id': 'private-a', 'kind': 'image', 'path': 'secret.jpg'},
                               {'id': 'private-b', 'kind': 'image', 'path': 'secret2.jpg'}]}
        profile = {'platform_policies': {'x': {'max_assets': {'image': 1},
                                                'allowed_extensions': {'image': ['.jpg']}}}}
        count = explain_known_blocker('image 数量超过配置', target, manifest, profile)
        self.assertEqual((count['actual_count'], count['allowed_max']), (2, 1))
        self.assertEqual(count['category'], 'asset_count')
        fmt = explain_known_blocker('image 文件格式不符合配置', target, manifest, profile)
        self.assertEqual(fmt['category'], 'asset_format')
        self.assertNotIn('secret', json.dumps([count, fmt], ensure_ascii=False))
        self.assertNotIn('private', json.dumps([count, fmt], ensure_ascii=False))
        self.assertEqual(explain_known_blocker('平台要求封面', target, manifest, profile)['field'], 'cover')
        self.assertEqual(explain_known_blocker('平台要求字幕', target, manifest, profile)['field'], 'subtitle')

    def test_actual_media_count_and_format_checks_report_the_cause(self):
        manifest = pack_fixture.fixture(self.root, 'x', 'text')
        path = self.root / 'media-manifest.json'
        write_json(path, manifest)
        self.profile['accounts']['owner'] = {'platforms': ['x']}
        self.profile['adapters']['x']['enabled_capabilities'] = ['text:prefill']
        self.profile['platform_policies'] = {'x': {'max_assets': {'image': 0}}}
        row = preflight_campaign(path, self.profile)['targets'][0]
        self.assertEqual((row['issue']['category'], row['issue']['actual_count'],
                          row['issue']['allowed_max']), ('asset_count', 1, 0))
        self.profile['platform_policies']['x'] = {'allowed_extensions': {'image': ['.jpg']}}
        row = preflight_campaign(path, self.profile)['targets'][0]
        self.assertEqual(row['issue']['category'], 'asset_format')
        self.assertEqual(row['issue']['field'], 'image')


if __name__ == '__main__':
    unittest.main()
