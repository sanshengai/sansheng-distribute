"""Core-to-CLI tests: durable identity, independent reads and no retry after ambiguity."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import bilibili_adapter as adapter
from environment import load_profile
from platform_pack import build_pack
from publish_core import PublishError, Store, digest, read_json, run, validate, write_json


class BilibiliAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'video.mp4').write_bytes(b'sealed test video')
        (self.root/'cover.jpg').write_bytes(b'sealed test cover')
        self.cover = 'https://i0.hdslb.com/bfs/archive/abcdef012345.jpg'
        self.credentials = self.root/'auth.json'
        write_json(self.credentials, {'cookie_info':{'cookies':[{'name':'SESSDATA','value':'LOCAL-SECRET'}]}})
        self.cli = self.root/'cli.py'
        self.cli.write_text('import sys\nprint("https://i0.hdslb.com/bfs/archive/abcdef012345.jpg",flush=True)\n'
            'print(\'ResponseData { code: 0, data: Some(Object {"aid": Number(7), "bvid": String("BVTest7")}) }\',flush=True)\n')
        self.manifest = dict(schema_version=1, job_id='bili-fixture', producer='ordinary-files', content_status='final',
            source_root=str(self.root), documents=[], media_slots=[], assets=[
                {'id':'video','path':'video.mp4','kind':'video'}, {'id':'cover','path':'cover.jpg','kind':'image'}],
            targets=[dict(id='bili', platform='bilibili', account='editorial', content_type='video', endpoint='publish',
                title='「示例」S1E01｜保留完整标题', description='完整简介\n第二段', tags=['英语','校园'],
                asset_ids=['video','cover'], cover_asset_id='cover', platform_metadata={'copyright':1,'ai_generated':True})])
        validate(self.manifest, sealing=True)
        self.path = self.root/'manifest.json'; write_json(self.path, self.manifest)
        self.profile_path = self.root/'profile.json'
        self.profile = {'configured_modules':['bilibili'], 'accounts':{'editorial':{
            'platforms':['bilibili'], 'public_identities':{'bilibili':'42'}}},
            'adapters':{'bilibili':{'enabled_capabilities':['video:publish'], 'timeout_seconds':2100,
                'cli':{'directory':str(self.root/'tool'), 'credentials':str(self.credentials), 'tid':229},
                'execute':[sys.executable,str(ROOT/'scripts/bilibili_adapter.py'),'execute','--profile',str(self.profile_path)],
                'verify':[sys.executable,str(ROOT/'scripts/bilibili_adapter.py'),'verify','--profile',str(self.profile_path)]}}}
        write_json(self.profile_path, self.profile)
        self.store = Store(self.root/'state'); self.addCleanup(self.store.close)
        self.key = self.store.prepare(self.manifest, 'bili')['key']
        self.store.authorize(self.manifest, 'bili', 'offline fixture, no real upload')
        target = self.manifest['targets'][0]
        self.archive = {'archive':{'aid':7,'bvid':'BVTest7','mid':42,'title':target['title'],
            'desc':target['description'],'tag':'英语,校园','copyright':1,'tid':229,'state':-30,
            'creation_statement':{'id':1},'cover':self.cover}, 'videos':[{'cid':9,'duration':61}]}
        self.public = {'aid':7,'bvid':'BVTest7','owner':{'mid':42},'title':target['title'],'state':0,'cid':9}
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(adapter, 'duration', return_value=61).start()
        patch('biliup_bridge.probe', return_value={'binary':str(self.root/'tool/biliup')}).start()
        real_plan = adapter.plan
        def executable_plan(*args):
            result = real_plan(*args)
            result['argv'] = [sys.executable,str(self.cli)]
            return result
        patch.object(adapter, 'plan', side_effect=executable_plan).start()
        patch.object(adapter.Reader, 'get', new=lambda reader, kind, **params: self.read(kind, params)).start()
        def invoke(config, action, request_path, *, lock_fd):
            self.calls.append(action)
            return adapter.dispatch(request_path, action, self.profile, lock_fd)
        patch('publish_core.invoke', side_effect=invoke).start()

    def read(self, kind, params):
        if kind == 'identity': return {'isLogin':True,'mid':42}
        if kind == 'archive':
            self.assertEqual(params, {'aid':'7'}); return copy.deepcopy(self.archive)
        if kind == 'public': return copy.deepcopy(self.public)
        raise AssertionError(kind)

    @property
    def request(self): return self.store.directory/'requests'/f'{self.key}.json'

    def test_accepted_reviewing_then_readonly_resume_reaches_same_public_id(self):
        value = run(self.path, 'bili', self.profile, self.store)
        self.assertEqual(value['state'], 'pending'); self.assertEqual(value['remote_id'],'7')
        receipt = read_json(adapter.receipt_path(self.request))
        self.assertEqual(receipt['bvid'],'BVTest7')
        self.assertEqual(adapter.receipt_path(self.request).stat().st_mode & 0o777,0o600)
        self.assertEqual(self.request.with_suffix('.bilibili-private.log').stat().st_mode & 0o777,0o600)
        self.archive['archive']['state'] = 0
        (self.root/'video.mp4').unlink()  # Resume uses the sealed receipt, not another upload.
        value = run(self.path,'bili',self.profile,self.store,resume=True)
        self.assertEqual(value['state'],'published')
        self.assertEqual(self.calls,['execute','verify','verify'])
        value=run(self.path,'bili',self.profile,self.store,resume=True)
        self.assertEqual(value['state'],'published')
        self.assertEqual(value['remote_id'],'7')
        self.assertEqual(self.calls,['execute','verify','verify','verify'])

    def test_cli_failure_after_accepted_id_does_not_lose_id_or_repost(self):
        with self.cli.open('a') as stream: stream.write('sys.exit(1)\n')
        value = run(self.path,'bili',self.profile,self.store)
        self.assertEqual(value['state'],'pending')
        self.assertEqual(value['remote_id'],'7')
        self.assertEqual(read_json(adapter.receipt_path(self.request))['exit_code'],1)
        run(self.path,'bili',self.profile,self.store,resume=True)
        self.assertEqual(self.calls.count('execute'),1)

    def test_unknown_response_never_guesses_title_or_restarts(self):
        self.cli.write_text('print("connection lost")\n')
        with self.assertRaisesRegex(PublishError,'结果未知'): run(self.path,'bili',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['state'],'submission_unknown')
        for resume in (False,True):
            with self.assertRaises(PublishError): run(self.path,'bili',self.profile,self.store,resume=resume)
        self.assertEqual(self.calls.count('execute'),1)

    def test_wrong_backend_fields_cannot_mark_published(self):
        self.archive['archive']['state'] = 0
        run(self.path,'bili',self.profile,self.store)
        # Start a new store per changed readback, preserving the same real ID.
        pack = build_pack(self.manifest,'bili',self.profile)
        receipt = read_json(adapter.receipt_path(self.request))
        for key, value in [('mid',43),('aid',8),('bvid','BVOther'),('tid',1),
                           ('title','截断标题'),('desc','少一段'),('tag','英语'),('copyright',2),
                           ('creation_statement',{'id':2})]:
            data = copy.deepcopy(self.archive); data['archive'][key] = value
            with self.subTest(key=key), self.assertRaises(PublishError):
                adapter.verify_content(pack,receipt,data,'42',229)
        for bad in ([],[{'duration':61}], [{'cid':9,'duration':20}], [{'cid':9,'duration':'nan'}]):
            data = copy.deepcopy(self.archive); data['videos'] = bad
            with self.assertRaises(PublishError): adapter.verify_content(pack,receipt,data,'42',229)

    def test_unverified_cover_or_public_identity_keeps_pending(self):
        self.archive['archive']['state'] = 0
        self.archive['archive']['cover'] = 'https://i0.hdslb.com/bfs/archive/deadbeef.jpg'
        self.assertEqual(run(self.path,'bili',self.profile,self.store)['state'],'pending')
        self.archive['archive']['cover'] = self.cover
        self.public['owner']['mid'] = 99
        self.assertEqual(run(self.path,'bili',self.profile,self.store,resume=True)['state'],'pending')
        self.public['owner']['mid'] = 42
        self.assertEqual(run(self.path,'bili',self.profile,self.store,resume=True)['state'],'published')

    def test_source_changed_during_account_check_is_never_uploaded(self):
        original = self.read
        def changed(kind, params):
            if kind == 'identity': (self.root/'video.mp4').write_bytes(b'changed while checking login')
            return original(kind, params)
        with patch.object(self, 'read', side_effect=changed):
            with self.assertRaisesRegex(PublishError,'核对期间改变'):
                run(self.path,'bili',self.profile,self.store)
        self.assertFalse(self.request.with_suffix('.bilibili-private.log').exists())
        self.assertFalse(adapter.receipt_path(self.request).exists())

    def test_cli_source_is_private_verified_snapshot(self):
        run(self.path,'bili',self.profile,self.store)
        root = self.request.with_suffix('.bilibili-assets')
        self.assertEqual((root/'video.mp4').read_bytes(),b'sealed test video')
        self.assertEqual((root/'cover.jpg').read_bytes(),b'sealed test cover')
        self.assertEqual((root/'video.mp4').stat().st_mode & 0o777,0o400)
        (self.root/'video.mp4').write_bytes(b'producer continues editing')
        self.assertEqual((root/'video.mp4').read_bytes(),b'sealed test video')

    def test_unsupported_fields_and_unknown_ai_stop_before_upload(self):
        for metadata in ({'copyright':1}, {'copyright':1,'ai_generated':True,'collection':'required'}):
            m = copy.deepcopy(self.manifest); m['targets'][0]['platform_metadata'] = metadata
            validate(m,sealing=True)
            self.calls.clear()
            with self.assertRaises(PublishError):
                adapter.preflight_bilibili(m,m['targets'][0],self.profile,self.profile['adapters']['bilibili']['execute'])
            self.assertEqual(self.calls,[])

    def test_credentials_no_group_read_and_no_header_injection(self):
        self.credentials.chmod(0o644)
        with self.assertRaises(PublishError): adapter.credential_header(self.credentials)
        self.credentials.chmod(0o600)
        for cookies in ([], [{'name':'X\r\nHeader','value':'bad'}], [{'name':'X','value':'a;b'}], [None]):
            write_json(self.credentials, {'cookie_info':{'cookies':cookies}})
            with self.assertRaises(PublishError): adapter.credential_header(self.credentials)

    def test_risk_stops_child_and_no_second_attempt(self):
        self.cli.write_text('import time\nprint("检测到限流",flush=True)\ntime.sleep(15)\n')
        with self.assertRaises(PublishError): run(self.path,'bili',self.profile,self.store)
        self.assertEqual(read_json(adapter.receipt_path(self.request))['stopped'],'platform_verification_or_rate_limit')
        with self.assertRaises(PublishError): run(self.path,'bili',self.profile,self.store,resume=True)
        self.assertEqual(self.calls.count('execute'),1)

    def test_relative_cli_paths_resolve_against_profile(self):
        value = copy.deepcopy(self.profile)
        value['adapters']['bilibili']['cli'].update(directory='tool',credentials='auth.json')
        write_json(self.profile_path,value)
        cfg = load_profile(self.profile_path)['adapters']['bilibili']['cli']
        self.assertEqual(cfg['directory'],str(self.root/'tool'))
        self.assertEqual(cfg['credentials'],str(self.credentials))

    def test_short_outer_budget_rejected_before_upload(self):
        for timeout in (None,100,True,float('nan'),float('inf')):
            profile = copy.deepcopy(self.profile)
            if timeout is None: profile['adapters']['bilibili'].pop('timeout_seconds')
            else: profile['adapters']['bilibili']['timeout_seconds'] = timeout
            with self.assertRaisesRegex(PublishError,'外层'):
                adapter.preflight_bilibili(self.manifest,self.manifest['targets'][0],profile,
                    profile['adapters']['bilibili']['execute'])
        self.assertEqual(self.calls,[])


if __name__ == '__main__': unittest.main()
