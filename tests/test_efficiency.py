import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from browser_prefill import assert_page, bind, browser_arguments, make_steps, perform
from biliup_bridge import identity, plan, probe, VERSION, URL, SHA256
from publish_core import PublishError
import test_campaign
import test_social_adapter


class ParallelTests(unittest.TestCase):
    setUp = test_campaign.CampaignTests.setUp
    authorize = test_campaign.CampaignTests.authorize
    calls = test_campaign.CampaignTests.calls
    def test_cross_platform_overlap_uses_separate_connections(self):
        from campaign import run_campaign
        from publish_core import run as real_run
        barrier = threading.Barrier(2)
        seen = []
        def work(path, ident, profile, store, **kwargs):
            seen.append((threading.get_ident(), id(store.db)))
            barrier.wait(timeout=5)
            return real_run(path, ident, profile, store, **kwargs)
        self.authorize()
        with patch('campaign.run', side_effect=work):
            report = run_campaign(self.cid, self.profile, self.store, max_parallel=2)
        self.assertTrue(report['all_endpoints_reached'])
        self.assertEqual(len({x[0] for x in seen}), 2)
        self.assertEqual(len({x[1] for x in seen}), 2)

    def test_parallel_unknown_still_only_queries_on_repeat(self):
        from campaign import run_campaign
        from publish_core import write_json
        self.authorize()
        write_json(self.root / 'modes.json', {'x': 'unknown'})
        run_campaign(self.cid, self.profile, self.store, max_parallel=2)
        run_campaign(self.cid, self.profile, self.store, max_parallel=2)
        self.assertEqual(self.calls().count(['x', 'execute']), 1)

    def test_same_platform_stays_serial(self):
        from campaign import run_campaign, prepare_campaign
        from publish_core import run as real_run, write_json
        manifest = copy.deepcopy(self.manifest)
        manifest['targets'][1]['platform'] = 'x'
        write_json(self.path, manifest)
        self.cid = prepare_campaign(self.path, self.store)['campaign_id']
        self.authorize()
        active = 0
        def work(*args, **kwargs):
            nonlocal active
            active += 1
            self.assertEqual(active, 1)
            try: return real_run(*args, **kwargs)
            finally: active -= 1
        with patch('campaign.run', side_effect=work):
            report = run_campaign(self.cid, self.profile, self.store, max_parallel=2)
        self.assertTrue(report['all_endpoints_reached'])

    def test_parallel_limits_and_readonly(self):
        from campaign import run_campaign
        for value in (0, 3, True, '2'):
            with self.assertRaises(PublishError):
                run_campaign(self.cid, self.profile, self.store, max_parallel=value)
        self.authorize()
        report = run_campaign(self.cid, self.profile, self.store, resume=True, max_parallel=2)
        self.assertEqual(set(report['outcomes'].values()), {'not_started'})
        self.assertEqual(self.calls(), [])


class BrowserStepsTests(unittest.TestCase):
    def setUp(self):
        self.pack = {'platform': 'xiaohongshu', 'fields': {'title': '不执行代码 $(touch nope)'},
                     'assets': [{'id': 'v', 'kind': 'video', 'absolute_path': '/private/video.mp4'}]}
        self.recipe = {'enabled': True, 'page_path': '/publish/publish',
                       'identity_selector': '[data-account]',
                       'steps': [{'action': 'fill', 'selector': 'input', 'field': 'title'}]}

    def test_empty_disabled_or_submit_recipe_rejected(self):
        for recipe in ({}, {**self.recipe, 'steps': []},
                       {**self.recipe, 'steps': [{'action': 'click', 'selector': 'button.publish'}]},
                       {**self.recipe, 'steps': [{'action': 'fill', 'selector': 'input', 'field': 'javascript'}]}):
            with self.assertRaises(PublishError): make_steps(self.pack, recipe)

    def test_exact_values_and_sealed_assets_only(self):
        steps = make_steps(self.pack, self.recipe)
        self.assertEqual(steps[0]['value'], self.pack['fields']['title'])
        recipe = {**self.recipe, 'steps': [{'action': 'upload', 'selector': 'input', 'asset_id': 'unknown'}]}
        with self.assertRaises(PublishError): make_steps(self.pack, recipe)

    def test_wrong_origin_and_path_rejected(self):
        class Page: url = 'https://creator.xiaohongshu.com/publish/publish'
        assert_page(Page(), self.pack, self.recipe)
        for url in ('https://creator.xiaohongshu.com.evil.test/publish/publish',
                    'https://creator.xiaohongshu.com/wrong', 'https://x.com/publish/publish'):
            p = Page(); p.url = url
            with self.assertRaises(PublishError): assert_page(p, self.pack, self.recipe)

    def test_lease_checked_before_every_mutation_and_no_text_truncation(self):
        class Node:
            value = ''
            def count(self): return 1
            def fill(self, value, **kwargs): self.value = value
            def evaluate(self, code): return 'INPUT'
            def input_value(self): return self.value
        class Page:
            node = Node()
            def locator(self, selector): return self.node
        page = Page(); checks = []
        def guard():
            checks.append(1)
            if len(checks) == 2: raise PublishError('lease closed')
        steps = make_steps(self.pack, self.recipe)
        with self.assertRaisesRegex(PublishError, 'lease closed'):
            perform(page, steps + [{**steps[0], 'value': 'wrong'}], guard)
        self.assertEqual(page.node.value, self.pack['fields']['title'])
        page.node.count = lambda: 2
        with self.assertRaisesRegex(PublishError, '不唯一'): perform(page, steps, lambda: None)

    def test_browser_pid_fallback_does_not_restart_or_choose_other_chrome(self):
        from unittest.mock import Mock
        session = Mock()
        session.send.side_effect = [RuntimeError('no automation flag'),
                                   {'processInfo': [{'type': 'browser', 'id': 123}]}]
        command = '/Applications/Chrome --user-data-dir=/private/Dedicated Browser --remote-debugging-port=9333'
        with patch('browser_prefill.subprocess.run', return_value=subprocess.CompletedProcess([], 0, command, '')) as ps:
            self.assertEqual(browser_arguments(session), ['--user-data-dir=/private/Dedicated Browser'])
        self.assertEqual(ps.call_args.args[0], ['ps', '-ww', '-p', '123', '-o', 'command='])


class BrowserBindingTests(unittest.TestCase):
    def test_binding_rejects_stale_work_account_profile_and_readonly_stage(self):
        from publish_core import read_json, write_json, digest
        from social_adapter import work_path
        fixture = test_social_adapter.SocialAdapterTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        task = {'action': 'execute', 'request': read_json(fixture.request), 'nonce': 'test'}
        work = {'pack': fixture.pack, 'pack_digest': digest(fixture.pack),
                'expected_account_identity': '@owner', 'browser_profile': fixture.profile['browser_profile']}
        write_json(work_path(fixture.request), work)
        with patch('browser_prefill.agent_adapter.inspect', return_value=task):
            self.assertEqual(bind(fixture.request, fixture.profile)[1], fixture.pack)
            for changes in ({'pack_digest': 'wrong'}, {'expected_account_identity': '@wrong'},
                            {'browser_profile': '/another/profile'}):
                write_json(work_path(fixture.request), {**work, **changes})
                with self.assertRaises(PublishError): bind(fixture.request, fixture.profile)
            write_json(work_path(fixture.request), work)
            task['action'] = 'verify'
            with self.assertRaises(PublishError): bind(fixture.request, fixture.profile)


class BiliBridgeTests(unittest.TestCase):
    def setUp(self):
        self.pack = {'platform': 'bilibili', 'content_type': 'video', 'endpoint': 'publish',
                     'platform_metadata': {'copyright': 1},
                     'fields': {'title': '示例', 'description': '正文', 'tags': ['英语']},
                     'assets': [{'id': 'v', 'kind': 'video', 'absolute_path': '/private/video.mp4'}]}

    def test_plan_never_uploads_and_preserves_argv_values(self):
        self.pack['fields']['title'] = '$(touch nope)'
        with patch('biliup_bridge.probe', return_value={'binary': '/private/biliup'}), patch('biliup_bridge.subprocess.run') as remote:
            result = plan(self.pack, '/private/tool', '/private/cookies.json', 171)
        remote.assert_not_called()
        self.assertIn('$(touch nope)', result['argv'])
        self.assertFalse(result['upload_enabled'])

    def test_unsupported_fields_never_silently_dropped(self):
        for change in ({'platform_metadata': {'ai_generated': True}},
                       {'subtitle_asset_ids': ['s']}, {'endpoint': 'prefill'}, {'platform': 'x'}):
            with patch('biliup_bridge.probe') as runtime, self.assertRaises(PublishError):
                plan({**self.pack, **change}, '/tool', '/cookies', 171)
            runtime.assert_not_called()
        for field, value in [('links', [{'url': 'https://example.org'}]), ('text', 'extra'), ('tags', ['a,b'])]:
            pack = copy.deepcopy(self.pack); pack['fields'][field] = value
            with self.assertRaises(PublishError): plan(pack, '/tool', '/cookies', 171)

    def test_ai_declaration_is_explicit_and_preserved(self):
        pack = copy.deepcopy(self.pack)
        pack['platform_metadata']['ai_generated'] = True
        with patch('biliup_bridge.probe', return_value={'binary': '/private/biliup'}):
            argv = plan(pack, '/tool', '/cookies', 229)['argv']
        self.assertEqual(json.loads(argv[argv.index('--extra-fields') + 1]),
                         {'creation_statement': {'id': 1}})
        for invalid in ('true', 1, None):
            pack['platform_metadata']['ai_generated'] = invalid
            with patch('biliup_bridge.probe') as runtime, self.assertRaises(PublishError):
                plan(pack, '/tool', '/cookies', 229)
            runtime.assert_not_called()

    def test_tampered_binary_not_executed(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'biliup').write_bytes(b'changed')
            (root / 'install.json').write_text(json.dumps({'version': VERSION, 'source': URL,
                'archive_sha256': SHA256, 'binary_sha256': hashlib.sha256(b'original').hexdigest()}))
            with patch('biliup_bridge.subprocess.run') as process, self.assertRaises(PublishError): probe(root)
            process.assert_not_called()

    def test_identity_rejects_wrong_account_and_public_credentials(self):
        import io
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'auth.json'
            path.write_text(json.dumps({'cookie_info': {'cookies': [{'name': 'SESSDATA', 'value': 'fake'}]}}))
            path.chmod(0o600)
            with patch('biliup_bridge.urllib.request.urlopen') as http:
                http.return_value.__enter__.return_value = io.StringIO(json.dumps({'code': 0, 'data': {'isLogin': True, 'mid': 7}}))
                self.assertTrue(identity(path, '7')['expected_uid_matches'])
            with patch('biliup_bridge.urllib.request.urlopen') as http:
                http.return_value.__enter__.return_value = io.StringIO(json.dumps({'code': 0, 'data': {'isLogin': True, 'mid': 8}}))
                with self.assertRaisesRegex(PublishError, '不符'): identity(path, '7')
            path.chmod(0o644)
            with patch('biliup_bridge.urllib.request.urlopen') as http, self.assertRaises(PublishError): identity(path, '7')
            http.assert_not_called()


if __name__ == '__main__': unittest.main()
