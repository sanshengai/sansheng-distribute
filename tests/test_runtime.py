"""Runtime dependency and profile-connection boundaries, without remote mutations."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import runtime
from browser_runtime import endpoint, assert_profile, profile_lease, select_page
from publish_core import PublishError


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_no_browser_platform_never_installs_or_probes(self):
        for profile in ({}, {'configured_modules':[]}, {'configured_modules':['wechat','bilibili']}):
            with patch.object(runtime.subprocess, 'run', side_effect=AssertionError('must remain silent')):
                result = runtime.prepare(profile, self.root/'runtime')
            self.assertTrue(result['ok']); self.assertFalse(result['required'])
            self.assertFalse((self.root/'runtime').exists())

    def test_custom_install_directory_must_be_bound_before_install(self):
        from publish_core import write_json
        config = self.root/'profile.json'
        write_json(config, {'configured_modules':['x'], 'accounts':{}, 'adapters':{}})
        result = subprocess.run([sys.executable, str(ROOT/'setup.py'), '--profile', str(config),
            '--prepare-runtime', '--runtime-dir', str(self.root/'unused')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('runtime_dir', json.loads(result.stdout)['error'])
        self.assertFalse((self.root/'unused').exists())
        self.assertNotIn('runtime_dir', json.loads(config.read_text()))

    def test_prefill_cli_reexecutes_private_environment_before_browser_import(self):
        import browser_prefill
        profile = {'configured_modules':['x'], 'runtime_dir':str(self.root/'runtime')}
        with patch.object(sys, 'argv', ['browser_prefill.py', '/private/request.json', '--profile', '/private/profile.json']), \
                patch.object(browser_prefill, 'load_profile', return_value=profile), \
                patch.object(runtime, 'require_python', return_value=str(self.root/'runtime/bin/python3')), \
                patch.object(browser_prefill.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as child, \
                patch.object(browser_prefill, 'execute', side_effect=AssertionError('must not import browser in system Python')):
            self.assertEqual(browser_prefill.main(), 0)
        command = child.call_args.args[0]
        self.assertEqual(command[0], str(self.root/'runtime/bin/python3'))
        self.assertIn('browser_prefill', command)
        self.assertIn('-I', command)

    def test_reuse_requires_actual_versions_and_current_lock(self):
        root = self.root/'runtime'; (root/'bin').mkdir(parents=True)
        (root/'bin/python3').touch()
        (root/'runtime.json').write_text(json.dumps({'requirements_sha256':hashlib.sha256(runtime.LOCK.read_bytes()).hexdigest()}))
        profile = {'configured_modules':['x'], 'runtime_dir':str(root)}
        good = subprocess.CompletedProcess([], 0, json.dumps(runtime.VERSIONS))
        with patch.object(runtime.subprocess, 'run', return_value=good), patch.object(runtime.venv.EnvBuilder, 'create', side_effect=AssertionError('no reinstall')):
            self.assertTrue(runtime.prepare(profile)['reused'])
        bad = subprocess.CompletedProcess([], 0, json.dumps(dict(runtime.VERSIONS, playwright='0.0.0')))
        with patch.object(runtime.subprocess, 'run', return_value=bad):
            self.assertFalse(runtime.inspect(profile)['ok'])
            with self.assertRaisesRegex(PublishError, '保留原目录'): runtime.prepare(profile)
        (root/'runtime.json').write_text('{}')
        with patch.object(runtime.subprocess, 'run', side_effect=AssertionError('lock drift should stop first')):
            self.assertFalse(runtime.inspect(profile)['ok'])

    def test_loopback_endpoint_and_actual_profile_are_both_required(self):
        self.assertEqual(endpoint('http://127.0.0.1:9333/'), 'http://127.0.0.1:9333')
        for value in ('', None, 'https://localhost:9333', 'http://127.0.0.1:9333/a',
                      'http://127.0.0.1.evil.example:9333', 'http://x@localhost:9333', 'http://localhost:wrong'):
            with self.subTest(value=value), self.assertRaises(PublishError): endpoint(value)
        expected = str(self.root/'dedicated')
        class Session:
            def send(self, method): return {'arguments':['chrome', '--user-data-dir='+expected]}
        assert_profile(Session(), expected)
        with self.assertRaisesRegex(PublishError, '专用 profile'): assert_profile(Session(), self.root/'main-browser')

    def test_same_browser_is_serialized_and_lease_releases(self):
        with profile_lease(self.root/'browser', directory=self.root/'locks', timeout=0):
            with self.assertRaisesRegex(PublishError, '不并行写入'):
                with profile_lease(self.root/'browser', directory=self.root/'locks', timeout=0): pass
            with profile_lease(self.root/'different', directory=self.root/'locks', timeout=0): pass
        with profile_lease(self.root/'browser', directory=self.root/'locks', timeout=0): pass

    def test_page_matching_refuses_duplicates_wrong_origin_and_empty(self):
        class Page:
            def __init__(self, url): self.url = url
        class Context:
            def __init__(self, pages): self.pages = pages
        class Connection:
            def __init__(self, pages): self.contexts = [Context(pages)]
        good = Page('https://x.com/compose/post')
        self.assertIs(select_page(Connection([good]), 'x', path='/compose/post'), good)
        for pages in ([], [good,good], [Page('https://x.com.evil.test/compose/post')]):
            with self.assertRaises(PublishError): select_page(Connection(pages), 'x', path='/compose/post')


if __name__ == '__main__': unittest.main()
