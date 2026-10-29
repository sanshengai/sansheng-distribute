import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError
from weibo_official_probe import Client, _NoRedirect, run, validate_config


class OfficialProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.creds = self.folder / 'auth.json'
        self.creds.write_text(json.dumps({'app_id': 'app-id', 'app_secret': 'secret-value'}))
        self.creds.chmod(0o600)
        self.config = {'schema': 'weibo-official-probe.v1', 'enabled': True, 'account': 'example',
                       'uid': '101', 'credentials': str(self.creds), 'work_ids': ['201']}
        self.calls = []

    def request(self, method, path, params):
        self.calls.append((method, path, dict(params)))
        if method == 'POST':
            return {'code': 0, 'data': {'uid': 101, 'token': 'token-value', 'expire_in': 7200}}
        return {'code': 0, 'data': {'comments': [{'id': '301', 'text': 'hello'}], 'count': 1}}

    def test_only_auth_and_explicit_read_paths_no_install_token_cache_or_inbox(self):
        root = self.folder / 'state'
        stdout = io.StringIO()
        client = Client(self.request)
        with contextlib.redirect_stdout(stdout): result = run(root, self.config, client)
        self.assertEqual(result['state'], 'read_probe_complete')
        self.assertEqual((result['read_requests'], result['returned_rows'], result['social_writes']), (2, 2, 0))
        self.assertFalse(result['automatic_sync'])
        self.assertEqual([(x[0], x[1]) for x in self.calls], [
            ('POST', '/open/auth/ws_token'), ('GET', '/open/interactive/comments/to_me'),
            ('GET', '/open/interactive/comments/show')])
        self.assertEqual(self.calls[2][2]['id'], '201')
        self.assertIsNone(client.token)
        evidence = Path(result['evidence'])
        saved = evidence.read_text()
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(root.iterdir())), 1)
        for secret in ('secret-value', 'token-value', 'app-id'):
            self.assertNotIn(secret, saved + stdout.getvalue())

    def test_disabled_invalid_and_empty_config_rejected_before_auth(self):
        for key, value in [('enabled', False), ('enabled', 1), ('work_ids', []),
                           ('work_ids', ['201'] * 6), ('work_ids', ['201', '201']),
                           ('work_ids', [{}]), ('uid', 101), ('credentials', 'relative.json')]:
            cfg = dict(self.config); cfg[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(PublishError):
                run(self.folder / 'state', cfg, Client(self.request))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.folder / 'state').exists())

    def test_wrong_uid_stops_before_comment_query_and_saves_failure(self):
        def request(method, path, params):
            response = self.request(method, path, params)
            response['data']['uid'] = 999
            return response
        root = self.folder / 'state'
        with self.assertRaises(PublishError): run(root, self.config, Client(request))
        self.assertEqual(len(self.calls), 1)
        saved = json.loads(next(root.glob('*.json')).read_text())
        self.assertEqual((saved['state'], saved['requests']), ('failed', []))

    def test_credentials_are_private_and_never_command_arguments(self):
        self.creds.chmod(0o644)
        with self.assertRaises(PublishError): run(self.folder / 'state', self.config, Client(self.request))
        self.assertEqual(self.calls, [])
        self.creds.chmod(0o600)
        self.creds.write_text('{"app_id":"x","app_secret":"y","extra":"z"}')
        with self.assertRaises(PublishError): run(self.folder / 'state', self.config, Client(self.request))
        self.assertEqual(self.calls, [])

    def test_query_error_is_not_zero_comments_and_stops_remaining_work(self):
        cfg = dict(self.config, work_ids=['201', '202'])
        def request(method, path, params):
            response = self.request(method, path, params)
            return response if method == 'POST' else {'code': 403, 'message': 'secret-value'}
        root = self.folder / 'state'
        with self.assertRaises(PublishError) as error: run(root, cfg, Client(request))
        self.assertNotIn('secret-value', str(error.exception))
        self.assertEqual(len(self.calls), 2)
        saved = json.loads(next(root.glob('*.json')).read_text())
        self.assertEqual(saved['state'], 'failed')

    def test_unrecognized_payloads_rejected(self):
        invalid = [{}, {'code': True, 'data': {}}, {'code': 0, 'data': {}},
                   {'code': 0, 'data': {'comments': [], 'count': False}},
                   {'code': 0, 'data': {'comments': [], 'count': -1}},
                   {'code': 0, 'data': {'comments': 'bad', 'count': 1}}]
        for payload in invalid:
            client = Client(lambda *args: payload); client.token = 'token-value'
            with self.subTest(payload=payload), self.assertRaises(PublishError): client.read('received')

    def test_empty_real_list_is_a_probe_not_complete_sync(self):
        def request(method, path, params):
            return self.request(method, path, params) if method == 'POST' else {
                'code': 0, 'data': {'comments': [], 'count': 0}}
        result = run(self.folder / 'state', self.config, Client(request))
        self.assertEqual(result['returned_rows'], 0)
        self.assertFalse(result['automatic_sync'])
        self.assertIn('不证明全量覆盖', Path(result['evidence']).read_text())

    def test_transport_whitelist_and_no_redirect(self):
        with patch('urllib.request.build_opener') as opener:
            for method, path in [('POST', '/open/crowd/comment'), ('GET', '/open/auth/ws_token'),
                                 ('GET', 'https://other.example/'), ('DELETE', '/open/interactive/comments/to_me')]:
                with self.assertRaises(PublishError): Client._request(method, path, {})
            opener.assert_not_called()
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example/'))

    def test_transport_does_not_expose_token_in_http_or_network_errors(self):
        errors = [urllib.error.HTTPError('https://bad?token=secret-value', 403, 'bad', {}, None),
                  urllib.error.URLError('bad?token=secret-value')]
        for failure in errors:
            with patch('urllib.request.build_opener') as build:
                build.return_value.open.side_effect = failure
                with self.assertRaises(PublishError) as error:
                    Client._request('GET', '/open/interactive/comments/to_me', {'token': 'secret-value'})
                self.assertNotIn('secret-value', str(error.exception))
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_interrupted_request_leaves_reserved_started_record(self):
        def request(*args): raise KeyboardInterrupt()
        root = self.folder / 'state'
        with self.assertRaises(KeyboardInterrupt): run(root, self.config, Client(request))
        document = json.loads(next(root.glob('*.json')).read_text())
        self.assertEqual(document['state'], 'started')
        self.assertEqual(document['social_writes'], 0)


if __name__ == '__main__': unittest.main()
