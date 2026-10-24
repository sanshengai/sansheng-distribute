"""Actual producer → ledger → API readback contract, with a read-only fake API."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError, Store, digest, file_hash, run, validate, write_json
from wechat_adapter import dispatch


@unittest.skipUnless((ROOT/'sansheng-write/scripts/legacy_draft_handoff.py').is_file(), 'optional Write checkout absent')
class LegacyAdoptionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT/'sansheng-write/scripts'))
        from evidence import stable_digest
        from release_to_draft import build_expected_draft
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.anchor = '请在此插入这篇文章的视频'
        (self.root/'素材').mkdir()
        (self.root/'素材/cover.png').write_bytes(b'cover-fixture')
        (self.root/'素材/photo.png').write_bytes(b'image-fixture')
        (self.root/'video.mp4').write_bytes(b'video-fixture')
        (self.root/'定稿.html').write_text('<html><head><title>窗口标题</title></head><body><div id="output"><p>前'+self.anchor+'后</p><img src="素材/photo.png"><a href="https://example.org/">链接</a></div></body></html>')
        (self.root/'article-meta.yaml').write_text('title: 测试\ndigest: 测试摘要\nauthor: fixture\n')
        with patch('release_to_draft.brand', return_value={}):
            expected, errors = build_expected_draft(self.root)
        self.assertEqual(errors, [])
        manifest = dict(files=[dict(path='定稿.html', sha256=file_hash(self.root/'定稿.html'))])
        self.receipt = dict(schema_version=2, scope='wechat-draft', formal_publish=False,
                            remote_verified=True, draft_media_id='old-draft', cover_media_id='old-cover',
                            manifest=manifest, manifest_digest=stable_digest(manifest), publish_ready_digest=stable_digest(manifest),
                            expected_draft_digest=stable_digest(expected), remote_readback=dict(image_sources=['https://example.org/photo.png']))
        write_json(self.root/'_publish-receipt.json', self.receipt)
        self.targets = [dict(id='wx', platform='wechat', account='fixture', content_type='article', endpoint='draft', title='测试')]
        self.media = [dict(id='video', kind='video', path='video.mp4', anchor=self.anchor)]
        self.store = Store(self.root/'state')
        env = self.root/'credentials.env'; env.write_text('WECHAT_APP_ID=fixture\nWECHAT_APP_SECRET=fixture\n')
        self.profile = dict(accounts={'fixture': dict(platforms=['wechat'], wechat_env_file=str(env), wechat_appid_sha256=hashlib.sha256(b'fixture').hexdigest())},
                            adapters={'wechat': {'verify': ['not-executed']}})

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def export(self):
        from publish_handoff import export_handoff
        with patch('release_to_draft.brand', return_value={}):
            path = export_handoff(self.root, self.targets, media=self.media, existing_draft='old-draft')
        self.manifest = json.loads(path.read_text()); validate(self.manifest)
        self.job = self.store.prepare(self.manifest, 'wx')
        expected_path = next(d['path'] for d in self.manifest['documents'] if d['id'] == 'legacy-expected')
        self.actual = json.loads((self.root/expected_path).read_text())
        self.actual['content'] = self.actual['content'].replace('【发布媒体:video】', self.anchor)
        self.path = path
        return path

    def invoke(self, config, action, request, **kwargs):
        self.assertEqual(action, 'verify', 'adoption must never execute or upload')
        outer = self
        class ReadOnlyApi:
            def get_draft(self, **args):
                outer.assertEqual(args['media_id'], 'old-draft')
                return {'news_item': [copy.deepcopy(outer.actual)]}
        return dispatch(request, self.profile, ReadOnlyApi(), action=action)

    def adopt(self):
        with patch('publish_core.invoke', side_effect=self.invoke):
            return run(self.path, 'wx', self.profile, self.store, adopt=True)

    def test_real_export_adopts_without_writes_or_new_grant_and_can_resume(self):
        self.export()
        before = (self.root/'定稿.html').read_bytes()
        result = self.adopt()
        self.assertEqual(self.manifest['media_slots'][0]['context']['before'], '前')
        self.assertEqual(result['state'], 'pending'); self.assertEqual(result['remote_id'], 'old-draft')
        self.assertEqual(self.adopt()['key'], result['key'])
        with self.assertRaisesRegex(PublishError, '缺少.*授权'):
            self.store.begin(self.manifest, 'wx', continuing=True)
        self.assertEqual(before, (self.root/'定稿.html').read_bytes())
        with self.assertRaisesRegex(PublishError, '不能新建'):
            self.store.begin(self.manifest, 'wx')

    def test_reordered_image_changed_link_and_wrong_position_rejected(self):
        self.export(); original = self.actual['content']
        variants = [original.replace(self.anchor, '').replace('前', self.anchor+'前'),
                    original.replace('https://example.org/', 'https://wrong.example/'),
                    original.replace('<img src="https://example.org/photo.png">', '') + '<img src="https://example.org/photo.png">']
        for text in variants:
            self.actual['content'] = text
            with self.assertRaisesRegex(PublishError, '差异'):
                self.adopt()
            self.assertEqual(self.store.status(self.job['key'])['remote_id'], 'old-draft')
        self.actual['content'] = original
        self.assertEqual(self.adopt()['state'], 'pending')

    def test_same_remote_cannot_be_owned_by_second_manifest(self):
        self.export(); self.adopt()
        other = copy.deepcopy(self.manifest); other['targets'][0]['text'] = 'another local task'
        self.store.prepare(other, 'wx')
        with self.assertRaisesRegex(PublishError, '另一任务'):
            self.store.bind_existing(other, 'wx')

    def test_even_authorized_adoption_package_cannot_start_a_new_draft(self):
        self.export()
        self.store.authorize(self.manifest, 'wx', 'offline fixture authorization')
        with self.assertRaises(PublishError):
            self.store.begin(self.manifest, 'wx')
        self.assertEqual(self.store.status(self.job['key'])['state'], 'prepared')
        for bad in ('', None):
            changed = copy.deepcopy(self.manifest)
            changed['targets'][0]['existing_draft']['remote_id'] = bad
            with self.assertRaises(PublishError): validate(changed)

    def test_no_receipt_stale_source_empty_or_conflicting_receipt_rejected(self):
        from publish_handoff import HandoffError
        path = self.root/'_publish-receipt.json'
        for value in ({}, dict(self.receipt, remote_verified=False), dict(self.receipt, draft_media_id='wrong')):
            write_json(path, value)
            with self.assertRaises(HandoffError): self.export()
        write_json(path, self.receipt)
        (self.root/'过程记录').mkdir(exist_ok=True)
        write_json(self.root/'过程记录/_publish-receipt.json', dict(self.receipt, sealed_at='conflict'))
        with self.assertRaisesRegex(HandoffError, '冲突'): self.export()
        (self.root/'过程记录/_publish-receipt.json').unlink()
        (self.root/'定稿.html').write_text('changed')
        with self.assertRaisesRegex(HandoffError, '源文件已变化'): self.export()

    def test_comment_anchor_is_not_a_safe_legacy_editor_location(self):
        from publish_handoff import HandoffError
        from evidence import stable_digest
        from release_to_draft import build_expected_draft
        path = self.root/'定稿.html'
        path.write_text(path.read_text().replace(self.anchor, '<!-- video -->'))
        self.media[0]['anchor'] = '<!-- video -->'
        self.receipt['manifest']['files'][0]['sha256'] = file_hash(path)
        self.receipt['manifest_digest'] = self.receipt['publish_ready_digest'] = stable_digest(self.receipt['manifest'])
        with patch('release_to_draft.brand', return_value={}):
            expected, errors = build_expected_draft(self.root)
        self.assertFalse(errors)
        self.receipt['expected_draft_digest'] = stable_digest(expected)
        write_json(self.root/'_publish-receipt.json', self.receipt)
        with self.assertRaisesRegex(HandoffError, '可见纯文字'): self.export()

    def test_inserted_native_video_resumes_in_same_slot_and_needs_visual_check(self):
        self.export(); self.adopt()
        component = '<mpvideo vid="fixture-video"></mpvideo>'
        self.actual['content'] = self.actual['content'].replace(self.anchor, component)
        request = self.store.directory/'requests'/f"{self.job['key']}.json"
        video = next(a for a in self.manifest['assets'] if a['id'] == 'video')
        receipt = dict(manifest_digest=digest(self.manifest), remote_id='old-draft', components=[dict(
            slot_id='video', kind='video', asset_sha256=video['sha256'], component_sha256=hashlib.sha256(component.encode()).hexdigest(),
            remote_media_id='fixture-video', remote_id_attribute='vid', upload_evidence='offline fixture only',
            saved=True, reopened=True, loaded=True, played=True)])
        write_json(request.with_suffix('.native-media.json'), receipt)
        self.assertEqual(self.adopt()['state'], 'pending')
        evidence = self.root/'visual.txt'; evidence.write_text('Offline test fixture, no actual platform observation')
        receipt['layout_review'] = dict(reviewed=True, content_sha256=hashlib.sha256(self.actual['content'].encode()).hexdigest(),
                                        evidence=[dict(path=str(evidence), sha256=file_hash(evidence))])
        write_json(request.with_suffix('.native-media.json'), receipt)
        self.assertEqual(self.adopt()['state'], 'draft_ready')

    def test_markdown_comment_maps_explicitly_to_old_visible_html_location(self):
        from publish_handoff import HandoffError
        original = '# 测试\n<!-- VIDEO: demo -->'
        (self.root/'定稿.md').write_text(original)
        with self.assertRaisesRegex(HandoffError, '未交接'): self.export()
        self.media[0]['markdown_anchor'] = '<!-- VIDEO: demo -->'
        self.export()
        self.assertEqual(self.adopt()['state'], 'pending')
        self.assertEqual((self.root/'定稿.md').read_text(), original)
        self.media[0]['markdown_anchor'] = '<!-- VIDEO: missing -->'
        with self.assertRaisesRegex(HandoffError, '唯一匹配'): self.export()


if __name__ == '__main__': unittest.main()
