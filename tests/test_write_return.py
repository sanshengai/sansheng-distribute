"""Producer export → adopted media draft → fresh readback → Write consumer.

All remote responses below are offline fixtures, never platform evidence.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_legacy_adoption as legacy
from publish_core import PublishError, digest, file_hash, validate, write_json
from write_bridge import return_to_write


@unittest.skipUnless((legacy.ROOT/'sansheng-write/scripts/legacy_draft_handoff.py').is_file(), 'optional Write checkout absent')
class WriteReturnTests(unittest.TestCase):
    def setUp(self):
        self.fx = legacy.LegacyAdoptionTests(); self.fx.setUp()
        self.fx.export(); self.fx.adopt()
        self.root = self.fx.root
        self.key = self.fx.job['key']
        self.request = self.fx.store.directory/'requests'/f'{self.key}.json'
        self.component = '<mpvideo vid="test-only-video"></mpvideo>'
        self.fx.actual['content'] = self.fx.actual['content'].replace(self.fx.anchor,self.component)
        visual = self.root/'visual.txt'; visual.write_text('Offline fixture; no real platform verification')
        asset = next(a for a in self.fx.manifest['assets'] if a['id']=='video')
        self.native = dict(manifest_digest=digest(self.fx.manifest),remote_id='old-draft',components=[dict(
            slot_id='video',kind='video',asset_sha256=asset['sha256'],
            component_sha256=hashlib.sha256(self.component.encode()).hexdigest(),
            remote_media_id='test-only-video',remote_id_attribute='vid',upload_evidence='offline test only',
            saved=True,reopened=True,loaded=True,played=True)],
            layout_review=dict(reviewed=True,content_sha256=hashlib.sha256(self.fx.actual['content'].encode()).hexdigest(),
                               evidence=[dict(path=str(visual),sha256=file_hash(visual))]))
        write_json(self.request.with_suffix('.native-media.json'),self.native)
        self.ready = dict(manifest=self.fx.receipt['manifest'],manifest_digest=self.fx.receipt['manifest_digest'])
        self.scripts = Path(__file__).resolve().parents[2]/'sansheng-write/scripts'

    def tearDown(self):
        self.fx.tearDown()

    def bridge(self):
        with patch('write_bridge.invoke',side_effect=self.fx.invoke), \
             patch('evidence.verify_publish_ready',return_value=(self.ready,[])), \
             patch('release_to_draft.brand',return_value={}):
            return return_to_write(self.fx.store,self.key,self.fx.profile,self.root,self.scripts)

    def consume(self, path):
        from publish_return import import_return
        with patch('publish_return.verify_publish_ready',return_value=(self.ready,[])), \
             patch('release_to_draft.brand',return_value={}):
            return import_return(self.root,path)

    def test_producer_profile_is_bound_before_brand_resolution(self):
        bound=[]
        def brand():
            if bound != [self.root]:
                raise RuntimeError('workspace placeholders are unbound')
            return {}
        with patch('profile_config.bind_workspace',side_effect=lambda root:bound.append(root)), \
             patch('write_bridge.invoke',side_effect=self.fx.invoke), \
             patch('evidence.verify_publish_ready',return_value=(self.ready,[])), \
             patch('release_to_draft.brand',side_effect=brand):
            result=return_to_write(self.fx.store,self.key,self.fx.profile,self.root,self.scripts)
        self.assertEqual(result['draft_media_id'],'old-draft')

    def test_media_roundtrip_preserves_original_receipt_and_never_uploads(self):
        old = (self.root/'_publish-receipt.json').read_bytes()
        result = self.bridge(); receipt = self.consume(result['receipt'])
        self.assertEqual(receipt['draft_media_id'],'old-draft')
        self.assertTrue(receipt['remote_readback']['checks']['native_media'])
        self.assertEqual((self.root/'_publish-receipt.json').read_bytes(),old)
        validate(self.fx.manifest)  # Old records in adoption inputs remain sealed.
        repeated = self.bridge()
        self.assertEqual(repeated['draft_media_id'],'old-draft')
        self.assertNotEqual(result['receipt'], repeated['receipt'])
        from publish_return import validate_return
        validate_return(self.root,receipt)  # Fresh reads preserve old proof files.
        self.assertTrue((self.root/'过程记录/_external-publish-receipt.json').is_file())

    def test_pending_playback_is_not_importable(self):
        self.native['components'][0]['played'] = False
        write_json(self.request.with_suffix('.native-media.json'),self.native)
        with self.assertRaisesRegex(PublishError,'尚未完成'): self.bridge()
        self.assertFalse((self.root/'过程记录/publish/returned').exists())

    def test_final_ledger_state_does_not_skip_fresh_remote_readback(self):
        self.bridge()
        self.fx.actual['content'] = self.fx.actual['content'].replace('前','被编辑')
        with self.assertRaisesRegex(PublishError,'差异'): self.bridge()

    def test_changed_source_and_changed_evidence_invalidate_consumer(self):
        result = self.bridge()
        original = (self.root/'video.mp4').read_bytes()
        (self.root/'video.mp4').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'源文件已变化'): self.consume(result['receipt'])
        (self.root/'video.mp4').write_bytes(original)
        (self.root/'visual.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'视觉观察证据'): self.consume(result['receipt'])

    def test_wrong_destination_and_wrong_existing_id_refused(self):
        result = self.bridge()
        write_json(self.root/'_release-attempt.json',dict(draft_media_id='another-draft'))
        with self.assertRaisesRegex(ValueError,'另一草稿'): self.consume(result['receipt'])
        with self.assertRaisesRegex(PublishError,'原 Write'):
            return_to_write(self.fx.store,self.key,self.fx.profile,self.root/'other',self.scripts)

    def test_empty_or_tampered_receipt_refused(self):
        from publish_return import validate_return
        with self.assertRaises(ValueError): validate_return(self.root,{})
        result = self.bridge(); receipt = json.loads(Path(result['receipt']).read_text())
        for edit in ('draft_media_id','cover_media_id'):
            changed = copy.deepcopy(receipt); changed[edit] = 'wrong'
            with self.assertRaises(ValueError): validate_return(self.root,changed)

    def test_changed_producer_manifest_refused_before_import(self):
        result = self.bridge(); path = Path(result['receipt'])
        receipt = json.loads(path.read_text())
        receipt['manifest'] = {}
        write_json(path,receipt)
        with self.assertRaisesRegex(ValueError,'生产端证据'): self.consume(path)
        self.assertFalse((self.root/'过程记录/_external-publish-receipt.json').exists())

    def test_pipeline_consumer_and_old_command_use_same_local_result(self):
        import pipeline
        result = self.bridge()
        with patch('publish_return.verify_publish_ready',return_value=(self.ready,[])), \
             patch('release_to_draft.brand',return_value={}), \
             patch.object(pipeline,'load_state',return_value={'stages':{}}), \
             patch.object(pipeline,'verify_stage',return_value=(True,[])), \
             patch.object(pipeline,'_record_stage_success'), patch.object(pipeline,'save_state') as saved:
            self.assertEqual(pipeline.cmd_import_publish(self.root,result['receipt']),0)
            self.assertEqual(saved.call_args.args[1]['stages']['publish']['publisher'],'external')
            handoff_path = self.root/'过程记录/_wechat-audio-handoff.json'
            handoff = handoff_path.read_bytes()
            with patch('release_to_draft.release_to_draft') as uploader:
                with self.assertRaises(SystemExit) as stopped: pipeline.cmd_release_to_draft(self.root)
                self.assertEqual(stopped.exception.code,0)
                uploader.assert_not_called()
            self.assertEqual(handoff_path.read_bytes(),handoff)


if __name__ == '__main__': unittest.main()
