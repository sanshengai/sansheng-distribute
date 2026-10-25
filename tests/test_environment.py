import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import environment as env


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='distribute-config-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / 'home with spaces'
        self.home.mkdir()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def paths(self, **kw):
        return env.resolve_paths(home=self.home, environ={}, **kw)

    def test_platform_names_match_runtime_contract(self):
        from publish_core import PLATFORMS
        self.assertEqual(set(env.PLATFORMS), set(PLATFORMS))

    def test_no_configuration_defaults_are_new_and_readonly(self):
        result = self.paths()
        self.assertEqual(Path(result['profile']), self.home / '.config/sansheng-distribute/profile.json')
        self.assertEqual(Path(result['state_dir']), self.home / '.local/state/sansheng-distribute')
        self.assertFalse(result['profile_exists'])
        self.assertFalse(Path(result['state_dir']).exists())
        self.assertFalse(env.doctor(result, environ={})['ok'])

    def test_malformed_config_is_rejected_without_echoing_content(self):
        path = self.home / 'malformed.json'
        for value in ([], {'accounts': []}, {'adapters': {'x': 'SECRET_TEXT'}},
                      {'configured_modules': 'SECRET_TEXT'}, {'acceptance_runs': None}, {'state_dir': []}):
            self.write(path, value)
            with self.assertRaises(env.EnvironmentError) as raised:
                env.load_profile(path)
            self.assertNotIn('SECRET_TEXT', str(raised.exception))

    def test_legacy_profile_and_state_are_preserved(self):
        profile = self.write(self.home / '.config/sandy-publish/profile.json', {})
        state = self.home / '.local/state/sandy-publish'
        state.mkdir(parents=True)
        (state / 'publish.db').write_text('legacy')
        result = self.paths()
        self.assertEqual(result['profile'], str(profile))
        self.assertEqual(result['state_dir'], str(state))
        self.assertEqual((state / 'publish.db').read_text(), 'legacy')

    def test_existing_legacy_profile_without_ledger_keeps_legacy_default(self):
        self.write(self.home / '.config/sandy-publish/profile.json', {})
        self.assertEqual(self.paths()['state_dir'], str(self.home / '.local/state/sandy-publish'))

    def test_ambiguous_defaults_refuse_and_explicit_choice_wins(self):
        old = self.write(self.home / '.config/sandy-publish/profile.json', {})
        self.write(self.home / '.config/sansheng-distribute/profile.json', {})
        with self.assertRaisesRegex(env.EnvironmentError, '配置同时存在'):
            self.paths()
        old_state = self.home / '.local/state/sandy-publish'
        new_state = self.home / '.local/state/sansheng-distribute'
        old_state.mkdir(parents=True); new_state.mkdir(parents=True)
        with self.assertRaisesRegex(env.EnvironmentError, '状态目录同时存在'):
            self.paths(profile=old)
        self.assertEqual(self.paths(profile=old, state_dir=old_state)['state_dir'], str(old_state))

    def test_environment_and_cli_precedence(self):
        values = {'SANSHENG_DISTRIBUTE_PROFILE': str(self.home / 'new.json'),
                  'SANDY_PUBLISH_PROFILE': str(self.home / 'old.json'),
                  'SANSHENG_DISTRIBUTE_STATE': str(self.home / 'new-state'),
                  'SANDY_PUBLISH_STATE': str(self.home / 'old-state')}
        result = env.resolve_paths(environ=values, home=self.home)
        self.assertEqual(result['profile'], values['SANSHENG_DISTRIBUTE_PROFILE'])
        self.assertEqual(result['state_dir'], values['SANSHENG_DISTRIBUTE_STATE'])
        result = env.resolve_paths(self.home / 'explicit.json', self.home / 'explicit-state', environ=values, home=self.home)
        self.assertEqual(result['profile'], str(self.home / 'explicit.json'))
        del values['SANSHENG_DISTRIBUTE_PROFILE']; del values['SANSHENG_DISTRIBUTE_STATE']
        result = env.resolve_paths(environ=values, home=self.home)
        self.assertEqual(result['profile'], values['SANDY_PUBLISH_PROFILE'])
        self.assertEqual(result['state_dir'], values['SANDY_PUBLISH_STATE'])

    def test_config_relative_paths_do_not_depend_on_working_directory(self):
        path = self.write(self.home / 'private/profile.json', {'state_dir': '../original ledger',
            'accounts': {'a': {'wechat_env_file': 'private.env'}},
            'adapters': {'wechat': {'execute': ['python3', '${SKILL_ROOT}/scripts/wechat_adapter.py', 'execute',
                                               '--profile', 'profile.json', '--write-scripts', '../write scripts']}}})
        before = path.read_bytes()
        loaded = env.load_profile(path)
        self.assertEqual(loaded['accounts']['a']['wechat_env_file'], str(path.parent / 'private.env'))
        self.assertEqual(loaded['adapters']['wechat']['execute'][1], str(ROOT / 'scripts/wechat_adapter.py'))
        self.assertEqual(loaded['adapters']['wechat']['execute'][-1], str(self.home / 'write scripts'))
        self.assertEqual(self.paths(profile=path)['state_dir'], str(self.home / 'original ledger'))
        self.assertEqual(path.read_bytes(), before)

    def test_blank_initialization_is_private_silent_and_refuses_overwrite(self):
        path = self.home / 'private/profile.json'
        env.initialize(path, state_dir=self.home / 'ledger')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        result = env.doctor(self.paths(profile=path), environ={})
        self.assertTrue(result['ok'])
        serialized = json.dumps(result)
        for name in env.PLATFORMS:
            self.assertNotIn(name, serialized)
        before = path.read_bytes()
        with self.assertRaisesRegex(env.EnvironmentError, '拒绝覆盖'):
            env.initialize(path)
        self.assertEqual(before, path.read_bytes())

    def test_bilibili_cli_initialization_is_inactive_and_has_no_browser_dependency(self):
        path = self.home/'private/profile.json'
        env.initialize(path, platforms=['bilibili'], public_identities={'bilibili':'42'},
            bilibili_cli={'directory':'tools/biliup', 'credentials':'auth.json', 'tid':229})
        value = env.load_profile(path)
        adapter = value['adapters']['bilibili']
        self.assertFalse(adapter['enabled']); self.assertEqual(adapter['enabled_capabilities'], [])
        self.assertIn(str(ROOT/'scripts/bilibili_adapter.py'), adapter['execute'])
        self.assertEqual(adapter['cli']['directory'], str(path.parent/'tools/biliup'))
        self.assertFalse((path.parent/'tools').exists())
        self.assertFalse((path.parent/'auth.json').exists())
        with patch('biliup_bridge.probe', return_value={}), patch('bilibili_adapter.credential_header', return_value='fixture'), \
                patch.object(env.shutil, 'which', return_value='/available'):
            self.assertTrue(env.doctor(self.paths(profile=path), environ={})['ok'])
        for platforms, cli in ((['x'], adapter['cli']), (['bilibili'], dict(adapter['cli'],tid=True)),
                               (['bilibili'], dict(adapter['cli'],tid=0)), (['bilibili'], {})):
            bad = self.home/'bad.json'
            with self.assertRaises(env.EnvironmentError): env.initialize(bad, platforms=platforms, bilibili_cli=cli)
            self.assertFalse(bad.exists())

    def test_weibo_bundled_initialization_is_explicit_and_inactive(self):
        path=self.home/'weibo/profile.json'
        env.initialize(path,platforms=['weibo'],public_identities={'weibo':'123456789'},
            browser_profile=self.home/'chrome',browser_engines=['weibo'],
            browser_cdp_url='http://127.0.0.1:9333')
        value=env.load_profile(path);adapter=value['adapters']['weibo']
        self.assertEqual(adapter['enabled_capabilities'],[]);self.assertFalse(adapter['enabled'])
        self.assertIn(str(ROOT/'scripts/browser_engine.py'),adapter['execute'])
        self.assertNotIn('x',value['adapters']);self.assertFalse((self.home/'chrome').exists())
        with patch('runtime.inspect',return_value={'ok':True}), patch.object(env.shutil,'which',return_value='/available'):
            self.assertTrue(env.doctor(self.paths(profile=path),environ={})['ok'])
        absent=self.home/'bad-weibo.json'
        with self.assertRaises(env.EnvironmentError):
            env.initialize(absent,platforms=['x'],browser_engines=['weibo'])
        self.assertFalse(absent.exists())

    def test_douyin_bundled_initialization_is_explicit_and_inactive(self):
        path=self.home/'douyin/profile.json'
        env.initialize(path,platforms=['douyin'],public_identities={'douyin':'fixture_123'},
            browser_profile=self.home/'chrome',browser_engines=['douyin'],
            browser_cdp_url='http://127.0.0.1:9333')
        value=env.load_profile(path);adapter=value['adapters']['douyin']
        self.assertEqual(adapter['enabled_capabilities'],[]);self.assertFalse(adapter['enabled'])
        self.assertIn(str(ROOT/'scripts/browser_engine.py'),adapter['execute'])
        self.assertEqual(set(value['adapters']),{'douyin'})
        self.assertFalse((self.home/'chrome').exists())
        with patch('runtime.inspect',return_value={'ok':True}), patch.object(env.shutil,'which',return_value='/available'):
            self.assertTrue(env.doctor(self.paths(profile=path),environ={})['ok'])

    def test_prepare_tools_uses_only_selected_cli_and_never_logs_in(self):
        spec = importlib.util.spec_from_file_location('distribute_setup_tools', ROOT/'setup.py')
        setup = importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)
        path = self.home/'private/profile.json'
        env.initialize(path, platforms=['bilibili'], public_identities={'bilibili':'42'},
            bilibili_cli={'directory':'tools/biliup', 'credentials':'auth.json', 'tid':229})
        argv = ['--prepare-tools','--profile',str(path)]
        with patch('biliup_bridge.install', return_value={'version':'1.3.4'}) as install, \
                patch('biliup_bridge.login', side_effect=AssertionError('no login')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(argv), 0)
        install.assert_called_once_with(path.parent/'tools/biliup')
        (path.parent/'tools/biliup').mkdir(parents=True)
        with patch('biliup_bridge.install', side_effect=AssertionError('no overwrite')), \
                patch('biliup_bridge.probe', return_value={'version':'1.3.4'}) as probe, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(argv), 0)
        probe.assert_called_once()
        empty = self.write(self.home/'empty.json', {'configured_modules':[], 'accounts':{}, 'adapters':{}})
        with patch('biliup_bridge.install', side_effect=AssertionError('unselected')), \
                patch('biliup_bridge.probe', side_effect=AssertionError('unselected')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(['--prepare-tools','--profile',str(empty)]), 0)
    def test_selected_wechat_checks_missing_dependencies_without_secret_output(self):
        profile = self.home / 'selected/profile.json'
        with patch.object(env, 'discover_write_scripts', return_value=None):
            env.initialize(profile, platforms=['wechat'])
        result = env.doctor(self.paths(profile=profile), environ={'WECHAT_APP_SECRET': 'NEVER_ECHO_THIS'})
        self.assertFalse(result['ok'])
        text = json.dumps(result, ensure_ascii=False)
        self.assertIn('wechat', text)
        self.assertNotIn('NEVER_ECHO_THIS', text)
        self.assertNotIn('douyin', text)
        data = json.loads(profile.read_text())
        self.assertEqual(data['acceptance_runs'], [])
        self.assertEqual(data['adapters']['wechat']['enabled_capabilities'], [])
        self.assertFalse(data['adapters']['wechat']['enabled'])

    def test_single_selected_module_dependencies_and_fingerprint(self):
        scripts = self.home / 'write scripts'
        scripts.mkdir(); (scripts / 'wechat_api.py').write_text('# local dependency')
        secrets = self.home / 'private.env'
        secrets.write_text('WECHAT_APP_ID=app-test\nWECHAT_APP_SECRET=DO_NOT_PRINT\n')
        profile = self.home / 'profile.json'
        env.initialize(profile, platforms=['wechat'], write_scripts=scripts, env_file=secrets,
                       appid_sha256=hashlib.sha256(b'app-test').hexdigest())
        result = env.doctor(self.paths(profile=profile), environ={})
        self.assertTrue(result['ok'], result)
        self.assertNotIn('DO_NOT_PRINT', json.dumps(result))
        secrets.write_text('WECHAT_APP_ID=wrong\nWECHAT_APP_SECRET=DO_NOT_PRINT\n')
        self.assertFalse(env.doctor(self.paths(profile=profile), environ={})['ok'])
        (scripts / 'wechat_api.py').unlink()
        self.assertIn('wechat_api.py', json.dumps(env.doctor(self.paths(profile=profile), environ={})))

    def test_selected_social_module_requires_identity_and_dedicated_browser(self):
        profile = self.home / 'social/profile.json'
        env.initialize(profile, platforms=['x'])
        result = env.doctor(self.paths(profile=profile), environ={})
        self.assertFalse(result['ok'])
        self.assertIn('public_identities', json.dumps(result))
        self.assertIn('browser_profile', json.dumps(result))
        data = json.loads(profile.read_text())
        data['accounts']['x-main']['public_identities'] = {'x': '@test_handle'}
        data['browser_profile'] = '../dedicated browser'
        self.write(profile, data)
        result = env.doctor(self.paths(profile=profile), environ={})
        self.assertTrue(result['ok'], result)
        self.assertEqual(env.load_profile(profile)['browser_profile'], str(self.home / 'dedicated browser'))
        self.assertFalse((self.home / 'dedicated browser').exists())

    def test_social_initialization_persists_binding_without_enabling_publication(self):
        profile = self.home / 'social.json'
        env.initialize(profile, platforms=['x'], browser_profile='browser directory', public_identities={'x': '@demo'})
        data = env.load_profile(profile)
        self.assertEqual(data['accounts']['x-main']['public_identities'], {'x': '@demo'})
        self.assertEqual(data['browser_profile'], str(self.home / 'browser directory'))
        self.assertEqual(data['adapters']['x']['enabled_capabilities'], [])
        self.assertEqual(data['acceptance_runs'], [])
        for identities in ({'x': ''}, {'weibo': '@other'}):
            with self.assertRaises(env.EnvironmentError):
                env.initialize(self.home / 'rejected.json', platforms=['x'], public_identities=identities)
            self.assertFalse((self.home / 'rejected.json').exists())

    def test_bundled_x_engine_initialization_is_explicit_and_inactive(self):
        path=self.home/'x-engine.json'
        env.initialize(path, platforms=['x'], browser_profile='dedicated browser',
            public_identities={'x':'@demo'},browser_engines=['x'],browser_cdp_url='http://127.0.0.1:9333/')
        data=env.load_profile(path)
        self.assertEqual(data['browser_runtime']['cdp_url'],'http://127.0.0.1:9333')
        self.assertTrue(data['adapters']['x']['execute'][1].endswith('/scripts/browser_engine.py'))
        self.assertEqual(data['adapters']['x']['enabled_capabilities'],[])
        self.assertEqual(data['acceptance_runs'],[])
        with patch('runtime.inspect',return_value={'ok':False}):
            self.assertFalse(env.doctor(self.paths(profile=path),environ={})['ok'])
        with patch('runtime.inspect',return_value={'ok':True}):
            self.assertTrue(env.doctor(self.paths(profile=path),environ={})['ok'])
        self.assertFalse((self.home/'dedicated browser').exists())

    def test_bundled_engine_invalid_config_leaves_no_file(self):
        for options in ({'browser_engines':['weibo']}, {'browser_engines':['x']},
                        {'browser_engines':['x'],'browser_profile':'dedicated','browser_cdp_url':'https://remote.test:9222'},
                        {'browser_engines':'x'}):
            path=self.home/'bad-engine.json'
            with self.assertRaises(env.EnvironmentError):env.initialize(path,platforms=['x'],**options)
            self.assertFalse(path.exists())

    def test_setup_can_select_bundled_engine_without_an_extra_skill(self):
        path=self.home/'cli-x.json'
        result=subprocess.run([sys.executable,str(ROOT/'setup.py'),'--init','--profile',str(path),
            '--platform','x','--browser-engine','x','--browser-cdp-url','http://127.0.0.1:9333',
            '--browser-profile',str(self.home/'dedicated'),'--runtime-dir',str(self.home/'absent-runtime'),
            '--public-identity','x=@demo'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)  # Configuration is saved; optional runtime is not installed.
        value=json.loads(result.stdout)
        self.assertTrue(value['created']);self.assertFalse(value['configuration_ready'])
        self.assertEqual(env.load_profile(path)['adapters']['x']['enabled_capabilities'],[])

    def test_write_discovery_uses_installed_entry(self):
        scripts = self.home / '.agents/skills/sansheng-write/scripts'
        scripts.mkdir(parents=True); (scripts / 'wechat_api.py').write_text('')
        with patch.object(env, 'SKILL_ROOT', self.home / 'elsewhere/distribute'):
            self.assertEqual(env.discover_write_scripts(home=self.home), scripts)
        with self.assertRaisesRegex(env.EnvironmentError, '缺少 wechat_api.py'):
            with patch.object(env, 'discover_write_scripts', return_value=None):
                env.initialize(self.home / 'bad.json', platforms=['wechat'], write_scripts=self.home / 'missing')

    def test_setup_noninteractive_show_and_repeat_do_not_leak_or_replace(self):
        command = [sys.executable, str(ROOT / 'setup.py')]
        process_env = {k: v for k, v in os.environ.items() if not k.startswith(('SANSHENG_DISTRIBUTE_', 'SANDY_PUBLISH_'))}
        process_env['HOME'] = str(self.home)
        result = subprocess.run(command, input='', capture_output=True, text=True, env=process_env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('needs_input', result.stdout)
        result = subprocess.run(command + ['--init'], capture_output=True, text=True, env=process_env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.home / '.config/sansheng-distribute/profile.json'
        before = path.read_bytes()
        result = subprocess.run(command, capture_output=True, text=True, env=process_env)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)['existing'])
        result = subprocess.run(command + ['--init'], capture_output=True, text=True, env=process_env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(path.read_bytes(), before)
        data = json.loads(path.read_text()); data['secret'] = 'NEVER_OUTPUT'
        self.write(path, data)
        result = subprocess.run(command + ['--show-profile'], capture_output=True, text=True, env=process_env)
        self.assertNotIn('NEVER_OUTPUT', result.stdout)
        self.assertNotIn('secret', result.stdout)

    def test_setup_incomplete_social_config_is_created_but_not_claimed_ready(self):
        profile = self.home / 'pending-social.json'
        result = subprocess.run([sys.executable, str(ROOT / 'setup.py'), '--init', '--profile', str(profile),
                                 '--state-dir', str(self.home / 'state'), '--platform', 'x'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout)
        output = json.loads(result.stdout)
        self.assertTrue(output['created'])
        self.assertFalse(output['configuration_ready'])
        self.assertIn('next_action', output)


if __name__ == '__main__':
    unittest.main()
