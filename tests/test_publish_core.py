import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError, Store, digest, marker, run, validate, write_json


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'article.html').write_text('<h1>标题</h1><p>前文</p><p>'+marker('theme')+'</p><p>后文</p>')
        (self.root/'theme.mp3').write_bytes(b'test-audio-source')
        self.manifest = {
            'schema_version': 1, 'job_id': 'test-job', 'producer': 'test', 'content_status': 'final', 'source_root': str(self.root),
            'documents': [{'id': 'article', 'path': 'article.html', 'format': 'html'}],
            'assets': [{'id': 'theme', 'path': 'theme.mp3', 'kind': 'audio'}],
            'media_slots': [{'id': 'theme', 'document_id': 'article', 'asset_id': 'theme'}],
            'targets': [{'id': 'wechat', 'platform': 'wechat', 'account': 'main', 'content_type': 'article', 'endpoint': 'draft', 'title': '测试文章', 'document_id': 'article'}],
        }
        validate(self.manifest, sealing=True)
        self.store = Store(self.root/'state')
        self.key = self.store.prepare(self.manifest, 'wechat')['key']

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def authorize(self):
        self.store.authorize(self.manifest, 'wechat', '测试夹具：仅创建测试草稿')

    def observation(self, **updates):
        value = {'manifest_digest': digest(self.manifest), 'target_id': 'wechat', 'platform': 'wechat', 'account': 'main', 'state': 'draft_ready', 'remote_id': 'draft-1', 'content_verified': True}
        value.update(updates)
        return value

    def test_valid_and_empty_manifest(self):
        self.assertEqual(validate(self.manifest)['slots'], 1)
        with self.assertRaises(PublishError): validate({})

    def test_source_bytes_changed(self):
        (self.root/'theme.mp3').write_bytes(b'replacement')
        with self.assertRaisesRegex(PublishError, '已改变'): validate(self.manifest)

    def test_slot_missing_and_duplicate(self):
        for text in ('no marker', marker('theme')*2):
            (self.root/'article.html').write_text(text)
            with self.assertRaisesRegex(PublishError, '唯一匹配'): validate(self.manifest, sealing=True)

    def test_context_changed_after_hash_updated(self):
        m = copy.deepcopy(self.manifest)
        (self.root/'article.html').write_text('changed'+marker('theme'))
        validate(m, sealing=True)
        m['media_slots'][0]['context'] = self.manifest['media_slots'][0]['context']
        with self.assertRaisesRegex(PublishError, '上下文'): validate(m)

    def test_undeclared_marker_and_symlink_escape(self):
        (self.root/'article.html').write_text(marker('theme')+marker('missing'))
        with self.assertRaisesRegex(PublishError, '未登记'): validate(self.manifest, sealing=True)
        (self.root/'article.html').unlink()
        (self.root/'article.html').symlink_to(__file__)
        with self.assertRaisesRegex(PublishError, '越界'): validate(self.manifest, sealing=True)

    def test_no_authorization_no_attempt(self):
        with self.assertRaisesRegex(PublishError, '授权'): self.store.begin(self.manifest, 'wechat')
        self.assertEqual(self.store.status(self.key)['state'], 'prepared')

    def test_endpoint_change_invalidates_grant(self):
        self.authorize()
        m = copy.deepcopy(self.manifest); m['targets'][0]['endpoint'] = 'publish'
        self.store.prepare(m, 'wechat')
        with self.assertRaisesRegex(PublishError, '授权'): self.store.begin(m, 'wechat')

    def test_crash_lock_survives_reopen(self):
        self.authorize(); self.store.begin(self.manifest, 'wechat')
        self.store.close(); self.store = Store(self.root/'state')
        with self.assertRaisesRegex(PublishError, '不重复提交'): self.store.begin(self.manifest, 'wechat')
        m = copy.deepcopy(self.manifest); m['targets'][0]['title'] = 'second-job'
        self.store.authorize(m, 'wechat', '测试夹具')
        with self.assertRaisesRegex(PublishError, '账号'): self.store.begin(m, 'wechat')

    def test_wrong_account_and_endpoint_refused(self):
        self.authorize(); self.store.begin(self.manifest, 'wechat')
        for bad in (self.observation(account='other'), self.observation(state='published', remote_url='https://example.com/post')):
            with self.assertRaises(PublishError): self.store.save_observation(self.key, bad, verified=True)
        self.assertEqual(self.store.status(self.key)['state'], 'submission_unknown')

    def test_response_is_not_readback(self):
        self.authorize(); self.store.begin(self.manifest, 'wechat')
        with self.assertRaises(PublishError): self.store.save_observation(self.key, self.observation(), verified=False)
        self.store.save_observation(self.key, self.observation(state='pending'), verified=False)
        self.assertEqual(self.store.status(self.key)['state'], 'pending')
        self.store.save_observation(self.key, self.observation(), verified=True)
        self.assertEqual(self.store.status(self.key)['state'], 'draft_ready')

    def test_chrome_editor_is_not_a_platform_draft(self):
        self.authorize(); self.store.begin(self.manifest, 'wechat')
        for scope in (None, 'chrome_session_only'):
            value=self.observation(remote_id='browser-editor:RealTarget')
            if scope:value['tracking_scope']=scope
            with self.assertRaises(PublishError):self.store.save_observation(self.key,value,verified=True)
        self.assertEqual(self.store.status(self.key)['state'],'submission_unknown')

    def test_remote_id_cannot_change(self):
        self.authorize(); self.store.begin(self.manifest, 'wechat')
        self.store.save_observation(self.key, self.observation(state='pending'), verified=False)
        with self.assertRaisesRegex(PublishError, 'ID'): self.store.save_observation(self.key, self.observation(remote_id='draft-other'), verified=True)

    def test_absence_requires_evidence(self):
        self.authorize(); self.store.begin(self.manifest, 'wechat')
        with self.assertRaises(PublishError): self.store.save_observation(self.key, self.observation(state='not_submitted', remote_id=None), verified=True)
        self.store.save_observation(self.key, self.observation(state='not_submitted', remote_id=None, absence_verified=True), verified=True)
        self.assertEqual(self.store.status(self.key)['state'], 'prepared')

    def test_xiaohongshu_cannot_autopublish(self):
        m = copy.deepcopy(self.manifest)
        m['targets'][0].update(platform='xiaohongshu', content_type='images', endpoint='publish')
        with self.assertRaisesRegex(PublishError, '作者'): validate(m)

    def test_native_media_continuation_keeps_identity_and_rechecks_state(self):
        self.authorize(); self.store.begin(self.manifest,'wechat')
        self.store.save_observation(self.key,self.observation(state='pending'),verified=True)
        path=self.root/'manifest.json'; write_json(path,self.manifest)
        profile={'accounts':{'main':{'platforms':['wechat']}},'adapters':{'wechat':{
                 'enabled_capabilities':['article:draft'],'execute':['unused'],'verify':['unused'],'continue':['unused']}}}
        calls=[]
        def invoke(config,action,request_path,**kwargs):
            calls.append(action)
            self.assertEqual(json.loads(Path(request_path).read_text())['job']['remote_id'],'draft-1')
            if len(calls)==1: return self.observation(state='pending',stage='native_media_insertion_required')
            if action=='continue': return self.observation(state='pending')
            return self.observation()
        with patch('publish_core.invoke',side_effect=invoke):
            self.assertEqual(run(path,'wechat',profile,self.store,advance=True)['state'],'draft_ready')
        self.assertEqual(calls,['verify','continue','verify'])

    def test_continuation_cannot_resend_unknown_or_unready_job(self):
        self.authorize()
        with self.assertRaisesRegex(PublishError,'pending'): self.store.begin(self.manifest,'wechat',continuing=True)
        self.store.begin(self.manifest,'wechat')
        with self.assertRaisesRegex(PublishError,'pending'): self.store.begin(self.manifest,'wechat',continuing=True)

    def profile(self, script):
        return {'accounts': {'main': {'platforms': ['wechat']}}, 'adapters': {'wechat': {
            'enabled_capabilities': ['article:draft'], 'execute': [sys.executable, str(script), 'execute'], 'verify': [sys.executable, str(script), 'verify']}}}

    def test_subprocess_dispatch_idempotency(self):
        # Fixture is outside all active Skill scan trees.
        script = self.root/'adapter.py'
        script.write_text('''import json,sys
from pathlib import Path
r=json.loads(Path(sys.argv[-1]).read_text())
p=Path(__file__).with_name('executions.txt')
if sys.argv[1]=='execute': p.write_text(p.read_text()+'1' if p.exists() else '1')
t=r['target']
print(json.dumps({'manifest_digest':r['manifest_digest'],'target_id':t['id'],'platform':t['platform'],'account':t['account'],'remote_id':'draft-1','state':'draft_ready','content_verified':True}))
''')
        path = self.root/'manifest.json'; write_json(path, self.manifest)
        self.authorize()
        for _ in range(2):
            self.assertEqual(run(path, 'wechat', self.profile(script), self.store)['state'], 'draft_ready')
        self.assertEqual((self.root/'executions.txt').read_text(), '1')

    def test_adapter_crash_does_not_resend(self):
        script = self.root/'crash.py'; script.write_text('import sys; sys.exit(5)')
        path = self.root/'manifest.json'; write_json(path, self.manifest)
        self.authorize()
        with self.assertRaisesRegex(PublishError, 'exit=5'): run(path, 'wechat', self.profile(script), self.store)
        with self.assertRaisesRegex(PublishError, '不重复提交'): run(path, 'wechat', self.profile(script), self.store)

    def test_explicit_resume_rechecks_completed_draft_without_execution_or_sources(self):
        script = self.root/'readonly_adapter.py'
        script.write_text('''import json,sys
from pathlib import Path
r=json.loads(Path(sys.argv[-1]).read_text()); t=r['target']
p=Path(__file__).with_name('actions.json')
calls=json.loads(p.read_text()) if p.exists() else []
calls.append(sys.argv[1]); p.write_text(json.dumps(calls))
state=Path(__file__).with_name('remote-state.txt').read_text()
print(json.dumps(dict(manifest_digest=r['manifest_digest'],target_id=t['id'],platform=t['platform'],account=t['account'],remote_id='draft-1',state=state,content_verified=state=='draft_ready')))
''')
        remote = self.root/'remote-state.txt'; remote.write_text('draft_ready')
        path = self.root/'manifest.json'; write_json(path,self.manifest)
        self.authorize(); profile=self.profile(script)
        self.assertEqual(run(path,'wechat',profile,self.store)['state'],'draft_ready')
        self.assertEqual(run(path,'wechat',profile,self.store)['state'],'draft_ready')
        self.assertEqual(json.loads((self.root/'actions.json').read_text()),['execute','verify'])
        # The platform has changed since success; explicit resume must observe it.
        remote.write_text('pending')
        (self.root/'article.html').unlink(); (self.root/'theme.mp3').unlink()
        profile['accounts']['main']['restricted']=True
        result=run(path,'wechat',profile,self.store,resume=True)
        self.assertEqual(result['state'],'pending')
        self.assertEqual(result['remote_id'],'draft-1')
        self.assertEqual(json.loads((self.root/'actions.json').read_text()),['execute','verify','verify'])

    def test_failed_remote_read_does_not_return_old_completed_status(self):
        self.authorize(); self.store.begin(self.manifest,'wechat')
        self.store.save_observation(self.key,self.observation(),verified=True)
        script=self.root/'missing_draft.py'
        script.write_text('''import sys
from pathlib import Path
assert sys.argv[1]=='verify', 'readback must never execute again'
Path(__file__).with_name('queried.txt').write_text('queried once')
print('remote draft unavailable'); raise SystemExit(2)
''')
        path=self.root/'manifest.json'; write_json(path,self.manifest)
        with self.assertRaisesRegex(PublishError,'exit=2'):
            run(path,'wechat',self.profile(script),self.store,resume=True)
        self.assertEqual((self.root/'queried.txt').read_text(),'queried once')
        # Keep the historical observation, but never return it as a fresh success.
        self.assertEqual(self.store.status(self.key)['remote_id'],'draft-1')

    def test_adapter_timeout_stops_grandchild_even_if_it_ignores_term(self):
        from publish_core import invoke
        child = self.root/'late_child.py'
        child.write_text('import signal,time\nfrom pathlib import Path\n'
            'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
            'Path(__file__).with_suffix(".started").touch()\n'
            'time.sleep(1)\nPath(__file__).with_suffix(".late").touch()\n')
        parent = self.root/'executor.py'
        parent.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
            'subprocess.Popen([sys.executable,str(Path(__file__).with_name("late_child.py"))])\n'
            'time.sleep(20)\n')
        with (self.root/'held.lock').open('w') as lock:
            with self.assertRaisesRegex(PublishError,'必须先查询'):
                invoke({'execute':[sys.executable,str(parent)], 'timeout_seconds':.4},'execute',
                    self.root/'request.json',lock_fd=lock.fileno())
        self.assertTrue(child.with_suffix('.started').is_file())
        time.sleep(1)
        self.assertFalse(child.with_suffix('.late').exists())

    def test_other_target_and_producer_do_not_resend(self):
        m = copy.deepcopy(self.manifest)
        m['producer'] = 'another-producer'
        m['targets'].append({'id':'x','platform':'x','account':'social','endpoint':'publish','content_type':'text','text':'hi'})
        self.assertEqual(self.store.prepare(m, 'wechat')['key'], self.key)
        self.authorize()
        self.store.prepare(m, 'wechat')
        with self.assertRaisesRegex(PublishError, '授权'):
            self.store.begin(m, 'wechat')

    def test_asset_collection_order_not_image_order(self):
        (self.root/'cover.png').write_bytes(b'fixture')
        m = copy.deepcopy(self.manifest)
        m['assets'].append(dict(id='cover',path='cover.png',kind='image'))
        m['targets'][0]['asset_ids'] = ['theme','cover']
        validate(m,sealing=True)
        first = self.store.key(m,m['targets'][0])
        m['assets'].reverse()
        self.assertEqual(first,self.store.key(m,m['targets'][0]))
        m['targets'][0]['asset_ids'].reverse()
        self.assertNotEqual(first,self.store.key(m,m['targets'][0]))

    def test_run_parses_input_once_before_lock(self):
        # Atomic producer replacement must not switch identity after choosing a lock.
        from publish_core import read_json as original_reader
        script = self.root/'failed.py'; script.write_text('raise SystemExit(5)')
        path = self.root/'manifest.json'; write_json(path,self.manifest)
        self.authorize()
        count = 0
        def reader(candidate):
            nonlocal count
            value = original_reader(candidate)
            if Path(candidate) == path:
                count += 1
                if count > 1:
                    self.fail('manifest was reread after the account lock was selected')
            return value
        with patch('publish_core.read_json',reader):
            with self.assertRaisesRegex(PublishError,'exit=5'):
                run(path,'wechat',self.profile(script),self.store)
        self.assertEqual(count,1)

    def test_unknown_document_dependency_refused(self):
        self.manifest['documents'][0]['asset_ids']=['missing']
        with self.assertRaisesRegex(PublishError,'依赖'): validate(self.manifest)

    def test_resume_after_source_cleanup_and_account_restricted(self):
        script = self.root/'verify.py'
        script.write_text('''import json,sys
from pathlib import Path
r=json.loads(Path(sys.argv[-1]).read_text()); t=r['target']
assert '前文' in json.loads(Path(r['document_snapshot_path']).read_text())['article']
print(json.dumps(dict(manifest_digest=r['manifest_digest'],target_id=t['id'],platform=t['platform'],account=t['account'],remote_id='draft-1',state='draft_ready',content_verified=True)))
''')
        path = self.root/'manifest.json'; write_json(path, self.manifest)
        self.authorize(); self.store.begin(self.manifest, 'wechat')
        (self.root/'article.html').unlink(); (self.root/'theme.mp3').unlink()
        profile = self.profile(script); profile['accounts']['main']['restricted'] = True
        profile['adapters']['wechat'].pop('execute')
        profile['adapters']['wechat']['enabled_capabilities'] = []
        self.assertEqual(run(path, 'wechat', profile, self.store, resume=True)['state'], 'draft_ready')

    def test_orphan_executor_retains_account_lock(self):
        script = self.root/'slow.py'
        script.write_text('''import json,os,sys,time
from pathlib import Path
root=Path(__file__).parent
r=json.loads(Path(sys.argv[-1]).read_text()); t=r['target']
if sys.argv[1]=='execute':
    (root/'child.pid').write_text(str(os.getpid()))
    while not (root/'release').exists(): time.sleep(.01)
    (root/'remote').write_text('draft-1')
print(json.dumps(dict(manifest_digest=r['manifest_digest'],target_id=t['id'],platform=t['platform'],account=t['account'],remote_id='draft-1',state='draft_ready',content_verified=True)))
''')
        path = self.root/'manifest.json'; write_json(path, self.manifest)
        profile_path = self.root/'profile.json'; write_json(profile_path, self.profile(script))
        self.authorize()
        cli = Path(__file__).resolve().parents[1]/'scripts/publish.py'
        proc = subprocess.Popen([sys.executable,str(cli),'--state-dir',str(self.root/'state'),'--profile',str(profile_path),'run',str(path),'--target','wechat'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            deadline = time.monotonic()+5
            while not (self.root/'child.pid').exists() and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue((self.root/'child.pid').exists())
            child_pid = int((self.root/'child.pid').read_text())
            proc.kill(); proc.wait(timeout=3)
            with self.assertRaisesRegex(PublishError, '执行器仍在运行'):
                run(path, 'wechat', self.profile(script), self.store, resume=True)
            self.assertEqual(self.store.status(self.key)['state'], 'submission_unknown')
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=3)
            if child_pid:
                try: os.kill(child_pid, 9)
                except ProcessLookupError: pass


if __name__ == '__main__': unittest.main()
