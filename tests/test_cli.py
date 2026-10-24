"""Exercise installed command entry points with offline platform fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_campaign as campaign_fixture
from publish_core import write_json

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.fx = campaign_fixture.CampaignTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.profile = self.fx.root / 'profile with spaces.json'
        # Relative script paths are resolved against configuration, not cwd.
        for adapter in self.fx.profile['adapters'].values():
            for action in ('execute', 'verify'):
                adapter[action][1] = 'adapter.py'
        write_json(self.profile, self.fx.profile)

    def cli(self, *args, ok=True):
        env = {k:v for k,v in os.environ.items() if not k.startswith(('SANDY_PUBLISH_', 'SANSHENG_DISTRIBUTE_'))}
        result = subprocess.run([sys.executable, str(ROOT/'scripts/publish.py'),
            '--profile', str(self.profile), '--state-dir', str(self.fx.store.directory), *args],
            cwd=self.fx.root, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['ok'], ok)
        return value.get('result', value)

    def test_batch_cli_preserves_partial_authorization_and_no_resubmit(self):
        cid = self.cli('batch-prepare', str(self.fx.path))['campaign_id']
        self.cli('batch-authorize', cid, '--target', 'x', '--statement', 'Offline fixture: selected draft only')
        self.cli('batch-run', cid)
        self.assertEqual(self.fx.calls(), [['x','execute'], ['x','verify']])
        self.cli('batch-resume', cid)
        self.cli('batch-run', cid)
        self.assertEqual(self.fx.calls(), [['x','execute'], ['x','verify']])
        self.fx.path.unlink()
        self.cli('batch-status', cid)

    def test_invalid_platform_policy_stops_before_submission(self):
        self.fx.profile['platform_policies'] = {'x': {'max_lengths': {'text': 1}}}
        write_json(self.profile, self.fx.profile)
        self.cli('authorize', str(self.fx.path), '--target','x','--statement','Offline draft fixture')
        self.cli('run', str(self.fx.path), '--target','x', ok=False)
        self.assertEqual(self.fx.calls(), [])
        self.assertEqual(self.fx.store.status(self.fx.store.key(self.fx.manifest, self.fx.manifest['targets'][0]))['state'], 'prepared')

    def test_pack_keeps_metadata_and_uses_relative_configuration(self):
        output = self.fx.root/'output with spaces.json'
        result = self.cli('pack', str(self.fx.path), '--target','x','--output',str(output))
        self.assertEqual(json.loads(output.read_text()), result)
        self.assertEqual(result['fields']['text'], '原定稿文字')
        self.assertEqual(self.fx.calls(), [])


if __name__ == '__main__':
    unittest.main()
