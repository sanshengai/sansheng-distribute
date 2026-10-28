"""Standalone component tasks bind original video and cannot silently resend."""
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch
import test_youtube_continue_core as fixture
from publish_core import PublishError, run, run_youtube_component, run_youtube_bundle, validate, digest

class ComponentLedgerTest(unittest.TestCase):
    def setUp(self):
        fixture.YoutubeContinuationCoreTest.setUp(self)
        self.fake.interrupt_after_accept = False
        result = run(self.path,'t',self.profile,self.store)
        self.assertEqual(result['state'],'published')
        self.cover=self.root/'cover.png';self.cover.write_bytes(b'PNG-example')
        self.spec={'cover':{'id':'cover','absolute_path':str(self.cover),
                            'sha256':hashlib.sha256(self.cover.read_bytes()).hexdigest()},
                   'playlist_id':'PLabcdefghijk','playlist_position':2}
    def task(self):return self.store.prepare_youtube_component(self.key,self.spec,self.profile)
    def response(self,key,part,state):
        return {'component_key':key,'original_job_key':self.key,'video_id':self.store.status(self.key)['remote_id'],
                'part':part,'state':state}
    def test_requires_published_original_and_bound_snapshot(self):
        self.store.db.execute("UPDATE jobs SET state='pending' WHERE key=?",(self.key,));self.store.db.commit()
        with self.assertRaises(PublishError):self.task()
    def test_rejects_changed_cover(self):
        self.cover.write_bytes(b'changed')
        with self.assertRaises(PublishError):self.task()
    def test_requires_grant_and_explicit_revocation_blocks_write(self):
        key=self.task()['key']
        with self.assertRaises(PublishError):self.store.begin_youtube_component(key,'cover',self.profile)
        self.store.authorize_youtube_component(key,'authorize this component')
        self.store.revoke_youtube_component(key)
        with self.assertRaises(PublishError):self.store.begin_youtube_component(key,'cover',self.profile)
    def test_unknown_write_is_durable_and_never_replayed(self):
        key=self.task()['key'];self.store.authorize_youtube_component(key,'authorize')
        self.store.begin_youtube_component(key,'cover',self.profile)
        self.assertEqual(self.store.youtube_component_status(key)['cover_state'],'unknown')
        with self.assertRaises(PublishError):self.store.begin_youtube_component(key,'cover',self.profile)
    def test_other_account_task_lock_refused(self):
        key=self.task()['key'];self.store.authorize_youtube_component(key,'authorize')
        self.store.db.execute('INSERT INTO account_locks VALUES (?,?,?)',('youtube','owner','other'));self.store.db.commit()
        with self.assertRaises(PublishError):self.store.begin_youtube_component(key,'cover',self.profile)
    def test_component_response_wrong_video_refused(self):
        key=self.task()['key'];self.store.authorize_youtube_component(key,'authorize');self.store.begin_youtube_component(key,'cover',self.profile)
        obs=self.response(key,'cover','accepted');obs['video_id']='wrong_video'
        with self.assertRaises(PublishError):self.store.observe_youtube_component(key,'cover',obs)
        self.assertEqual(self.store.youtube_component_status(key)['cover_state'],'unknown')
    def test_verified_playlist_requires_readback_and_cover_requires_review(self):
        key=self.task()['key'];self.store.authorize_youtube_component(key,'authorize');self.store.begin_youtube_component(key,'playlist',self.profile)
        obs=self.response(key,'playlist','verified')
        with self.assertRaises(PublishError):self.store.observe_youtube_component(key,'playlist',obs)
        self.assertEqual(self.store.observe_youtube_component(key,'playlist',obs,verified=True)['playlist_state'],'verified')
        self.store.begin_youtube_component(key,'cover',self.profile)
        obs=self.response(key,'cover','verified')
        with self.assertRaises(PublishError):self.store.observe_youtube_component(key,'cover',obs,verified=True)
    def test_needs_authorization_must_prove_no_write(self):
        key=self.task()['key'];self.store.authorize_youtube_component(key,'authorize');self.store.begin_youtube_component(key,'playlist',self.profile)
        obs=self.response(key,'playlist','needs_authorization')
        with self.assertRaises(PublishError):self.store.observe_youtube_component(key,'playlist',obs)
        obs['no_write']=True
        self.assertEqual(self.store.observe_youtube_component(key,'playlist',obs)['playlist_state'],'needs_authorization')
    def test_channel_change_and_restricted_account_refused(self):
        key=self.task()['key'];self.store.authorize_youtube_component(key,'authorize')
        self.profile['accounts']['owner']['restricted']=True
        with self.assertRaises(PublishError):self.store.begin_youtube_component(key,'cover',self.profile)
    def test_other_component_unknown_keeps_account_lock(self):
        key=self.task()['key'];self.store.authorize_youtube_component(key,'authorize')
        self.store.begin_youtube_component(key,'cover',self.profile)
        self.store.begin_youtube_component(key,'playlist',self.profile)
        self.store.observe_youtube_component(key,'playlist',self.response(key,'playlist','accepted'))
        row=self.store.db.execute('SELECT job_key FROM account_locks WHERE platform=? AND account=?',('youtube','owner')).fetchone()
        self.assertIsNotNone(row);self.assertEqual(row['job_key'],key)
        self.store.observe_youtube_component(key,'cover',self.response(key,'cover','accepted'))
        self.assertIsNotNone(self.store.db.execute('SELECT job_key FROM account_locks WHERE platform=? AND account=?',('youtube','owner')).fetchone())
        self.store.observe_youtube_component(key,'playlist',self.response(key,'playlist','verified'),verified=True)
        self.assertIsNotNone(self.store.db.execute('SELECT job_key FROM account_locks WHERE platform=? AND account=?',('youtube','owner')).fetchone())
        self.store.observe_youtube_component(key,'cover',self.response(key,'cover','needs_visual_review'),verified=True)
        self.assertIsNone(self.store.db.execute('SELECT job_key FROM account_locks WHERE platform=? AND account=?',('youtube','owner')).fetchone())

    def configure_component(self):
        self.profile['accounts']['owner']['youtube_allow_component_write']=True
        adapter=self.profile['adapters']['youtube']
        for action in ('component','verify_component'):
            adapter[action]=[adapter['execute'][0],adapter['execute'][1],action,'--profile',str(self.profile_file)]
        self.profile_file.write_text(__import__('json').dumps(self.profile))

    def test_run_requires_explicit_enable_and_grant(self):
        key=self.task()['key']
        with patch('youtube_adapter.preflight_youtube'), patch('publish_core.invoke') as call:
            with self.assertRaises(PublishError):run_youtube_component(self.store,key,'playlist',self.profile)
            self.configure_component()
            with self.assertRaises(PublishError):run_youtube_component(self.store,key,'playlist',self.profile)
            call.assert_not_called()
            self.assertEqual(self.store.youtube_component_status(key)['playlist_state'],'prepared')

    def test_execute_saves_acceptance_before_verification_and_no_replay(self):
        self.configure_component();key=self.task()['key'];self.store.authorize_youtube_component(key,'actual user authorization')
        calls=[]
        def invoke(_config,action,request_path,**kwargs):
            calls.append(action)
            request=__import__('json').loads(request_path.read_text())
            self.assertEqual(request['component_job']['component_digest'],key)
            self.assertEqual(request['component_job']['part'],'playlist')
            if action=='verify_component':
                self.assertEqual(self.store.youtube_component_status(key)['playlist_state'],'accepted')
            return self.response(key,'playlist','accepted' if action=='component' else 'verified')
        with patch('youtube_adapter.preflight_youtube'), patch('publish_core.invoke',side_effect=invoke):
            result=run_youtube_component(self.store,key,'playlist',self.profile)
            self.assertEqual(result['playlist_state'],'verified')
            with self.assertRaises(PublishError):run_youtube_component(self.store,key,'playlist',self.profile)
        self.assertEqual(calls,['component','verify_component'])

    def test_interrupted_component_recovery_is_read_only_even_after_revoke(self):
        self.configure_component();key=self.task()['key'];self.store.authorize_youtube_component(key,'actual user authorization')
        calls=[]
        def invoke(_config,action,_request_path,**kwargs):
            calls.append(action)
            if action=='component':raise PublishError('network result unknown')
            return self.response(key,'playlist','verified')
        with patch('youtube_adapter.preflight_youtube'), patch('publish_core.invoke',side_effect=invoke):
            with self.assertRaises(PublishError):run_youtube_component(self.store,key,'playlist',self.profile)
            self.assertEqual(self.store.youtube_component_status(key)['playlist_state'],'unknown')
            with self.assertRaises(PublishError):run_youtube_component(self.store,key,'playlist',self.profile)
            self.store.revoke_youtube_component(key)
            self.assertEqual(run_youtube_component(self.store,key,'playlist',self.profile,verify_only=True)['playlist_state'],'verified')
        self.assertEqual(calls,['component','verify_component'])

    def test_visual_review_is_bound_to_actual_adapter_download_not_arbitrary_file(self):
        from datetime import datetime, timezone, timedelta
        key=self.task()['key'];self.store.authorize_youtube_component(key,'authorize');self.store.begin_youtube_component(key,'cover',self.profile)
        self.store.observe_youtube_component(key,'cover',self.response(key,'cover','accepted'))
        video=self.store.status(self.key)['remote_id']
        thumb=self.root/'download.png';thumb.write_bytes(b'downloaded-thumbnail')
        sha=hashlib.sha256(thumb.read_bytes()).hexdigest()
        url='https://i.ytimg.com/vi/'+video+'/hqdefault.jpg'
        obs={**self.response(key,'cover','needs_visual_review'), 'thumbnail_url':url,
             'thumbnail_path':str(thumb), 'thumbnail_sha256':sha, 'cover_sha256':self.spec['cover']['sha256']}
        self.store.observe_youtube_component(key,'cover',obs,verified=True)
        now=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        def review(url_value=url,path=str(thumb)):
            return self.store.record_youtube_cover_review(self.key,video,self.spec['cover']['sha256'],
                url_value,path,'agent','checked cover and title visually',now,component_key=key)
        with self.assertRaises(PublishError):review(url_value='https://i.ytimg.com/vi/'+video+'/mqdefault.jpg')
        other=self.root/'other.png';other.write_bytes(b'different-image')
        with self.assertRaises(PublishError):review(path=str(other))
        thumb.write_bytes(b'changed-after-readback')
        with self.assertRaises(PublishError):review()
        thumb.write_bytes(b'downloaded-thumbnail')
        result=review()
        self.assertEqual(result['review_kind'],'observer_visual_assertion')
        self.assertEqual(self.store.youtube_component_status(key)['cover_state'],'visually_reviewed')

class BundleCoreTest(unittest.TestCase):
    def setUp(self):
        fixture.YoutubeContinuationCoreTest.setUp(self)
        self.fake.interrupt_after_accept=False
        self.cover=self.root/'cover.png';self.cover.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        self.manifest['job_id']='series-s2-test'
        self.manifest['assets'].append({'id':'cover','path':'cover.png','kind':'image'})
        target=self.manifest['targets'][0];target['asset_ids'].append('cover');target['cover_asset_id']='cover';target['title']='S2E01 | test'
        target['platform_metadata'].update(playlist_id='PLabcdefghijk',playlist_position=0)
        validate(self.manifest,sealing=True)
        self.path.write_text(__import__('json').dumps(self.manifest))
        self.profile['accounts']['owner']['youtube_allow_component_write']=True
        adapter=self.profile['adapters']['youtube']
        for action in ('component','verify_component'):
            adapter[action]=[adapter['execute'][0],adapter['execute'][1],action,'--profile',str(self.profile_file)]
        self.profile_file.write_text(__import__('json').dumps(self.profile))
        self.key=self.store.prepare_youtube_bundle(self.manifest,'t',self.profile)['key']
        self.store.authorize(self.manifest,'t','bundle explicit user authorization')
        self.component_actions=[]
        original=fixture.dispatch
        def invoke(config,action,request_path,**kwargs):
            if action in {'execute','verify','continue'}:
                return original(request_path,action,self.profile,lambda _:self.fake)
            req=__import__('json').loads(request_path.read_text());job=req['component_job'];part=job['part']
            self.component_actions.append((action,part))
            response={'component_key':job['component_key'],'original_job_key':self.key,
                      'video_id':job['video_id'],'part':part}
            if action=='component':return {**response,'state':'accepted'}
            if part=='playlist':return {**response,'state':'verified'}
            thumbnail=self.root/'downloaded.jpg';thumbnail.write_bytes(b'thumbnail')
            return {**response,'state':'needs_visual_review',
                    'thumbnail_url':'https://i.ytimg.com/vi/'+job['video_id']+'/hqdefault.jpg',
                    'thumbnail_path':str(thumbnail),'thumbnail_sha256':hashlib.sha256(thumbnail.read_bytes()).hexdigest(),
                    'cover_sha256':job['cover']['sha256']}
        self.dispatch_patch=patch('publish_core.invoke',side_effect=invoke);self.dispatch_patch.start();self.addCleanup(self.dispatch_patch.stop)

    def approve_cover(self,status):
        from datetime import datetime, timezone, timedelta
        cover=status['components']['cover']
        evidence=self.store.youtube_component_status(cover['key'])['cover_visual_evidence']
        return self.store.record_youtube_cover_review(self.key,status['video_id'],evidence['cover_sha256'],
            evidence['thumbnail_url'],evidence['thumbnail_path'],'agent','visually checked title and image',
            (datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat(),component_key=cover['key'])

    def test_full_bundle_requires_separate_entry_and_records_pending_parts(self):
        with self.assertRaises(PublishError):run(self.path,'t',self.profile,self.store)
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),0)
        status=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(status['video_state'],'verified_public')
        self.assertEqual(status['overall_state'],'pending')
        self.assertEqual(status['components']['playlist']['state'],'verified')
        cover=status['components']['cover'];self.assertEqual(cover['state'],'needs_visual_review')
        with self.assertRaises(PublishError):self.store.finalize_youtube_bundle(self.key)
        self.approve_cover(status)
        final=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(final['overall_state'],'published')
        self.assertEqual(final['components']['cover']['state'],'visually_reviewed')
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)
        self.assertEqual(self.component_actions,[('component','playlist'),('verify_component','playlist'),
                  ('component','cover'),('verify_component','cover'),('verify_component','playlist'),('verify_component','cover')])

    def test_caller_cannot_finalize_by_replaying_latest_historical_event_ids(self):
        status=run_youtube_bundle(self.store,self.key,self.profile);self.approve_cover(status)
        video_event=self.store.db.execute("SELECT id FROM events WHERE job_key=? AND state='pending' ORDER BY id DESC",(self.key,)).fetchone()['id']
        playlist_event=self.store.db.execute("SELECT id FROM youtube_component_events WHERE component_key=? AND part='playlist' ORDER BY id DESC",(status['components']['playlist']['key'],)).fetchone()['id']
        with self.assertRaises((PublishError, TypeError)):
            self.store.finalize_youtube_bundle(self.key, video_event_id=video_event, playlist_event_id=playlist_event)
        self.assertEqual(self.store.status(self.key)['state'],'pending')

    def test_changed_remote_cover_same_url_requires_new_visual_review(self):
        status=run_youtube_bundle(self.store,self.key,self.profile);review=self.approve_cover(status)
        self.dispatch_patch.stop();actions=[]
        def invoke(config,action,path,**kwargs):
            if action in {'execute','verify','continue'}:
                return fixture.dispatch(path,action,self.profile,lambda _:self.fake)
            job=__import__('json').loads(path.read_text())['component_job'];part=job['part'];actions.append((action,part))
            response={'component_key':job['component_key'],'original_job_key':self.key,'video_id':job['video_id'],'part':part}
            if part=='playlist':return {**response,'state':'verified'}
            changed=self.root/'changed-thumbnail.jpg';changed.write_bytes(b'new cover image')
            return {**response,'state':'needs_visual_review','thumbnail_url':review['thumbnail_url'],
                    'thumbnail_path':str(changed),'thumbnail_sha256':hashlib.sha256(changed.read_bytes()).hexdigest(),
                    'cover_sha256':job['cover']['sha256']}
        with patch('publish_core.invoke',side_effect=invoke):
            result=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(result['overall_state'],'pending')
        self.assertEqual(result['components']['cover']['state'],'needs_visual_review')
        self.assertIn('cover_readback',result['component_errors'])
        self.assertEqual(self.store.youtube_cover_review(self.key,result['video_id'],review['cover_sha256'])['thumbnail_sha256'],review['thumbnail_sha256'])
        self.assertEqual(actions,[('verify_component','playlist'),('verify_component','cover')])

    def test_cover_readback_error_does_not_complete_or_erase_review(self):
        status=run_youtube_bundle(self.store,self.key,self.profile);review=self.approve_cover(status)
        self.dispatch_patch.stop()
        def invoke(config,action,path,**kwargs):
            if action in {'execute','verify','continue'}:
                return fixture.dispatch(path,action,self.profile,lambda _:self.fake)
            job=__import__('json').loads(path.read_text())['component_job']
            if job['part']=='cover':raise PublishError('cover fetch unavailable')
            return {'component_key':job['component_key'],'original_job_key':self.key,'video_id':job['video_id'],
                    'part':'playlist','state':'verified'}
        with patch('publish_core.invoke',side_effect=invoke):
            result=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(result['overall_state'],'pending')
        self.assertEqual(result['components']['cover']['state'],'unknown')
        self.assertIn('cover_readback',result['component_errors'])
        self.assertEqual(self.store.youtube_cover_review(self.key,result['video_id'],review['cover_sha256'])['thumbnail_sha256'],review['thumbnail_sha256'])

    def test_video_not_public_at_finalize_stays_pending_and_keeps_cover_review(self):
        status=run_youtube_bundle(self.store,self.key,self.profile);self.approve_cover(status)
        self.fake.public=False
        result=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(result['overall_state'],'pending')
        self.assertNotEqual(result['video_state'],'verified_public')
        self.assertEqual(result['components']['cover']['state'],'visually_reviewed')
        self.assertIn('video_readback',result['component_errors'])
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)

    def test_video_readback_exception_invalidates_old_success(self):
        status=run_youtube_bundle(self.store,self.key,self.profile);self.approve_cover(status)
        self.fake.mismatch=True
        result=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(result['overall_state'],'pending')
        self.assertNotEqual(result['video_state'],'verified_public')
        self.assertEqual(result['components']['cover']['state'],'visually_reviewed')
        self.assertIn('video_readback',result['component_errors'])

    def test_forged_video_verify_command_cannot_complete_bundle(self):
        status=run_youtube_bundle(self.store,self.key,self.profile);self.approve_cover(status)
        fake_script=self.root/'forged.py';fake_script.write_text('print("forged")')
        self.profile['adapters']['youtube']['verify']=[self.profile['adapters']['youtube']['verify'][0],str(fake_script),
                                                       'verify','--profile',str(self.profile_file)]
        self.profile_file.write_text(__import__('json').dumps(self.profile))
        calls=len(self.fake.calls)
        result=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(result['overall_state'],'pending')
        self.assertIn('video_readback',result['component_errors'])
        self.assertEqual(len(self.fake.calls),calls)
        self.assertEqual(result['components']['cover']['state'],'visually_reviewed')

    def test_removed_playlist_at_finalize_stays_pending_without_write_or_cover_loss(self):
        status=run_youtube_bundle(self.store,self.key,self.profile);self.approve_cover(status)
        self.dispatch_patch.stop()
        actions=[]
        def invoke(config,action,path,**kwargs):
            if action in {'execute','verify','continue'}:
                return fixture.dispatch(path,action,self.profile,lambda _:self.fake)
            job=__import__('json').loads(path.read_text())['component_job'];actions.append((action,job['part']))
            return {'component_key':job['component_key'],'original_job_key':self.key,
                    'video_id':job['video_id'],'part':job['part'],'state':'unknown'}
        with patch('publish_core.invoke',side_effect=invoke):
            result=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(result['overall_state'],'pending')
        self.assertEqual(result['components']['playlist']['state'],'unknown')
        self.assertEqual(result['components']['cover']['state'],'visually_reviewed')
        self.assertEqual(actions,[('verify_component','playlist')])
        self.assertIn('playlist_readback',result['component_errors'])

    def test_playlist_readback_exception_invalidates_old_success(self):
        status=run_youtube_bundle(self.store,self.key,self.profile);self.approve_cover(status)
        self.dispatch_patch.stop()
        def invoke(config,action,path,**kwargs):
            if action in {'execute','verify','continue'}:
                return fixture.dispatch(path,action,self.profile,lambda _:self.fake)
            raise PublishError('simulated readback unavailable')
        with patch('publish_core.invoke',side_effect=invoke):
            result=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(result['overall_state'],'pending')
        self.assertEqual(result['components']['playlist']['state'],'unknown')
        self.assertEqual(result['components']['cover']['state'],'visually_reviewed')
        self.assertIn('playlist_readback',result['component_errors'])

    def test_execution_claim_cannot_fake_independent_video_readback(self):
        self.store.begin(self.manifest,'t',profile=self.profile)
        observation={'manifest_digest':digest(self.manifest),'target_id':'t','platform':'youtube','account':'owner',
                     'state':'pending','remote_id':'abcdefghijk','remote_url':'https://youtube.example/v',
                     'stage':'youtube_components_pending','video_only_verified':True,'_readback_verified':True}
        self.store.save_observation(self.key,observation,verified=False)
        self.assertNotEqual(self.store.youtube_bundle_status(self.key)['video_state'],'verified_public')
        with self.assertRaises(PublishError):self.store.finalize_youtube_bundle(self.key)

    def test_missing_original_grant_prevents_video_upload(self):
        self.store.db.execute('DELETE FROM grants');self.store.db.commit()
        with self.assertRaises(PublishError):run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(self.fake.calls,[])

    def test_missing_component_capability_blocks_video_before_remote_write(self):
        self.profile['accounts']['owner']['youtube_allow_component_write']=False
        self.profile_file.write_text(__import__('json').dumps(self.profile))
        with self.assertRaises(PublishError):run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(self.fake.calls,[])

    def test_interrupted_upload_continues_original_session_only(self):
        self.fake.interrupt_after_accept=True
        with self.assertRaises(PublishError):run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(self.store.status(self.key)['state'],'submission_unknown')
        self.assertEqual(run_youtube_bundle(self.store,self.key,self.profile,continue_upload=True)['video_state'],'verified_public')
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)

    def test_playlist_permission_issue_does_not_suppress_cover(self):
        self.dispatch_patch.stop()
        def invoke(config,action,request_path,**kwargs):
            if action in {'execute','verify','continue'}:
                return fixture.dispatch(request_path,action,self.profile,lambda _:self.fake)
            job=__import__('json').loads(request_path.read_text())['component_job'];part=job['part']
            self.component_actions.append((action,part))
            response={'component_key':job['component_key'],'original_job_key':self.key,'video_id':job['video_id'],'part':part}
            if part=='playlist':return {**response,'state':'needs_authorization','no_write':True}
            if action=='component':return {**response,'state':'accepted'}
            thumb=self.root/'remote.jpg';thumb.write_bytes(b'thumbnail')
            return {**response,'state':'needs_visual_review','thumbnail_url':'https://i.ytimg.com/vi/'+job['video_id']+'/hqdefault.jpg',
                    'thumbnail_path':str(thumb),'thumbnail_sha256':hashlib.sha256(thumb.read_bytes()).hexdigest(),'cover_sha256':job['cover']['sha256']}
        with patch('publish_core.invoke',side_effect=invoke):
            status=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(status['components']['playlist']['state'],'needs_authorization')
        self.assertEqual(status['components']['cover']['state'],'needs_visual_review')
        self.assertEqual(status['overall_state'],'pending')
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)

    def test_revoked_component_grant_is_not_silently_recreated(self):
        status=run_youtube_bundle(self.store,self.key,self.profile)
        playlist_key=status['components']['playlist']['key']
        self.store.db.execute("UPDATE youtube_component_jobs SET playlist_state='needs_authorization' WHERE key=?",(playlist_key,));self.store.db.commit()
        self.store.revoke_youtube_component(playlist_key)
        actions=list(self.component_actions)
        status=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertIn('playlist',status['component_errors'])
        self.assertEqual(self.component_actions,actions)
        self.assertEqual(status['overall_state'],'pending')

    def test_revoking_parent_grant_also_blocks_inherited_component_write(self):
        status=run_youtube_bundle(self.store,self.key,self.profile)
        playlist_key=status['components']['playlist']['key']
        self.store.db.execute("UPDATE youtube_component_jobs SET playlist_state='needs_authorization' WHERE key=?",(playlist_key,))
        self.store.db.execute('DELETE FROM grants');self.store.db.commit()
        with self.assertRaises(PublishError):self.store.begin_youtube_component(playlist_key,'playlist',self.profile)
        # A fresh, explicit authorization for this exact component stands on its own.
        self.store.authorize_youtube_component(playlist_key,'fresh authorization for this component')
        self.assertEqual(self.store.begin_youtube_component(playlist_key,'playlist',self.profile)['playlist_state'],'unknown')

    def test_unchanged_completed_components_do_not_repeat_writes(self):
        status=run_youtube_bundle(self.store,self.key,self.profile)
        actions=list(self.component_actions)
        again=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(self.component_actions,actions)
        self.assertEqual(status['components'],again['components'])
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)

    def test_bundle_grant_cannot_be_used_for_unapproved_playlist(self):
        run_youtube_bundle(self.store,self.key,self.profile)
        with self.assertRaises(PublishError):
            self.store.prepare_youtube_component(self.key,{'cover':None,'playlist_id':'PLotherabcdef','playlist_position':0},self.profile,from_bundle=True)

    def test_series_report_does_not_claim_bundle_is_published_with_pending_cover(self):
        from series_report import render_series_report
        run_youtube_bundle(self.store,self.key,self.profile)
        spec={'title':'Series','series_job_prefix':'series-s2-','series_code':'S2','episodes':[1],
              'platforms':[{'platform':'youtube','account':'owner'}],
              'targets':[{'job_key':self.key,'episode':1}]}
        page=render_series_report(spec,self.store)
        self.assertIn('待核验',page)
        self.assertNotIn('>已发布<',page)

    def test_bundle_invokes_real_adapter_with_simulated_platform_only(self):
        import json
        self.dispatch_patch.stop()
        base_call=self.fake.call; items=[]; calls=[]
        def api(method,url,**kwargs):
            calls.append((method,url))
            if '/playlists?' in url:
                return 200,{},json.dumps({'items':[{'id':'PLabcdefghijk','snippet':{'channelId':self.fake.channel}}]}).encode()
            if '/playlistItems?' in url:
                if method=='POST':
                    sn=json.loads(kwargs['data'])['snippet'];items.append({'id':'item-1','snippet':sn})
                    return 200,{},json.dumps(items[-1]).encode()
                return 200,{},json.dumps({'items':items}).encode()
            if '/thumbnails/set?' in url:
                return 200,{},json.dumps({'items':[{'default':{}}]}).encode()
            code,headers,body=base_call(method,url,**kwargs)
            if method=='GET' and '/videos?' in url:
                result=json.loads(body);result['items'][0]['snippet']['thumbnails']={'high':{'url':'https://i.ytimg.com/vi/'+self.fake.video+'/hqdefault.jpg'}}
                body=json.dumps(result).encode()
            return code,headers,body
        self.fake.call=api
        self.fake.granted_scopes=lambda: {'https://www.googleapis.com/auth/youtube'}
        self.fake.thumbnail=lambda url,video: b'\xff\xd8\xfffixture'
        with patch('publish_core.invoke',side_effect=lambda config,action,path,**kwargs:fixture.dispatch(path,action,self.profile,lambda _:self.fake)):
            status=run_youtube_bundle(self.store,self.key,self.profile)
        self.assertEqual(status['components']['playlist']['state'],'verified')
        self.assertEqual(status['components']['cover']['state'],'needs_visual_review')
        self.assertEqual(status['overall_state'],'pending')
        self.assertEqual(sum(method=='POST' and '/playlistItems?' in url for method,url in calls),1)
        self.assertEqual(sum(method=='POST' and '/thumbnails/set?' in url for method,url in calls),1)
