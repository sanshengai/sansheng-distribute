"""Single-package execution against a local HTTP API, plus credential/error boundaries."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import environment
import wechat_api as api
import wechat_tunnel as tunnel
from wechat_adapter import load_api, validate_images
from publish_core import Store, run, validate, write_json, PublishError


class InternalWechatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_core_executes_bundled_api_with_no_producer_skill(self):
        self.exercise_core()

    def test_lost_create_response_recovers_same_draft_without_reposting(self):
        self.exercise_core(lose_response=True)

    def exercise_core(self, lose_response=False):
        package = self.root / 'single-package'
        shutil.copytree(ROOT / 'scripts', package / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(ROOT / 'setup.py', package / 'setup.py')
        source = self.root / 'content'; source.mkdir()
        (source / 'article.html').write_text('<h1>本地联调</h1><p>保留正文位置</p>')
        (source / 'cover.png').write_bytes(b'fixture-cover')
        secret = self.root / 'private.env'
        secret.write_text('WECHAT_APP_ID=local-app\nWECHAT_APP_SECRET=LOCAL-SECRET\n')
        profile_path = self.root / 'profile.json'
        result = subprocess.run([sys.executable, '-I', str(package / 'setup.py'), '--init', '--profile', str(profile_path),
            '--platform', 'wechat', '--wechat-env-file', str(secret), '--wechat-appid-sha256',
            hashlib.sha256(b'local-app').hexdigest()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['configuration_ready'])
        self.assertNotIn('LOCAL-SECRET', result.stdout + result.stderr)
        profile = environment.load_profile(profile_path)
        self.assertNotIn('--write-scripts', profile['adapters']['wechat']['execute'])
        profile['adapters']['wechat']['enabled_capabilities'] = ['article:draft']
        write_json(profile_path, profile)
        manifest = dict(schema_version=1, job_id='internal-api', producer='ordinary-files', content_status='final',
            source_root=str(source), documents=[dict(id='doc', path='article.html', format='html')],
            assets=[dict(id='cover', path='cover.png', kind='image')], media_slots=[],
            targets=[dict(id='wx', platform='wechat', account='wechat-main', content_type='article', endpoint='draft',
                title='单包验证', document_id='doc', asset_ids=['cover'], cover_asset_id='cover')])
        validate(manifest, sealing=True)
        manifest_path = source / 'manifest.json'; write_json(manifest_path, manifest)
        received = []; articles = {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                received.append(('token', None)); self.reply(dict(access_token='LOCAL-TOKEN', expires_in=7200))
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                endpoint = self.path.split('?')[0].rsplit('/', 2)[-2:]
                route = '/'.join(endpoint); received.append((route, body))
                if route == 'material/add_material': self.reply(dict(media_id='local-cover'))
                elif route == 'draft/add':
                    articles['local-draft'] = json.loads(body)['articles'][0]
                    if lose_response:
                        self.connection.shutdown(2); self.connection.close(); return
                    self.reply(dict(media_id='local-draft'))
                elif route == 'draft/get':
                    self.reply(dict(news_item=[articles[json.loads(body)['media_id']]]))
                else: self.reply(dict(errcode=40005))
            def reply(self, value):
                data = json.dumps(value).encode(); self.send_response(200)
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        store = Store(self.root / 'state')
        try:
            key = store.prepare(manifest, 'wx')['key']; store.authorize(manifest, 'wx', 'local fixture draft only')
            with patch.dict(os.environ, {'SANSHENG_DISTRIBUTE_WECHAT_API_BASE':f'http://127.0.0.1:{server.server_port}',
                    'SANSHENG_DISTRIBUTE_CACHE_DIR':str(self.root/'cache'), 'SANSHENG_DISTRIBUTE_WECHAT_TUNNEL':'off'}):
                if lose_response:
                    with self.assertRaises(PublishError): run(manifest_path, 'wx', profile, store)
                    self.assertEqual(store.status(key)['state'], 'submission_unknown')
                    with self.assertRaises(PublishError): run(manifest_path, 'wx', profile, store, resume=True)
                    value = run(manifest_path, 'wx', profile, store, resume=True, recover_remote_id='local-draft')
                else:
                    value = run(manifest_path, 'wx', profile, store)
                run(manifest_path, 'wx', profile, store, resume=True)
            self.assertEqual(value['remote_id'], 'local-draft')
            self.assertEqual(articles['local-draft']['content'], (source/'article.html').read_text())
            self.assertEqual([name for name, _ in received].count('draft/add'), 1)
            self.assertGreaterEqual([name for name, _ in received].count('draft/get'), 2)
            cache = next((self.root/'cache').glob('wechat-token-*.json'))
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        finally:
            store.close(); server.shutdown(); server.server_close(); thread.join()

    def test_unsealed_html_file_references_rejected_before_any_network(self):
        (self.root/'article.html').write_text('<img src="cover.png">')
        (self.root/'cover.png').write_bytes(b'cover')
        manifest = dict(source_root=str(self.root), documents=[dict(id='doc',path='article.html')],
            assets=[dict(id='cover',kind='image',path='cover.png',sha256=hashlib.sha256(b'cover').hexdigest())])
        target = dict(document_id='doc',asset_ids=['cover'])
        validate_images(manifest, target)
        for src in ('missing.png','https://example.org/image.png','../secret.env','file:///private/secret.env'):
            (self.root/'article.html').write_text('<img src="'+src+'">')
            with self.subTest(src=src), self.assertRaises(PublishError): validate_images(manifest,target)
        (self.root/'article.html').write_text('<img src="cover.png">')
        with self.assertRaises(PublishError): validate_images(manifest,dict(target,asset_ids=[]))
        (self.root/'cover.png').write_bytes(b'changed')
        with self.assertRaises(PublishError): validate_images(manifest,target)

    def test_explicit_legacy_module_is_selected_without_poisoning_default(self):
        legacy = self.root/'legacy'; legacy.mkdir()
        (legacy/'wechat_api.py').write_text('from . import wechat_tunnel\nORIGIN=wechat_tunnel.ORIGIN\n')
        (legacy/'wechat_tunnel.py').write_text('ORIGIN="legacy"\n')
        self.assertEqual(load_api(legacy).ORIGIN, 'legacy')
        self.assertEqual(Path(load_api().__file__).resolve(), ROOT/'scripts/wechat_api.py')
        with self.assertRaisesRegex(Exception, '模块缺失'): load_api(self.root/'missing')

    def test_cache_and_network_errors_do_not_disclose_urls_or_secrets(self):
        with patch.dict(os.environ, {'SANSHENG_DISTRIBUTE_CACHE_DIR':str(self.root/'cache')}):
            with patch.object(api, '_request', return_value=dict(access_token='secret-token',expires_in=7200)) as request:
                self.assertEqual(api.access_token('app', 'secret'), 'secret-token')
                self.assertEqual(api.access_token('app', 'secret'), 'secret-token')
                self.assertEqual(request.call_count, 1)
            cache = api._token_cache_path('app')
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cache.parent.stat().st_mode & 0o777, 0o700)
        failure = urllib.error.URLError('https://example.org/?secret=DO_NOT_PRINT')
        with patch.object(api.urllib.request, 'urlopen', side_effect=failure):
            with self.assertRaises(api.WechatError) as error:
                api._request(api.urllib.request.Request('https://example.org'), 'fixture')
        self.assertNotIn('DO_NOT_PRINT', str(error.exception))
        with self.assertRaises(api.WechatError) as error:
            api._check(dict(errcode=40001, errmsg='access_token=DO_NOT_PRINT'), 'fixture')
        self.assertNotIn('DO_NOT_PRINT', str(error.exception))

    def test_forced_tunnel_uses_configuration_not_a_host_named_always(self):
        config = self.root/'tunnel.json'
        config.write_text(json.dumps(dict(host='example.org',user='publisher',port=22)))
        with patch.dict(os.environ, {'SANSHENG_DISTRIBUTE_WECHAT_TUNNEL':'always'}), patch.object(tunnel, 'CONFIG_FILE', config):
            self.assertEqual(tunnel.tunnel_config()['host'], 'example.org')
        with patch.object(tunnel, 'CONFIG_FILE', self.root/'absent'), patch.object(tunnel, 'LEGACY_CONFIG_FILE', config), patch.dict(os.environ, {'SANSHENG_DISTRIBUTE_WECHAT_TUNNEL':'always'}):
            self.assertEqual(tunnel.tunnel_config()['user'], 'publisher')
        class Fragmented:
            def __init__(self): self.fragments = [b'a',b'b',b'c',b'']
            def recv(self, count): return self.fragments.pop(0)
        self.assertEqual(tunnel._read_exact(Fragmented(), 3), b'abc')
        with self.assertRaises(OSError): tunnel._read_exact(Fragmented(), 4)


if __name__ == '__main__': unittest.main()
