"""Offline integration: no network, no real accounts or media."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from publish_core import PublishError, digest, validate
from youtube_adapter import dispatch, checkpoint_path, session_url, preflight_youtube

CHANNEL='UC'+'a'*22
VIDEO='abcdefghijk'
SESSION='https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test-only'

class FakeAPI:
    def __init__(self, token):
        self.tokens=[]; self.calls=[]; self.token=token; self.mode='normal'; self.offset=0
        self.channel=CHANNEL; self.resource=None; self.public=True; self.video=VIDEO
        self.interrupt_after_accept=False; self.mismatch=False
    def call(self, method, url, *, headers=None, data=None):
        self.calls.append((method,url,headers or {},data))
        if '/channels?' in url:
            return 200,{},json.dumps({'items':[{'id':self.channel}]}).encode()
        if method=='POST':
            self.resource=json.loads(data); return 200,{'Location':SESSION},b''
        if method=='PUT' and (headers or {}).get('Content-Range','').startswith('bytes */'):
            if self.offset == 9: return 200,{},json.dumps({'id':self.video}).encode()
            return 308,({'Range':f'bytes=0-{self.offset-1}'} if self.offset else {}),b''
        if method=='PUT':
            self.offset += len(data)
            if self.interrupt_after_accept:
                self.interrupt_after_accept=False; raise PublishError('simulated timeout')
            if self.offset==9: return 201,{},json.dumps({'id':self.video}).encode()
            return 308,{'Range':f'bytes=0-{self.offset-1}'},b''
        if '/videos?' in url:
            sn=copy.deepcopy(self.resource['snippet']); sn['channelId']=self.channel
            if self.mismatch: sn['title']='wrong title'
            st=copy.deepcopy(self.resource['status']); st['uploadStatus']='processed'
            return 200,{},json.dumps({'items':[{'id':self.video,'snippet':sn,'status':st}]}).encode()
        raise AssertionError('unexpected call')
    def public_video(self, video_id):
        if not self.public: return None
        return {'title':self.resource['snippet']['title'],
                'thumbnail_url':f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'}

class YoutubeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); (self.root/'movie.mp4').write_bytes(b'123456789')
        self.manifest={'schema_version':1,'job_id':'job','producer':'test','content_status':'final',
          'source_root':str(self.root),'documents':[],'media_slots':[],
          'assets':[{'id':'v','path':'movie.mp4','kind':'video'}],
          'targets':[{'id':'t','platform':'youtube','account':'owner','endpoint':'publish',
                      'content_type':'video','title':'A title','description':'A description',
                      'tags':['topic'], 'asset_ids':['v'],'platform_metadata':{
                      'made_for_kids':False,'visibility':'public','altered_content':True}}]}
        validate(self.manifest,sealing=True)
        self.mpath=self.root/'manifest.json'; self.mpath.write_text(json.dumps(self.manifest))
        self.request={'manifest_path':str(self.mpath),'manifest_digest':digest(self.manifest),
                      'target':self.manifest['targets'][0], 'job':{}}
        self.rpath=self.root/'request.json'; self.rpath.write_text(json.dumps(self.request))
        self.profile={'accounts':{'owner':{'platforms':['youtube'], 'public_identities':{'youtube':CHANNEL},
                                           'youtube_token_env':'TEST_YOUTUBE_TOKEN'}}}
        self.env=patch.dict(os.environ,{'TEST_YOUTUBE_TOKEN':'test-token'}); self.env.start(); self.addCleanup(self.env.stop)
        self.fake=FakeAPI('test-token')
    def run_adapter(self, action): return dispatch(self.rpath,action,self.profile,lambda _:self.fake)
    def test_success_requires_channel_content_and_anonymous_readback(self):
        result=self.run_adapter('execute'); self.assertEqual(result['state'],'pending')
        self.assertEqual(result['remote_id'],VIDEO)
        self.assertNotIn('upload_id',json.dumps(result))
        cp=checkpoint_path(self.rpath); self.assertEqual(cp.stat().st_mode & 0o777,0o600)
        self.fake.public=False; self.assertEqual(self.run_adapter('verify')['state'],'pending')
        self.fake.public=True; self.assertEqual(self.run_adapter('verify')['state'],'published')
        self.fake.mismatch=True
        with self.assertRaises(PublishError): self.run_adapter('verify')
    def test_spoofed_public_thumbnail_is_not_sufficient(self):
        self.run_adapter('execute')
        self.fake.public_video=lambda _: {'title':'A title','thumbnail_url':f'https://evil.example/vi/{VIDEO}/image.jpg'}
        self.assertEqual(self.run_adapter('verify')['state'],'pending')
        self.fake.public_video=lambda _: {'title':'A title','thumbnail_url':f'https://i.ytimg.com/vi/not-{VIDEO}/image.jpg'}
        self.assertEqual(self.run_adapter('verify')['state'],'pending')

    def test_unknown_post_cannot_be_reinitiated(self):
        orig=self.fake.call
        def failing(method,url,**kwargs):
            if method=='POST': raise PublishError('simulated timeout')
            return orig(method,url,**kwargs)
        self.fake.call=failing
        with self.assertRaises(PublishError): self.run_adapter('execute')
        with self.assertRaises(PublishError): self.run_adapter('execute')
        with self.assertRaises(PublishError): self.run_adapter('verify')
    def test_lost_final_response_is_probed_without_new_upload(self):
        self.fake.interrupt_after_accept=True
        with self.assertRaises(PublishError): self.run_adapter('execute')
        self.assertEqual(self.run_adapter('verify')['state'],'published')
        self.assertEqual(sum(c[0]=='POST' for c in self.fake.calls),1)
        self.assertEqual(sum(c[0]=='PUT' and bool(c[3]) for c in self.fake.calls),1)
    def test_continue_only_original_session_from_server_offset(self):
        self.fake.interrupt_after_accept=True
        with patch('youtube_adapter.CHUNK',4), self.assertRaises(PublishError): self.run_adapter('execute')
        with self.assertRaises(PublishError): self.run_adapter('verify')
        with patch('youtube_adapter.CHUNK',4):
            self.assertEqual(self.run_adapter('continue')['remote_id'],VIDEO)
        self.assertEqual(sum(c[0]=='POST' for c in self.fake.calls),1)
        requests=[c for c in self.fake.calls if c[0]=='PUT' and c[3]]
        self.assertEqual(requests[1][2]['Content-Range'],'bytes 4-7/9')
        self.assertEqual(self.run_adapter('verify')['state'],'published')
    def test_restricted_account_can_only_read_back_original(self):
        self.run_adapter('execute')
        self.profile['accounts']['owner']['restricted']=True
        self.assertEqual(self.run_adapter('verify')['state'],'published')
        with self.assertRaisesRegex(PublishError,'受限'):
            self.run_adapter('continue')
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)

    def test_wrong_channel_rejected_before_post(self):
        self.fake.channel='UC'+'b'*22
        with self.assertRaises(PublishError): self.run_adapter('execute')
        self.assertFalse(any(c[0]=='POST' for c in self.fake.calls))
        self.assertFalse(checkpoint_path(self.rpath).exists())
    def test_unsupported_fields_are_rejected_before_post(self):
        self.manifest['targets'][0]['platform_metadata']['playlist_name']='A playlist'
        self.mpath.write_text(json.dumps(self.manifest)); self.request['manifest_digest']=digest(self.manifest)
        self.request['target']=self.manifest['targets'][0];self.rpath.write_text(json.dumps(self.request))
        with self.assertRaises(PublishError): self.run_adapter('execute')
        self.assertFalse(any(c[0]=='POST' for c in self.fake.calls))
    def test_producer_mutation_and_restoration_do_not_change_uploaded_bytes(self):
        original=self.fake.call; source=self.root/'movie.mp4'; writes=0
        def mutate_during_transfer(method,url,**kwargs):
            nonlocal writes
            data=kwargs.get('data')
            if method=='PUT' and data:
                writes+=1
                if writes==1: source.write_bytes(b'XXXXXXXXX')
                elif writes==2: source.write_bytes(b'123456789')
            return original(method,url,**kwargs)
        self.fake.call=mutate_during_transfer
        with patch('youtube_adapter.CHUNK',4):
            self.assertEqual(self.run_adapter('execute')['remote_id'],VIDEO)
        uploaded=b''.join(c[3] for c in self.fake.calls if c[0]=='PUT' and c[3])
        self.assertEqual(uploaded,b'123456789')
        self.assertEqual(source.read_bytes(),b'123456789')
        self.assertEqual(self.run_adapter('verify')['state'],'published')

    def test_tampered_private_snapshot_blocks_continuation(self):
        self.fake.interrupt_after_accept=True
        with patch('youtube_adapter.CHUNK',4), self.assertRaises(PublishError): self.run_adapter('execute')
        from youtube_adapter import snapshot_path
        path=snapshot_path(self.rpath); self.assertEqual(path.stat().st_mode & 0o777,0o400)
        path.chmod(0o600); path.write_bytes(b'123456780'); path.chmod(0o400)
        with self.assertRaisesRegex(PublishError,'快照已改变'):
            self.run_adapter('continue')
        self.assertEqual(sum(c[0]=='PUT' and bool(c[3]) for c in self.fake.calls),1)

    def test_file_change_blocks_resume(self):
        self.fake.interrupt_after_accept=True
        with patch('youtube_adapter.CHUNK',4), self.assertRaises(PublishError): self.run_adapter('execute')
        (self.root/'movie.mp4').write_bytes(b'123456780')
        with self.assertRaises(PublishError): self.run_adapter('continue')
        self.assertEqual(sum(c[0]=='POST' for c in self.fake.calls),1)
    def test_preflight_stops_missing_fields_before_remote(self):
        profile_file=self.root/'profile.json'; profile_file.write_text(json.dumps(self.profile))
        argv=[sys.executable, str(ROOT/'scripts/youtube_adapter.py'), 'execute','--profile',str(profile_file)]
        self.assertEqual(preflight_youtube(self.manifest,self.request['target'],self.profile,argv)['local_only'],True)
        self.manifest['targets'][0]['platform_metadata']['playlist_name']='Series'
        with self.assertRaisesRegex(PublishError,'字段'):
            preflight_youtube(self.manifest,self.manifest['targets'][0],self.profile,argv)
        self.assertEqual(self.fake.calls,[])

    def test_preflight_stops_mismatched_profile_and_bad_argv(self):
        other=copy.deepcopy(self.profile)
        other['accounts']['owner']['public_identities']['youtube']='UC'+'b'*22
        profile_file=self.root/'profile.json'; profile_file.write_text(json.dumps(other))
        argv=[sys.executable, str(ROOT/'scripts/youtube_adapter.py'), 'execute','--profile',str(profile_file)]
        with self.assertRaisesRegex(PublishError,'配置'):
            preflight_youtube(self.manifest,self.request['target'],self.profile,argv)
        argv.extend(['--unused','credential'])
        with self.assertRaisesRegex(PublishError,'参数'):
            preflight_youtube(self.manifest,self.request['target'],self.profile,argv)

    def test_preflight_rejects_wrapper_that_can_change_action(self):
        profile_file=self.root/'profile.json'; profile_file.write_text(json.dumps(self.profile))
        argv=['bash','-c','echo',str(ROOT/'scripts/youtube_adapter.py'),'execute','--profile',str(profile_file)]
        with self.assertRaisesRegex(PublishError,'解释器'):
            preflight_youtube(self.manifest,self.request['target'],self.profile,argv,expected_action='execute')

    def test_optional_oauth_refresh_is_ephemeral_and_preflight_offline(self):
        account=self.profile['accounts']['owner']; del account['youtube_token_env']
        account.update(youtube_refresh_token_env='TEST_REFRESH', youtube_client_id_env='TEST_CLIENT_ID',
                       youtube_client_secret_env='TEST_CLIENT_SECRET')
        env={'TEST_REFRESH':'private-refresh', 'TEST_CLIENT_ID':'private-client', 'TEST_CLIENT_SECRET':'private-secret'}
        profile_file=self.root/'profile.json'; profile_file.write_text(json.dumps(self.profile))
        argv=[sys.executable,str(ROOT/'scripts/youtube_adapter.py'),'execute','--profile',str(profile_file)]
        with patch.dict(os.environ,env), patch('youtube_adapter.refresh_access_token',return_value='ephemeral-access') as refresh:
            preflight_youtube(self.manifest,self.request['target'],self.profile,argv,expected_action='execute')
            refresh.assert_not_called()
            self.assertEqual(self.run_adapter('execute')['remote_id'],VIDEO)
            self.assertEqual(self.run_adapter('verify')['state'],'published')
            self.assertEqual(refresh.call_count,2)
        saved=checkpoint_path(self.rpath).read_text()
        for secret in (*env.values(),'ephemeral-access'): self.assertNotIn(secret,saved)

    def test_oauth_refresh_after_interrupted_upload_keeps_same_session(self):
        account=self.profile['accounts']['owner']; del account['youtube_token_env']
        account.update(youtube_refresh_token_env='TEST_REFRESH', youtube_client_id_env='TEST_CLIENT_ID',
                       youtube_client_secret_env='TEST_CLIENT_SECRET')
        env={'TEST_REFRESH':'secret-r', 'TEST_CLIENT_ID':'secret-c', 'TEST_CLIENT_SECRET':'secret-s'}
        self.fake.interrupt_after_accept=True
        with patch.dict(os.environ,env), patch('youtube_adapter.refresh_access_token',side_effect=['access-1','access-2','access-3']) as refresh:
            with patch('youtube_adapter.CHUNK',4), self.assertRaises(PublishError): self.run_adapter('execute')
            with patch('youtube_adapter.CHUNK',4): self.assertEqual(self.run_adapter('continue')['remote_id'],VIDEO)
            self.assertEqual(self.run_adapter('verify')['state'],'published')
            self.assertEqual(refresh.call_count,3)
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)
        saved=checkpoint_path(self.rpath).read_text()
        for secret in (*env.values(),'access-1','access-2','access-3'): self.assertNotIn(secret,saved)

    def test_oauth_refresh_settings_are_complete_exclusive_and_non_network_for_restricted(self):
        account=self.profile['accounts']['owner']
        account['youtube_refresh_token_env']='TEST_REFRESH'
        with patch.dict(os.environ,{'TEST_REFRESH':'hidden'}), patch('youtube_adapter.refresh_access_token') as refresh:
            with self.assertRaisesRegex(PublishError,'不能混用'): self.run_adapter('execute')
            refresh.assert_not_called()
        del account['youtube_token_env']
        with patch.dict(os.environ,{'TEST_REFRESH':'hidden'}), patch('youtube_adapter.refresh_access_token') as refresh:
            with self.assertRaisesRegex(PublishError,'完整'): self.run_adapter('execute')
            refresh.assert_not_called()
        account.update(youtube_client_id_env='TEST_CLIENT_ID',youtube_client_secret_env='TEST_CLIENT_SECRET',restricted=True)
        with patch.dict(os.environ,{'TEST_REFRESH':'hidden','TEST_CLIENT_ID':'client','TEST_CLIENT_SECRET':'secret'}), patch('youtube_adapter.refresh_access_token') as refresh:
            with self.assertRaisesRegex(PublishError,'受限'): self.run_adapter('execute')
            refresh.assert_not_called()

    def test_official_refresh_rejects_malformed_or_failed_response_without_leaking(self):
        from youtube_adapter import refresh_access_token
        from unittest.mock import MagicMock
        response=MagicMock(); response.status=200
        response.__enter__.return_value=response
        opener=MagicMock(); opener.open.return_value=response
        response.read.return_value=b'{"access_token":"ephemeral-access", "token_type":"Bearer"}'
        with patch('youtube_adapter.urllib.request.build_opener',return_value=opener):
            self.assertEqual(refresh_access_token('private-refresh','private-client','private-secret'),'ephemeral-access')
            request=opener.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://oauth2.googleapis.com/token')
            response.read.return_value=b'{"access_token":"private-secret", "token_type":3}'
            with self.assertRaises(PublishError) as exc: refresh_access_token('private-refresh','private-client','private-secret')
            self.assertNotIn('private-secret',str(exc.exception))
            opener.open.side_effect=OSError('private-refresh')
            with self.assertRaises(PublishError) as exc: refresh_access_token('private-refresh','private-client','private-secret')
            self.assertNotIn('private-refresh',str(exc.exception))

    def test_preflight_action_must_match_dispatch_stage(self):
        profile_file=self.root/'profile.json'; profile_file.write_text(json.dumps(self.profile))
        for actual in ('execute','verify','continue'):
            argv=[sys.executable,str(ROOT/'scripts/youtube_adapter.py'),actual,'--profile',str(profile_file)]
            self.assertTrue(preflight_youtube(self.manifest,self.request['target'],self.profile,argv,expected_action=actual)['local_only'])
            for expected in {'execute','verify','continue'}-{actual}:
                with self.subTest(actual=actual, expected=expected), self.assertRaisesRegex(PublishError,'不一致'):
                    preflight_youtube(self.manifest,self.request['target'],self.profile,argv,expected_action=expected)

    def test_insecure_or_tampered_checkpoint_is_never_continued(self):
        self.fake.interrupt_after_accept=True
        with patch('youtube_adapter.CHUNK',4), self.assertRaises(PublishError): self.run_adapter('execute')
        checkpoint=checkpoint_path(self.rpath)
        checkpoint.chmod(0o644)
        with self.assertRaisesRegex(PublishError,'权限'): self.run_adapter('continue')
        checkpoint.chmod(0o600)
        obj=json.loads(checkpoint.read_text()); obj['session_url']='https://evil.example/upload/youtube/v3/videos?id=x'
        checkpoint.write_text(json.dumps(obj))
        with self.assertRaisesRegex(PublishError,'不可信'): self.run_adapter('continue')
        self.assertEqual(sum(c[0]=='POST' for c in self.fake.calls),1)
        self.assertEqual(sum(c[0]=='PUT' and bool(c[3]) for c in self.fake.calls),1)

    def test_range_anomalies_are_not_used_to_resume(self):
        self.fake.interrupt_after_accept=True
        with patch('youtube_adapter.CHUNK',4), self.assertRaises(PublishError): self.run_adapter('execute')
        orig=self.fake.call
        def invalid_range(method,url,**kwargs):
            if method=='PUT' and kwargs.get('headers',{}).get('Content-Range','').startswith('bytes */'):
                return 308,{'Range':'bytes=1-8'},b''
            return orig(method,url,**kwargs)
        self.fake.call=invalid_range
        with self.assertRaisesRegex(PublishError,'偏移'): self.run_adapter('continue')
        self.assertEqual(sum(c[0]=='POST' for c in self.fake.calls),1)
        self.assertEqual(sum(c[0]=='PUT' and bool(c[3]) for c in self.fake.calls),1)

    def test_session_validation_and_unknown_id_binding(self):
        for url in ('https://evil.example/upload/youtube/v3/videos?id=x',
                    'http://www.googleapis.com/upload/youtube/v3/videos?id=x',
                    'https://www.googleapis.com@evil.example/upload/youtube/v3/videos?id=x'):
            with self.assertRaises(PublishError): session_url(url)
        self.run_adapter('execute')
        self.request['job']['remote_id']='aaaaaaaaaaa'; self.rpath.write_text(json.dumps(self.request))
        with self.assertRaises(PublishError): self.run_adapter('verify')

if __name__=='__main__': unittest.main()
