import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from agent_adapter import dispatch, inspect, reply, paths, check_reply
from publish_core import PublishError, digest, file_hash, write_json


class AgentAdapterTests(unittest.TestCase):
    def fixture(self,root):
        manifest=root/'manifest.json'; write_json(manifest,{'fixture':True})
        request=root/'request.json'
        write_json(request,dict(manifest_path=str(manifest),manifest_digest=digest({'fixture':True}),
                   target=dict(id='x1',platform='x',account='test',endpoint='publish'),job={}))
        return request

    def envelope(self,root,task):
        evidence=root/'observation.txt'; evidence.write_text('Offline mailbox fixture; not platform evidence.')
        return dict(nonce=task['nonce'],observed_phase=task['nonce'],
                    evidence=[dict(path=str(evidence),sha256=file_hash(evidence))],
                    observation=dict(state='pending',manifest_digest=task['request']['manifest_digest'],
                                     target_id='x1',platform='x',account='test',remote_id='fixture-only'))

    def test_two_phase_response_and_closed_session(self):
        with tempfile.TemporaryDirectory() as folder, concurrent.futures.ThreadPoolExecutor() as pool:
            root=Path(folder); request=self.fixture(root)
            first_nonce=None
            for action in ('execute','verify'):
                future=pool.submit(dispatch,request,action,timeout=3)
                for _ in range(100):
                    try:
                        task=inspect(request)
                        if task['action']==action: break
                    except (PublishError,FileNotFoundError): pass
                    time.sleep(.01)
                else: self.fail('mailbox did not open')
                env=self.envelope(root,task)
                if first_nonce:
                    stale=dict(env,nonce=first_nonce)
                    with self.assertRaisesRegex(PublishError,'旧接力'): reply(request,stale)
                    env['observation'].update(state='published',content_verified=True,remote_url='https://example.invalid/fixture')
                first_nonce=task['nonce']
                reply(request,env)
                self.assertEqual(future.result()['state'],env['observation']['state'])
                with self.assertRaisesRegex(PublishError,'已结束'): inspect(request)

    def test_no_fabricated_success_or_empty_or_changed_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); request=self.fixture(root)
            task=dict(nonce='phase',action='execute',request=json.loads(request.read_text()))
            env=self.envelope(root,task)
            for change in (dict(state='published'),dict(account='wrong')):
                changed=dict(env,observation={**env['observation'],**change})
                with self.assertRaises(PublishError): check_reply(task,changed)
            with self.assertRaises(PublishError): check_reply(task,dict(env,evidence=[]))
            (root/'observation.txt').write_text('')
            with self.assertRaises(PublishError): check_reply(task,env)

    def test_timeout_and_stale_lease(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); request=self.fixture(root)
            with self.assertRaisesRegex(PublishError,'超时'): dispatch(request,'verify',timeout=.01)
            _,task_path,_,_=paths(request)
            task=json.loads(task_path.read_text()); task['status']='active'; write_json(task_path,task)
            with self.assertRaisesRegex(PublishError,'已退出'): inspect(request)


if __name__=='__main__': unittest.main()
