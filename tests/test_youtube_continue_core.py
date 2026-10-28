"""Core -> real YouTube adapter using a fake official API, never online."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from publish_core import PublishError, Store, run, validate
from youtube_adapter import dispatch
from test_youtube_adapter import FakeAPI, CHANNEL, VIDEO

class YoutubeContinuationCoreTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'movie.mp4').write_bytes(b'123456789')
        self.manifest={'schema_version':1,'job_id':'job','producer':'test','content_status':'final',
          'source_root':str(self.root),'documents':[],'media_slots':[],
          'assets':[{'id':'v','path':'movie.mp4','kind':'video'}],
          'targets':[{'id':'t','platform':'youtube','account':'owner','endpoint':'publish',
                      'content_type':'video','title':'A title','description':'A description',
                      'asset_ids':['v'],'platform_metadata':{
                      'made_for_kids':False,'visibility':'public','altered_content':True}}]}
        validate(self.manifest,sealing=True)
        self.path=self.root/'manifest.json'; self.path.write_text(json.dumps(self.manifest))
        self.profile_file=self.root/'profile.json'; adapter=str(ROOT/'scripts/youtube_adapter.py')
        commands={action:[sys.executable,adapter,action,'--profile',str(self.profile_file)]
                  for action in ('execute','verify','continue')}
        self.profile={'accounts':{'owner':{'platforms':['youtube'],
            'public_identities':{'youtube':CHANNEL},'youtube_token_env':'TEST_YOUTUBE_TOKEN'}},
            'adapters':{'youtube':{'enabled_capabilities':['video:publish'],**commands}}}
        self.profile_file.write_text(json.dumps(self.profile))
        self.store=Store(self.root/'state'); self.addCleanup(self.store.close)
        self.key=self.store.authorize(self.manifest,'t','本条真实授权样例')['key']
        self.env=patch.dict(os.environ,{'TEST_YOUTUBE_TOKEN':'synthetic-token'}); self.env.start(); self.addCleanup(self.env.stop)
        self.fake=FakeAPI('synthetic-token'); self.fake.interrupt_after_accept=True
        self.actions=[]; self.before_verify=[]
        def real_invoke(config,action,request_path,**kwargs):
            self.actions.append(action)
            if action=='verify': self.before_verify.append(self.store.status(self.key)['remote_id'])
            return dispatch(request_path,action,self.profile,lambda _:self.fake)
        self.invocation=patch('publish_core.invoke',side_effect=real_invoke); self.invocation.start(); self.addCleanup(self.invocation.stop)
        self.chunks=patch('youtube_adapter.CHUNK',4); self.chunks.start(); self.addCleanup(self.chunks.stop)
    def interrupted(self):
        with self.assertRaises(PublishError): run(self.path,'t',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['state'],'submission_unknown')
        self.assertEqual(self.fake.offset,4)
    def cont(self): return run(self.path,'t',self.profile,self.store,resume=True,youtube_continue=True)
    def test_only_explicit_continue_uploads_and_verifies_original(self):
        self.interrupted()
        with self.assertRaises(PublishError): run(self.path,'t',self.profile,self.store,resume=True)
        self.assertEqual(self.fake.offset,4)
        result=self.cont()
        self.assertEqual(result['state'],'published'); self.assertEqual(result['remote_id'],VIDEO)
        self.assertEqual(self.before_verify[-1],VIDEO)
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)
        data=[x for x in self.fake.calls if x[0]=='PUT' and x[3]]
        self.assertEqual(data[1][2]['Content-Range'],'bytes 4-7/9')
    def test_public_cli_continues_original_session_without_new_post(self):
        import publish
        self.interrupted()
        args=['publish.py','--profile',str(self.profile_file),'--state-dir',str(self.store.directory),
              'continue-youtube',self.key]
        output=io.StringIO()
        with patch.object(sys,'argv',args), patch('sys.stdout',output):
            self.assertEqual(publish.main(),0,output.getvalue())
        result=json.loads(output.getvalue())
        self.assertEqual(result['result']['state'],'published')
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)
        self.assertIn('continue',self.actions)

    def test_missing_grant_refuses_continuation_without_upload(self):
        self.interrupted()
        self.store.db.execute('DELETE FROM grants'); self.store.db.commit()
        with self.assertRaises(PublishError): self.cont()
        self.assertEqual(self.fake.offset,4)
    def test_another_jobs_account_lock_refuses_continuation(self):
        self.interrupted()
        self.store.db.execute('UPDATE account_locks SET job_key=? WHERE job_key=?',('other-job',self.key)); self.store.db.commit()
        with self.assertRaises(PublishError): self.cont()
        self.assertEqual(self.fake.offset,4)
    def test_verify_failure_retains_original_id_and_never_reuploads(self):
        self.interrupted(); self.fake.mismatch=True
        with self.assertRaises(PublishError): self.cont()
        row=self.store.status(self.key)
        self.assertEqual(row['remote_id'],VIDEO)
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)
        self.fake.mismatch=False
        self.assertEqual(run(self.path,'t',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)
    def test_secret_session_and_token_never_enter_ledger(self):
        self.interrupted()
        self.cont()
        data='\n'.join(self.store.db.iterdump())
        self.assertNotIn('upload_id', data)
        self.assertNotIn('synthetic-token', data)
        self.assertNotIn('googleapis.com/upload', data)

    def test_wrong_platform_rejected(self):
        self.interrupted()
        self.store.db.execute('UPDATE jobs SET platform=? WHERE key=?',('weibo',self.key)); self.store.db.commit()
        with self.assertRaises(PublishError): self.cont()
        self.assertEqual(self.fake.offset,4)

if __name__=='__main__': unittest.main()
