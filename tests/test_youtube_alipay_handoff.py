"""Offline consumer-path checks; never contacts a platform or posts media."""
import copy
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from environment import PLATFORMS, initialize, load_profile
from publish_core import PublishError, validate
from platform_pack import build_pack


class VideoHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'video.mp4').write_bytes(b'video fixture')
        (self.root / 'cover.png').write_bytes(b'cover fixture')

    def manifest(self, platform):
        meta = {'made_for_kids': False, 'visibility': 'unlisted', 'playlist_name': 'Series',
                'altered_content': True} if platform == 'youtube' else {
                    'collection_name': 'Series', 'ai_generated': True}
        return {'schema_version': 1, 'job_id': 'video', 'producer': 'test',
                'content_status': 'final', 'source_root': str(self.root), 'documents': [],
                'assets': [{'id': 'video', 'path': 'video.mp4', 'kind': 'video'},
                           {'id': 'cover', 'path': 'cover.png', 'kind': 'image'}],
                'targets': [{'id': 'item', 'platform': platform, 'account': platform + '-main',
                             'content_type': 'video', 'endpoint': 'publish', 'title': 'Episode',
                             'text': 'Description', 'asset_ids': ['video', 'cover'],
                             'cover_asset_id': 'cover', 'platform_metadata': meta}], 'media_slots': []}

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/publish.py'), *map(str, args)],
                              text=True, capture_output=True, check=False)

    def test_cli_seal_check_pack_and_disabled_preflight(self):
        for platform in ('youtube', 'alipay'):
            with self.subTest(platform=platform):
                spec = self.root / f'{platform}-spec.json'
                sealed = self.root / f'{platform}-sealed.json'
                output = self.root / f'{platform}-pack.json'
                profile = self.root / f'{platform}-profile.json'
                manifest = self.manifest(platform)
                spec.write_text(json.dumps(manifest), encoding='utf-8')
                result = self.cli('seal', spec, '--output', sealed)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = self.cli('check', sealed)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                initialize(profile, platforms=(platform,))
                self.assertFalse(load_profile(profile)['adapters'][platform]['enabled_capabilities'])
                result = self.cli('--profile', profile, 'pack', sealed, '--target', 'item', '--output', output)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                pack = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(pack['platform_metadata'], manifest['targets'][0]['platform_metadata'])
                self.assertEqual(pack['cover_asset_id'], 'cover')
                self.assertIn('仅提供本地交接', ' '.join(pack['warnings']))
                result = self.cli('--profile', profile, 'preflight', sealed, '--target', 'item')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('尚未启用/验收', result.stdout)

    def test_youtube_never_infers_audience_or_visibility(self):
        for field, value in [('made_for_kids', None), ('made_for_kids', 'false'),
                             ('visibility', None), ('visibility', 'everyone'),
                             ('visibility', []), ('playlist_name', ''),
                             ('altered_content', 'true')]:
            m = self.manifest('youtube')
            m['targets'][0]['platform_metadata'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(PublishError):
                validate(m, sealing=True)
        for field in ('made_for_kids', 'visibility'):
            m = self.manifest('youtube')
            del m['targets'][0]['platform_metadata'][field]
            with self.subTest(field=field), self.assertRaises(PublishError):
                validate(m, sealing=True)
        m = self.manifest('youtube')
        m['targets'][0]['platform_metadata']['visibility'] = 'private'
        m['targets'][0]['platform_metadata']['made_for_kids'] = True
        validate(m, sealing=True)
        self.assertEqual(build_pack(m, 'item')['platform_metadata']['visibility'], 'private')

    def test_cli_blocks_missing_youtube_audience_and_invalid_privacy(self):
        for field, invalid in [('made_for_kids', None), ('visibility', 'invalid')]:
            with self.subTest(field=field):
                m = self.manifest('youtube')
                if invalid is None:
                    del m['targets'][0]['platform_metadata'][field]
                else:
                    m['targets'][0]['platform_metadata'][field] = invalid
                spec = self.root / f'{field}.json'
                output = self.root / f'{field}-sealed.json'
                spec.write_text(json.dumps(m), encoding='utf-8')
                result = self.cli('seal', spec, '--output', output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(field, result.stdout)
                self.assertFalse(output.exists())

    def test_alipay_unknown_declaration_is_not_false(self):
        m = self.manifest('alipay')
        m['targets'][0]['platform_metadata'] = {}
        validate(m, sealing=True)
        pack = build_pack(m, 'item')
        self.assertNotIn('ai_generated', pack['platform_metadata'])
        for field, value in [('ai_generated', 'false'), ('ai_generated', None), ('collection_name', ' ')]:
            candidate = copy.deepcopy(m)
            candidate['targets'][0]['platform_metadata'][field] = value
            with self.subTest(field=field), self.assertRaises(PublishError):
                validate(candidate)

    def test_missing_or_changed_cover_rejected_by_real_cli(self):
        m = self.manifest('youtube')
        m['targets'][0]['asset_ids'] = ['video']
        m['assets'] = [m['assets'][0]]
        spec = self.root / 'broken.json'
        spec.write_text(json.dumps(m), encoding='utf-8')
        result = self.cli('seal', spec, '--output', self.root / 'not-sealed.json')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('封面', result.stdout)
        m = self.manifest('youtube')
        validate(m, sealing=True)
        (self.root / 'cover.png').write_bytes(b'changed')
        with self.assertRaisesRegex(PublishError, '源文件已改变'):
            build_pack(m, 'item')

    def test_setup_interactive_uses_pty_and_keeps_platforms_inactive(self):
        profile = self.root / 'interactive.json'
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, str(ROOT / 'setup.py'), '--profile', str(profile),
                                 '--state-dir', str(self.root / 'state')], stdin=slave,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        os.close(slave)
        try:
            answers = [('y' if p in {'youtube', 'alipay'} else 'n') for p in PLATFORMS]
            answers += ['main', str(self.root / 'dedicated'), 'channel-id', 'life-account-id']
            os.write(master, ('\n'.join(answers) + '\n').encode())
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 0, stdout + stderr)
        config = load_profile(profile)
        self.assertEqual(set(config['configured_modules']), {'youtube', 'alipay'})
        for platform in ('youtube', 'alipay'):
            self.assertEqual(config['adapters'][platform]['enabled_capabilities'], [])
            self.assertFalse(config['adapters'][platform]['enabled'])


if __name__ == '__main__':
    unittest.main()
