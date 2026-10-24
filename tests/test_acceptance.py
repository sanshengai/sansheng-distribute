"""Behavioral regression cases for missing media, local preflight and lost-ID recovery."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from publish_core import PublishError, Store, digest, preflight, read_json, run, validate, write_json
from wechat_adapter import dispatch
from test_wechat_adapter import FakeApi


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name, value in [('article.html', b'<p>before</p><p>after</p>'), ('cover.png', b'cover'),
                            ('audio.mp3', b'audio'), ('video.mp4', b'video')]:
            (self.root/name).write_bytes(value)
        self.manifest = dict(schema_version=1, job_id='acceptance', producer='test', content_status='final',
            source_root=str(self.root), documents=[dict(id='doc', path='article.html', format='html')],
            assets=[dict(id='cover', path='cover.png', kind='image')], media_slots=[],
            targets=[dict(id='wx', platform='wechat', account='test', content_type='article', endpoint='draft',
                          title='fixture', document_id='doc', asset_ids=['cover'], cover_asset_id='cover')])
        validate(self.manifest, sealing=True)
        (self.root/'secret.env').write_text('WECHAT_APP_ID=fixture-app\nWECHAT_APP_SECRET=fixture-secret\n')
        self.write_scripts = self.root/'write'; self.write_scripts.mkdir()
        (self.write_scripts/'wechat_api.py').write_text('# local prerequisite fixture only\n')
        self.profile_path = self.root/'profile.json'
        argv = [sys.executable, str(SCRIPTS/'wechat_adapter.py')]
        flags = ['--profile', str(self.profile_path), '--write-scripts', str(self.write_scripts)]
        self.profile = {'accounts': {'test': {'platforms': ['wechat'], 'restricted': False,
            'wechat_env_file': str(self.root/'secret.env'), 'wechat_appid_sha256': hashlib.sha256(b'fixture-app').hexdigest()}},
            'adapters': {'wechat': {'enabled_capabilities': ['article:draft'],
                'execute': argv+['execute']+flags, 'verify': argv+['verify']+flags}}}
        write_json(self.profile_path, self.profile)
        self.path = self.root/'manifest.json'; write_json(self.path, self.manifest)
        self.store = Store(self.root/'state'); self.key = self.store.prepare(self.manifest, 'wx')['key']
        self.api = FakeApi(); self.actions = []

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def invoke(self, config, action, path, **kwargs):
        self.actions.append(action)
        return dispatch(path, self.profile, self.api, action=action)

    def lose_response(self):
        self.store.authorize(self.manifest, 'wx', 'fixture: create draft only')
        add = self.api.add_draft
        def timeout(token, article):
            add(token, article)
            raise TimeoutError('simulated loss after remote acceptance')
        with patch.object(self.api, 'add_draft', side_effect=timeout), patch('publish_core.invoke', side_effect=self.invoke):
            with self.assertRaises(TimeoutError): run(self.path, 'wx', self.profile, self.store)
        self.assertEqual(self.store.status(self.key)['state'], 'submission_unknown')
        self.assertIsNone(self.store.status(self.key)['remote_id'])
        return self.store.directory/'snapshots'/f'{self.key}.json'

    def test_declared_or_orphan_audio_video_cannot_be_silently_dropped(self):
        for kind, filename in [('audio','audio.mp3'), ('video','video.mp4')]:
            for association in ['target', 'document', 'none']:
                with self.subTest(kind=kind, association=association):
                    m=copy.deepcopy(self.manifest); m['assets'].append(dict(id='media', path=filename, kind=kind))
                    if association=='target': m['targets'][0]['asset_ids'].append('media')
                    if association=='document': m['documents'][0]['asset_ids']=['media']
                    with self.assertRaisesRegex(PublishError, '音视频'): validate(m, sealing=True)
        with self.assertRaises(PublishError): validate({})

    def test_video_for_another_destination_does_not_require_article_slot(self):
        m=copy.deepcopy(self.manifest);m['assets'].append(dict(id='video',path='video.mp4',kind='video'))
        m['targets'].append(dict(id='dy',platform='douyin',account='test',content_type='video',endpoint='draft',
                                 title='video fixture',asset_ids=['video']))
        validate(m,sealing=True)

    def test_unreachable_document_does_not_consume_media(self):
        m=copy.deepcopy(self.manifest)
        (self.root/'unused.txt').write_text('unused document')
        m['documents'].append(dict(id='unused',path='unused.txt',format='text',asset_ids=['audio']))
        m['assets'].append(dict(id='audio',path='audio.mp3',kind='audio'))
        with self.assertRaisesRegex(PublishError,'音视频未关联'):validate(m,sealing=True)

    def test_missing_title_fails_before_any_platform_call(self):
        for title in [None, '']:
            m=copy.deepcopy(self.manifest);m['targets'][0]['title']=title;m['targets'][0]['text']='fallback'
            validate(m,sealing=True);write_json(self.path,m)
            key=self.store.prepare(m,'wx')['key'];self.store.authorize(m,'wx','fixture draft only')
            with patch('publish_core.invoke') as invoke:
                with self.assertRaisesRegex(PublishError,'标题'):run(self.path,'wx',self.profile,self.store)
                invoke.assert_not_called()
            self.assertEqual(self.store.status(key)['state'],'prepared')
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM account_locks').fetchone()[0],0)

    def test_media_position_in_another_article_cannot_satisfy_target(self):
        m=copy.deepcopy(self.manifest)
        (self.root/'second.html').write_text('<p>【发布媒体:slot】</p>')
        m['documents'].append(dict(id='other',path='second.html',format='html'))
        m['assets'].append(dict(id='video',path='video.mp4',kind='video'))
        m['targets'][0]['asset_ids'].append('video')
        m['targets'].append(dict(m['targets'][0],id='second',document_id='other'))
        m['media_slots']=[dict(id='slot',document_id='other',asset_id='video')]
        with self.assertRaisesRegex(PublishError,'插入位置'):validate(m,sealing=True)

    def test_preflight_failure_never_enters_submission_window(self):
        self.manifest['targets'][0].pop('cover_asset_id');validate(self.manifest,sealing=True)
        write_json(self.path,self.manifest);key=self.store.prepare(self.manifest,'wx')['key']
        self.store.authorize(self.manifest,'wx','fixture draft only')
        with patch('publish_core.invoke',side_effect=self.invoke):
            with self.assertRaisesRegex(PublishError,'封面'):run(self.path,'wx',self.profile,self.store)
        self.assertEqual(self.store.status(key)['state'],'prepared');self.assertEqual(self.actions,[])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM account_locks').fetchone()[0],0)

    def test_preflight_checks_local_dependency_and_profile_consistency(self):
        self.assertTrue(preflight(self.manifest,'wx',self.profile)['local_only'])
        (self.write_scripts/'wechat_api.py').unlink()
        with self.assertRaisesRegex(PublishError,'模块缺失'):preflight(self.manifest,'wx',self.profile)
        (self.write_scripts/'wechat_api.py').write_text('# fixture')
        wrong=copy.deepcopy(self.profile);wrong['accounts']['test']['wechat_appid_sha256']='wrong'
        write_json(self.profile_path,wrong)
        with self.assertRaisesRegex(PublishError,'配置不一致'):preflight(self.manifest,'wx',self.profile)

    def test_acceptance_scope_is_exact_and_cannot_enable_publication(self):
        p=copy.deepcopy(self.profile);p['adapters']['wechat']['enabled_capabilities']=[]
        with self.assertRaisesRegex(PublishError,'尚未启用'):preflight(self.manifest,'wx',p)
        scope=dict(manifest_digest=digest(self.manifest),target_id='wx',platform='wechat',account='test',endpoint='draft')
        p['acceptance_runs']=[scope];self.assertTrue(preflight(self.manifest,'wx',p)['local_only'])
        for field,value in [('manifest_digest','wrong'),('account','other'),('target_id','other'),('endpoint','publish')]:
            bad=copy.deepcopy(p);bad['acceptance_runs'][0][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(PublishError,'尚未启用'):
                preflight(self.manifest,'wx',bad)
        p['accounts']['test']['restricted']=True
        with self.assertRaisesRegex(PublishError,'受限'):preflight(self.manifest,'wx',p)
        m=copy.deepcopy(self.manifest);m['targets'][0]['endpoint']='publish';validate(m,sealing=True)
        p['accounts']['test']['restricted']=False;p['acceptance_runs'][0].update(manifest_digest=digest(m),endpoint='publish')
        with self.assertRaisesRegex(PublishError,'尚未启用'):preflight(m,'wx',p)

    def test_recovery_after_source_cleanup_binds_same_draft_without_create(self):
        snapshot=self.lose_response()
        self.path.unlink();(self.root/'article.html').unlink();(self.root/'cover.png').unlink()
        with patch('publish_core.invoke',side_effect=self.invoke):
            value=run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
            self.assertEqual(value['remote_id'],'draft-fixture');self.assertEqual(value['state'],'pending')
            run(snapshot,'wx',self.profile,self.store,resume=True)
        self.assertEqual(self.api.creates,1);self.assertEqual(self.actions,['execute','verify','verify'])
        record=read_json(self.store.directory/'requests'/f'{self.key}.wechat.json')
        self.assertEqual(record['remote_id'],'draft-fixture')

    def test_recovery_refuses_wrong_body_cover_and_account(self):
        snapshot=self.lose_response();original=copy.deepcopy(self.api.articles['draft-fixture'])
        for field,value in [('content','<p>wrong</p>'),('thumb_media_id','wrong-cover')]:
            self.api.articles['draft-fixture']=dict(original,**{field:value})
            with self.subTest(field=field),patch('publish_core.invoke',side_effect=self.invoke):
                with self.assertRaisesRegex(PublishError,'有差异'):
                    run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
            self.assertIsNone(self.store.status(self.key)['remote_id'])
            record=read_json(self.store.directory/'requests'/f'{self.key}.wechat.json')
            self.assertNotIn('remote_id',record)
        self.api.articles['draft-fixture']=original
        self.profile['accounts']['test']['wechat_appid_sha256']='wrong'
        with patch('publish_core.invoke',side_effect=self.invoke),self.assertRaisesRegex(PublishError,'账号绑定'):
            run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
        self.assertEqual(self.api.creates,1)

    def test_recovery_requires_real_unknown_attempt_and_dedicated_verified_response(self):
        with self.assertRaisesRegex(PublishError,'未知提交'):
            run(self.path,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
        snapshot=self.lose_response()
        fake=dict(manifest_digest=digest(self.manifest),target_id='wx',platform='wechat',account='test',
                  state='pending',remote_id='draft-fixture')
        with patch('publish_core.invoke',return_value=fake),self.assertRaisesRegex(PublishError,'未经官方'):
            run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
        self.assertIsNone(self.store.status(self.key)['remote_id'])
        request=self.store.directory/'requests'/f'{self.key}.json'
        with self.assertRaisesRegex(PublishError,'只允许官方读回'):
            dispatch(request,self.profile,self.api,action='execute')
        self.assertEqual(self.api.creates,1)

    def test_recovery_rejects_valid_account_rebinding_before_remote_lookup(self):
        snapshot=self.lose_response()
        (self.root/'secret.env').write_text('WECHAT_APP_ID=other-app\nWECHAT_APP_SECRET=other-secret\n')
        self.profile['accounts']['test']['wechat_appid_sha256']=hashlib.sha256(b'other-app').hexdigest()
        with patch('publish_core.invoke',side_effect=self.invoke),patch.object(self.api,'get_draft') as lookup:
            with self.assertRaisesRegex(PublishError,'原提交账号身份'):
                run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
            lookup.assert_not_called()
        self.assertIsNone(self.store.status(self.key)['remote_id'])

    def test_recovery_rejects_missing_original_account_binding(self):
        snapshot=self.lose_response();path=self.store.directory/'requests'/f'{self.key}.wechat.json'
        record=read_json(path);record.pop('account_appid_sha256');write_json(path,record)
        with patch('publish_core.invoke',side_effect=self.invoke),self.assertRaisesRegex(PublishError,'原提交账号身份绑定'):
            run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')

    def test_recovery_rejects_draft_owned_by_another_job(self):
        snapshot=self.lose_response()
        other=copy.deepcopy(self.manifest);other['targets'][0]['title']='other task'
        job=self.store.prepare(other,'wx')
        self.store.db.execute('UPDATE jobs SET remote_id=? WHERE key=?',('draft-fixture',job['key']))
        self.store.db.commit()
        with patch('publish_core.invoke') as invoke:
            with self.assertRaisesRegex(PublishError,'归属其他发布任务'):
                run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
            invoke.assert_not_called()

    def test_recovery_refuses_missing_submission_record_and_corrupt_snapshot(self):
        snapshot=self.lose_response();record=self.store.directory/'requests'/f'{self.key}.wechat.json'
        saved=read_json(record);record.unlink()
        with patch('publish_core.invoke',side_effect=self.invoke),self.assertRaisesRegex(PublishError,'原提交窗口'):
            run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
        write_json(record,saved);m=read_json(snapshot);m['producer']='tampered';write_json(snapshot,m)
        with self.assertRaisesRegex(PublishError,'快照与账本不一致'):
            run(snapshot,'wx',self.profile,self.store,resume=True,recover_remote_id='draft-fixture')
        self.assertEqual(self.api.creates,1)
