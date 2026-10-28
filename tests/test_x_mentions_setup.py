import json
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError
import x_mentions_setup as setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'x-private' / 'profile.json'
        self.state = self.root / 'state'

    def tearDown(self):
        self.temp.cleanup()

    def answers(self):
        return iter(['y', 'example-account', '12345', str(self.state), 'TEST_X_ACCESS_TOKEN'])

    def test_default_off_and_non_tty_does_not_prompt_or_write(self):
        self.assertEqual(setup.diagnose(self.config)['checks'], [])
        output = []
        result = setup.interactive(self.config, is_tty=False, input_fn=lambda _:self.fail('prompted'), output=output.append)
        self.assertFalse(result['ok'])
        self.assertFalse(self.config.exists())

    def test_decline_does_not_create_config_or_network(self):
        result = setup.interactive(self.config, is_tty=True, input_fn=lambda _: 'n', output=lambda _:None)
        self.assertFalse(result['enabled'])
        self.assertFalse(self.config.exists())

    def test_interactive_roundtrip_no_secret_and_repeat_skips(self):
        answers = self.answers()
        with patch.dict(os.environ, {'TEST_X_ACCESS_TOKEN':'sensitive-test-value'}):
            result = setup.interactive(self.config, is_tty=True, input_fn=lambda _:next(answers), output=lambda _:None)
            self.assertTrue(result['ok'])
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode),0o600)
            content = self.config.read_text()
            self.assertNotIn('sensitive-test-value',content)
            self.assertEqual(json.loads(content)['token_env'],'TEST_X_ACCESS_TOKEN')
            setup.interactive(self.config, is_tty=False, input_fn=lambda _:self.fail('prompted'), output=lambda _:None)

    def test_missing_credential_diagnostic_without_secret_or_network(self):
        answers = self.answers()
        with patch.dict(os.environ, {}, clear=True):
            setup.interactive(self.config, is_tty=True, input_fn=lambda _:next(answers), output=lambda _:None)
            result = setup.diagnose(self.config)
        self.assertFalse(result['ok'])
        self.assertFalse(result['checks'][0]['ok'])

    def test_run_requires_explicit_paid_gate_and_uses_environment_name_only(self):
        answers = self.answers()
        with patch.dict(os.environ, {'TEST_X_ACCESS_TOKEN':'sensitive-test-value'}):
            setup.interactive(self.config, is_tty=True, input_fn=lambda _:next(answers), output=lambda _:None)
            with self.assertRaises(PublishError):
                setup.run(self.config, runner=lambda *a,**k:self.fail('ran'))
            calls=[]
            class Result: returncode=0
            self.assertEqual(setup.run(self.config, True, runner=lambda args,**kw:(calls.append(args) or Result())),0)
        self.assertIn('--accept-paid-api',calls[0])
        self.assertIn('TEST_X_ACCESS_TOKEN',calls[0])
        self.assertNotIn('sensitive-test-value',calls[0])

    def test_edit_decline_keeps_old_config(self):
        answers=self.answers()
        setup.interactive(self.config, is_tty=True, input_fn=lambda _:next(answers), output=lambda _:None)
        before=self.config.read_bytes()
        output=[]
        result=setup.interactive(self.config, edit=True, is_tty=True, input_fn=lambda _:'n', output=output.append)
        self.assertEqual(before,self.config.read_bytes())
        self.assertTrue(result['enabled'])
        self.assertIn('当前仍启用',output[0])

    def test_reject_public_or_symlink_config(self):
        with self.assertRaises(PublishError):
            setup._config_path(setup.ROOT / 'profile.json')
        p=self.root / 'link'
        p.symlink_to(self.root / 'elsewhere')
        with self.assertRaises(PublishError): setup._config_path(p)

    def test_disable_is_silent_in_diagnosis_and_can_reenable(self):
        answers=self.answers()
        setup.interactive(self.config, is_tty=True, input_fn=lambda _:next(answers), output=lambda _:None)
        with patch.object(sys,'argv',['x_mentions_setup.py','--config',str(self.config),'disable']), \
             patch('builtins.print'):
            self.assertEqual(setup.main(),0)
        self.assertEqual(setup.diagnose(self.config)['checks'],[])
        with self.assertRaises(PublishError): setup.run(self.config, True)
        answers=self.answers()
        setup.interactive(self.config, is_tty=True, input_fn=lambda _:next(answers), output=lambda _:None)
        self.assertTrue(setup._read_config(self.config)['enabled'])

    def test_cli_explains_paid_gate_without_leaking_or_running(self):
        error = io.StringIO()
        with patch.object(sys, 'argv', ['x_mentions_setup.py', '--config', str(self.config), 'run']), \
             patch('sys.stderr', error), patch('subprocess.run') as runner:
            self.assertEqual(setup.main(), 1)
        self.assertIn('可能收费', error.getvalue())
        self.assertIn('未联网', error.getvalue())
        self.assertNotIn(str(self.config), error.getvalue())
        runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
