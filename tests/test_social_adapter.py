import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import PublishError,digest,write_json,read_json
from platform_pack import build_pack
from social_adapter import dispatch,validate_observation,pinned_prefill_command,work_path,BAOYU_COMMIT,preflight_social
from test_platform_pack import fixture


class SocialAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.m=fixture(self.root);self.manifest=self.root/'manifest.json';write_json(self.manifest,self.m)
        self.request=self.root/'request.json';write_json(self.request,dict(manifest_path=str(self.manifest),manifest_digest=digest(self.m),target=self.m['targets'][0],job={}))
        self.profile={'accounts':{'owner':{'platforms':['x'],'public_identities':{'x':'@owner'}}},'browser_profile':str((self.root/'dedicated').resolve())}
        self.pack=build_pack(self.m,'t',self.profile)
        self.observation=dict(state='pending',manifest_digest=digest(self.m),target_id='t',platform='x',account='owner',observed_account_identity='@owner',remote_id='real-shaped-test-only')

    def verified(self):
        return dict(self.observation,state='prefill_ready',content_verified=True,verified_pack_digest=digest(self.pack),verified_asset_ids=['image'])

    def bridge(self):
        repo=self.root/'repo';script=repo/'skills/baoyu-post-to-x/scripts/x-browser.ts';script.parent.mkdir(parents=True,exist_ok=True);script.write_text('// fixture')
        runtime=self.root/'bun';runtime.write_text('#!/bin/sh\nexit 0\n');runtime.chmod(0o700)
        self.profile['adapters']={'x':{'upstream_prefill':{'provider':'baoyu-x','repository':str(repo),'runtime':str(runtime)}}}
        return repo

    def git_result(self,repo):
        return [subprocess.CompletedProcess([],0,str(repo)+'\n',''),subprocess.CompletedProcess([],0,BAOYU_COMMIT+'\n',''),subprocess.CompletedProcess([],0,'','')]

    def test_preflight_checks_profile_binding_before_platform_work(self):
        path=self.root/'profile.json';write_json(path,self.profile)
        argv=['python3','social_adapter.py','execute','--profile',str(path)]
        with patch('social_adapter.agent_adapter.dispatch') as mailbox:
            self.assertTrue(preflight_social(self.m,self.m['targets'][0],self.profile,argv)['local_only'])
            mailbox.assert_not_called()
        changed=copy.deepcopy(self.profile);changed['accounts']['owner']['public_identities']['x']='@other';write_json(path,changed)
        with self.assertRaisesRegex(PublishError,'不一致'):preflight_social(self.m,self.m['targets'][0],self.profile,argv)
        with self.assertRaisesRegex(PublishError,'需要 --profile'):preflight_social(self.m,self.m['targets'][0],self.profile,argv[:3])
        empty=copy.deepcopy(self.profile);empty['accounts']['owner']['public_identities']={}
        with self.assertRaisesRegex(PublishError,'公开账号身份'):preflight_social(self.m,self.m['targets'][0],empty,argv)

    def test_agent_path_needs_real_identity_and_full_readback(self):
        with patch('social_adapter.agent_adapter.dispatch',return_value=self.observation) as mailbox:
            self.assertEqual(dispatch(self.request,'execute',self.profile)['state'],'pending')
            self.assertEqual(mailbox.call_count,1)
        with patch('social_adapter.agent_adapter.dispatch',return_value=self.verified()):
            self.assertEqual(dispatch(self.request,'verify',self.profile)['state'],'prefill_ready')
        for change in ({'observed_account_identity':'@wrong'},{'verified_asset_ids':[]},{'verified_pack_digest':'wrong'},{'state':'published'},{'content_verified':False},{'remote_id':None}):
            with self.subTest(change=change),self.assertRaises(PublishError):validate_observation({**self.verified(),**change},self.pack,'@owner','verify')

    def test_restricted_missing_identity_and_tampered_snapshot_fail_before_mailbox(self):
        for change in ({'restricted':True},{'public_identities':{}}):
            p=copy.deepcopy(self.profile);p['accounts']['owner'].update(change)
            with patch('social_adapter.agent_adapter.dispatch') as mailbox,self.assertRaises(PublishError):dispatch(self.request,'execute',p)
            mailbox.assert_not_called()
        request=read_json(self.request);request['target']={**request['target'],'account':'other'};write_json(self.request,request)
        with self.assertRaises(PublishError):dispatch(self.request,'execute',self.profile)

    def test_bridge_never_accepts_publish_or_content_flags(self):
        repo=self.bridge()
        for key,value in [('endpoint','publish'),('endpoint','draft')]:
            p={**self.pack,key:value}
            with patch('social_adapter.subprocess.run',side_effect=self.git_result(repo)),self.assertRaises(PublishError):pinned_prefill_command(p,self.profile)
        for body in ('--submit','-x',' trailing '):
            p={**self.pack,'fields':{**self.pack['fields'],'composed_text':body}}
            with patch('social_adapter.subprocess.run',side_effect=self.git_result(repo)),self.assertRaises(PublishError):pinned_prefill_command(p,self.profile)

    def test_bridge_version_lock_and_parameter_array(self):
        repo=self.bridge()
        with patch('social_adapter.subprocess.run',side_effect=self.git_result(repo)):
            command=pinned_prefill_command(self.pack,self.profile)
        self.assertNotIn('--submit',command);self.assertIn('--profile',command);self.assertIn('--image',command)
        for wrong in ('badcommit','dirty'):
            results=self.git_result(repo);results[1 if wrong=='badcommit' else 2].stdout=wrong
            with patch('social_adapter.subprocess.run',side_effect=results),self.assertRaises(PublishError):pinned_prefill_command(self.pack,self.profile)

    def test_bridge_requires_fresh_account_confirmation_then_does_not_claim_completion(self):
        repo=self.bridge();ready=dict(self.observation,upstream_prefill_ready=True,browser_mode_authorized=True,observed_browser_profile=self.profile['browser_profile'])
        with (self.root/'lock').open('w') as lock,patch.dict(os.environ,{'SANDY_PUBLISH_LOCK_FD':str(lock.fileno())}),patch('social_adapter.agent_adapter.dispatch',side_effect=[ready,self.observation]) as mailbox,patch('social_adapter.subprocess.run',side_effect=self.git_result(repo)+[subprocess.CompletedProcess([],0,b'',b'')]) as proc:
            result=dispatch(self.request,'execute',self.profile)
            self.assertEqual(result['state'],'pending');self.assertEqual(mailbox.call_count,2)
            self.assertEqual(proc.call_args.kwargs['pass_fds'],(lock.fileno(),))
            self.assertNotIn('--submit',proc.call_args.args[0])
        with self.assertRaisesRegex(PublishError,'已尝试'):dispatch(self.request,'execute',self.profile)
        # Resume only observes; never calls upstream again, even if a bridge exists.
        with patch('social_adapter.agent_adapter.dispatch',return_value=self.verified()),patch('social_adapter.subprocess.run') as proc:
            self.assertEqual(dispatch(self.request,'verify',self.profile)['state'],'prefill_ready');proc.assert_not_called()

    def test_helper_timeout_keeps_unknown_attempt_and_forbids_retry(self):
        repo=self.bridge(); ready=dict(self.observation,upstream_prefill_ready=True,browser_mode_authorized=True,observed_browser_profile=self.profile['browser_profile'])
        with (self.root/'lock').open('w') as lock,patch.dict(os.environ,{'SANDY_PUBLISH_LOCK_FD':str(lock.fileno())}),patch('social_adapter.agent_adapter.dispatch',return_value=ready),patch('social_adapter.subprocess.run',side_effect=self.git_result(repo)+[subprocess.TimeoutExpired(['bun'],1)]),self.assertRaisesRegex(PublishError,'未知'):
            dispatch(self.request,'execute',self.profile)
        self.assertTrue(read_json(work_path(self.request))['upstream_started'])
        with patch('social_adapter.subprocess.run') as proc,self.assertRaisesRegex(PublishError,'已尝试'):
            dispatch(self.request,'execute',self.profile)
        proc.assert_not_called()

    def test_disabled_writes_do_not_prevent_readonly_recovery(self):
        with patch('social_adapter.agent_adapter.dispatch',return_value=self.observation):
            dispatch(self.request,'execute',self.profile)
        self.profile['accounts']['owner']['restricted']=True
        with patch('social_adapter.agent_adapter.dispatch',return_value=self.verified()):
            self.assertEqual(dispatch(self.request,'verify',self.profile)['state'],'prefill_ready')

    def test_resume_refuses_changed_identity_profile_or_missing_original_binding(self):
        with patch('social_adapter.agent_adapter.dispatch') as mailbox,self.assertRaisesRegex(PublishError,'缺少首次'):
            dispatch(self.request,'verify',self.profile)
        mailbox.assert_not_called()
        with patch('social_adapter.agent_adapter.dispatch',return_value=self.observation):
            dispatch(self.request,'execute',self.profile)
        original=read_json(work_path(self.request))
        for mode in ('identity','browser','missing-binding'):
            p=copy.deepcopy(self.profile)
            if mode=='identity':p['accounts']['owner']['public_identities']['x']='@new'
            elif mode=='browser':p['browser_profile']=str(self.root/'new-profile')
            else:
                record=copy.deepcopy(original);record.pop('expected_account_identity');write_json(work_path(self.request),record)
            with patch('social_adapter.agent_adapter.dispatch') as mailbox,self.assertRaisesRegex(PublishError,'禁止切换账号'):
                dispatch(self.request,'verify',p)
            mailbox.assert_not_called()
            if mode!='missing-binding':self.assertEqual(read_json(work_path(self.request)),original)
            write_json(work_path(self.request),original)

    def test_bridge_refuses_unapproved_browser_mode_before_running(self):
        repo=self.bridge()
        with patch('social_adapter.agent_adapter.dispatch',return_value=self.observation),patch('social_adapter.subprocess.run',side_effect=self.git_result(repo)) as proc,self.assertRaisesRegex(PublishError,'未获得'):
            dispatch(self.request,'execute',self.profile)
        self.assertEqual(proc.call_count,3) # Git reads only.
        self.assertFalse(read_json(work_path(self.request)).get('upstream_started',False))

if __name__=='__main__':unittest.main()
