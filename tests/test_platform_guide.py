import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from platform_guide import platform_guide
from publish_core import PublishError, adapter_config, validate


class PlatformGuideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / 'profile.json'
        self.state = self.root / 'does-not-exist'

    def cli(self, platform, profile=None):
        if profile is not None:
            self.profile.write_text(json.dumps(profile), encoding='utf-8')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('SANDY_PUBLISH_', 'SANSHENG_DISTRIBUTE_'))}
        env['HOME'] = str(self.root)
        cmd = [sys.executable, str(ROOT / 'scripts/publish.py'), '--state-dir', str(self.state)]
        if profile is not None:
            cmd.extend(['--profile', str(self.profile)])
        return subprocess.run(cmd + ['platform-guide', platform], capture_output=True, text=True, env=env)

    def test_cli_read_only_and_no_configuration(self):
        result = self.cli('wechat')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)['result']
        self.assertEqual(data['accepted_content_types'], ['article'])
        self.assertTrue(data['wechat_article_guidance'])
        self.assertFalse(data['local_pack_available'])
        self.assertEqual(data['configured_policy'], {})
        self.assertFalse(data['remote_checked'])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.profile.exists())

    def test_valid_config_does_not_leak_sensitive_data(self):
        secret = 'very-secret-token-not-to-print'
        profile = {'platform_policies': {'youtube': {'max_lengths': {'title': 100},
                     'allowed_extensions': {'video': ['.mp4', '.mov']}, 'require_cover': True}},
                   'accounts': {'private-name': {'cookie': secret}},
                   'adapters': {'youtube': {'enabled': True, 'enabled_capabilities': ['video:publish'],
                                            'api_key': secret, 'execute': ['/secret/path']}}}
        result = self.cli('youtube', profile)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)['result']
        self.assertEqual(data['accepted_content_types'], ['video'])
        self.assertEqual(data['configured_policy'], profile['platform_policies']['youtube'])
        self.assertEqual(data['enabled_capabilities_config'], ['video:publish'])
        self.assertFalse(data['configured_policy_redacted'])
        self.assertEqual(data['required_platform_metadata']['made_for_kids']['type'], 'boolean')
        self.assertEqual(data['required_platform_metadata']['visibility']['values'], ['public', 'unlisted', 'private'])
        self.assertIn('不代表官方当前规格', data['notice'])
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn('private-name', result.stdout + result.stderr)
        self.assertNotIn('/secret/path', result.stdout + result.stderr)
        self.assertFalse(self.state.exists())

    def test_unknown_and_malformed_inputs_rejected_without_echoing_values(self):
        self.assertEqual(self.cli('not-real').returncode, 2)
        bad = [
            {'platform_policies': {'x': {'unknown': 'private-secret'}}},
            {'platform_policies': {'x': {'max_lengths': {'title': True}}}},
            {'platform_policies': {'x': {'allowed_extensions': {'video': ['missing-dot']}}}},
            {'adapters': {'x': {'enabled': 'yes'}}},
            {'adapters': {'x': {'enabled_capabilities': ['private-secret']}}},
        ]
        for profile in bad:
            with self.subTest(profile=profile):
                result = self.cli('x', profile)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn('private-secret', result.stdout + result.stderr)
                self.assertFalse(self.state.exists())

    def test_conflicting_old_and_new_state_dirs_do_not_block_guide(self):
        for name in ('sandy-publish', 'sansheng-distribute'):
            (self.root / '.local/state' / name).mkdir(parents=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('SANDY_PUBLISH_', 'SANSHENG_DISTRIBUTE_'))}
        env['HOME'] = str(self.root)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/publish.py'), 'platform-guide', 'x'],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / '.config').exists())

    def test_policy_bad_types_fail_closed(self):
        for rule in ({'require_cover': 1}, {'max_assets': {'video': -1}},
                     {'required_fields': ['password']}, {'allowed_extensions': {'video': []}}):
            with self.subTest(rule=rule), self.assertRaises(PublishError):
                platform_guide('x', {'platform_policies': {'x': rule}})

    def test_missing_explicit_profile_is_error_and_never_mistaken_for_empty(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/publish.py'),
                                 '--profile', str(self.profile), 'platform-guide', 'x'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('配置文件不存在', result.stdout)
        self.assertFalse(self.profile.exists())

    def test_pack_accepted_unusual_extensions_are_hidden_in_guide(self):
        from platform_pack import build_pack
        source = self.root / 'video.字幕'
        source.write_bytes(b'fixture')
        manifest = {'schema_version': 1, 'job_id': 'test', 'producer': 'test',
                    'content_status': 'final', 'source_root': str(self.root), 'documents': [],
                    'assets': [{'id': 'v', 'path': source.name, 'kind': 'video'}],
                    'targets': [{'id': 't', 'platform': 'x', 'account': 'account',
                                 'endpoint': 'prefill', 'content_type': 'video', 'text': 'example',
                                 'asset_ids': ['v']}], 'media_slots': []}
        validate(manifest, sealing=True)
        profile = {'platform_policies': {'x': {'allowed_extensions': {'video': ['.字幕', '.mp4']}}}}
        self.assertEqual(build_pack(manifest, 't', profile)['target_id'], 't')
        result = platform_guide('x', profile)
        self.assertEqual(result['configured_policy']['allowed_extensions']['video'],
                         ['<未展示的其他扩展名>', '.mp4'])
        self.assertTrue(result['configured_policy_redacted'])
        self.assertNotIn('.字幕', json.dumps(result, ensure_ascii=False))

    def test_enabled_flag_is_not_execution_gate(self):
        profile = {'accounts': {'account': {'platforms': ['x']}},
                   'adapters': {'x': {'enabled': False, 'enabled_capabilities': ['text:draft'],
                                      'execute': ['placeholder'], 'verify': ['placeholder']}}}
        target = {'platform': 'x', 'account': 'account', 'content_type': 'text', 'endpoint': 'draft'}
        adapter_config(profile, target)
        result = platform_guide('x', profile)
        self.assertFalse(result['adapter_enabled_config'])
        self.assertEqual(result['enabled_capabilities_config'], ['text:draft'])
        self.assertIn('不是执行总闸', result['adapter_enabled_config_semantics'])

    def test_youtube_required_fields_agree_with_real_validator(self):
        (self.root / 'video.mp4').write_bytes(b'video')
        manifest = {'schema_version': 1, 'job_id': 'test', 'producer': 'test', 'content_status': 'final',
                    'source_root': str(self.root), 'documents': [], 'media_slots': [],
                    'assets': [{'id': 'v', 'path': 'video.mp4', 'kind': 'video'}],
                    'targets': [{'id': 't', 'platform': 'youtube', 'account': 'account',
                                 'endpoint': 'publish', 'content_type': 'video', 'title': 'Video',
                                 'asset_ids': ['v'], 'platform_metadata': {}}]}
        spec = platform_guide('youtube')['required_platform_metadata']
        for field in spec:
            with self.subTest(field=field), self.assertRaisesRegex(PublishError, field):
                validate(manifest, sealing=True)
            manifest['targets'][0]['platform_metadata'][field] = False if field == 'made_for_kids' else 'private'
        validate(manifest, sealing=True)


if __name__ == '__main__':
    unittest.main()
