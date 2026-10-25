import contextlib
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import weibo_browser as weibo
from browser_assets import stage, checked_path
from publish_core import PublishError, Store, read_json, run, validate, write_json


class Node:
    def __init__(self, page, kind): self.page, self.kind = page, kind
    def count(self): return self.page.cards if self.kind == 'card' else len(self.page.media) if self.kind == 'image' else 0 if self.kind == 'video' else 1
    def is_visible(self): return True
    def get_attribute(self, name): return '/u/'+self.page.uid
    def input_value(self): return self.page.text
    def all(self): return []
    def locator(self, selector):
        return Node(self.page, 'text' if selector == 'textarea' else 'file' if 'input' in selector else
                    'video' if selector == 'video' else 'image')
    def evaluate_all(self, script):
        return bool(self.page.pending) if self.kind == 'file' else self.page.media
    def fill(self, value): self.page.text = value; self.page.fills += 1
    def set_input_files(self, path):
        self.page.uploads.append(Path(path).read_bytes())
        index=len(self.page.uploads)
        pid='remotePicture'+str(index)
        self.page.response=Response(pid)
        self.page.media.append(dict(url='https://wx4.sinaimg.cn/orj360/'+pid+'.jpg',
            host='wx4.sinaimg.cn',path='/orj360/'+pid+'.jpg',complete=True,width=512))
        if self.page.lose_before_receipt: raise RuntimeError('lost after upload before receipt')
    def wait_for(self, **kwargs):
        if self.page.lose_after_receipt: raise RuntimeError('lost after durable receipt')


class Response:
    url='https://picupload.weibo.com/interface/upload.php'; status=200
    def __init__(self,pid): self.pid=pid
    def json(self): return {'ret':True,'pic':{'pid':self.pid}}


class Page:
    url='https://weibo.com/'
    def __init__(self):
        self.uid='123456789';self.text='';self.pending=0;self.cards=1;self.media=[]
        self.uploads=[];self.fills=0;self.lose_before_receipt=False;self.lose_after_receipt=False
        self.response=None;self.ident='WeiboRealTargetA';self.context=self
    def bring_to_front(self): raise AssertionError('must not activate Chrome')
    def wait_for_function(self,*args,**kwargs): pass
    @property
    def pages(self): return [self]
    def locator(self, selector):
        return Node(self, 'profile' if selector.startswith('a:has') else 'card' if 'homeWrap' in selector else 'alert')
    def new_cdp_session(self,page):
        owner=self
        class Session:
            def send(self,method): return {'targetInfo':{'targetId':owner.ident}}
            def detach(self): pass
        return Session()
    @contextlib.contextmanager
    def expect_response(self, predicate, **kwargs):
        owner=self
        class Pending:
            @property
            def value(self):
                if not predicate(owner.response): raise RuntimeError('response mismatch')
                return owner.response
        yield Pending()


class WeiboTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.path=self.root/'manifest.json';self.profile_path=self.root/'profile.json'
        self.profile={'configured_modules':['weibo'],'browser_profile':str(self.root/'chrome'),
            'browser_runtime':{'cdp_url':'http://127.0.0.1:9333'},
            'accounts':{'a':{'platforms':['weibo'],'public_identities':{'weibo':'123456789'}}},
            'adapters':{'weibo':{'enabled_capabilities':['images:prefill','text:prefill'],
                'execute':[sys.executable,str(ROOT/'scripts/browser_engine.py'),'execute','--profile',str(self.profile_path)],
                'verify':[sys.executable,str(ROOT/'scripts/browser_engine.py'),'verify','--profile',str(self.profile_path)]}}}
        write_json(self.profile_path,self.profile)
        for i in range(2): (self.root/f'{i}.png').write_bytes(b'fixture '+str(i).encode())
        self.manifest={'schema_version':1,'job_id':'weibo-bundled-test','producer':'files','content_status':'final',
            'source_root':str(self.root),'documents':[],'media_slots':[],
            'assets':[{'id':'image'+str(i),'kind':'image','path':f'{i}.png'} for i in range(2)],
            'targets':[{'id':'weibo','platform':'weibo','account':'a','endpoint':'prefill','content_type':'images',
                        'asset_ids':['image0','image1'],'text':'完整文案\n保留换行','tags':['测试'],
                        'links':[{'label':'资料','url':'https://example.org/read'}]}]}
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.store=Store(self.root/'state');self.addCleanup(self.store.close)
        self.key=self.store.prepare(self.manifest,'weibo')['key'];self.store.authorize(self.manifest,'weibo','test prefill')
        self.page=Page();self.actions=[]
        self.addCleanup(patch.stopall)
        @contextlib.contextmanager
        def connect(profile,**kwargs):
            class Browser: contexts=[self.page]
            yield Browser()
        patch.object(weibo,'connect',side_effect=connect).start()
        patch('runtime.inspect',return_value={'ok':True,'python':'/isolated/python'}).start()
        def invoke(config,action,path,*,lock_fd):
            self.actions.append(action);return weibo.dispatch(path,action,self.profile,lock_fd)
        patch('publish_core.invoke',side_effect=invoke).start()

    @property
    def request(self): return self.store.directory/'requests'/f'{self.key}.json'

    def test_upload_all_images_exactly_once_and_independent_resume(self):
        result=run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(result['state'],'prefill_ready')
        saved=read_json(weibo.marker_path(self.request))
        self.assertEqual([u['id'] for u in saved['uploaded']],['image0','image1'])
        self.assertEqual(len(self.page.uploads),2)
        self.assertEqual(self.page.uploads,[b'fixture 0',b'fixture 1'])
        self.assertEqual(weibo.marker_path(self.request).stat().st_mode&0o777,0o600)
        self.assertEqual(result['remote_id'],'browser-editor:WeiboRealTargetA')
        run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.fills,1);self.assertEqual(len(self.page.uploads),2)
        self.assertEqual(self.actions,['execute','verify','verify'])

    def test_batch_rechecks_session_prefill_and_surfaces_stale_error(self):
        from campaign import prepare_campaign, authorize_campaign, run_campaign
        campaign=prepare_campaign(self.path,self.store)['campaign_id']
        authorize_campaign(campaign,self.store,'approved prefill test')
        first=run_campaign(campaign,self.profile,self.store)
        self.assertTrue(first['all_endpoints_reached'])
        second=run_campaign(campaign,self.profile,self.store)
        self.assertEqual(second['outcomes']['weibo'],'queried')
        self.page.media=[]
        changed=run_campaign(campaign,self.profile,self.store,resume=True)
        self.assertEqual(changed['outcomes']['weibo'],'error')
        self.assertFalse(changed['all_endpoints_reached'])
        self.assertEqual(len(self.page.uploads),2);self.assertEqual(self.page.fills,1)

    def test_lost_upload_response_never_reuploads(self):
        self.page.lose_before_receipt=True
        with self.assertRaises(RuntimeError):run(self.path,'weibo',self.profile,self.store)
        self.page.lose_before_receipt=False
        with self.assertRaisesRegex(PublishError,'回执不完整'):
            run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_saved_receipt_for_partial_upload_is_not_full_success(self):
        self.page.lose_after_receipt=True
        with self.assertRaises(RuntimeError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(len(read_json(weibo.marker_path(self.request))['uploaded']),1)
        with self.assertRaisesRegex(PublishError,'回执不完整'):
            run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_existing_author_draft_and_pending_files_not_overwritten(self):
        for text,pending,media in [('author draft',0,[]),('',1,[]),('',0,[{'url':'blob:owned'}])]:
            self.page.text,self.page.pending,self.page.media=text,pending,media
            with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
            self.assertFalse(weibo.marker_path(self.request).exists())
            self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.fills,0)

    def test_actual_account_mismatch_stops_before_first_mutation(self):
        self.page.uid='987654321'
        with self.assertRaisesRegex(PublishError,'UID'):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.fills,0);self.assertEqual(self.page.uploads,[])

    def test_source_changed_after_sealing_never_uploads(self):
        (self.root/'1.png').write_bytes(b'changed content')
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.fills,0)

    def test_changed_snapshot_rejected_at_upload_boundary(self):
        with patch('browser_assets.file_hash',return_value='0'*64):
            with self.assertRaisesRegex(PublishError,'快照'):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.uploads,[])

    def test_added_deleted_reordered_or_loading_images_reject_stale_ready(self):
        run(self.path,'weibo',self.profile,self.store)
        original=copy.deepcopy(self.page.media)
        for media in [[],list(reversed(original)),original+[original[0]],
                      [dict(original[0],complete=False),original[1]],
                      [dict(original[0],host='sinaimg.cn.evil.test'),original[1]]]:
            self.page.media=media
            with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),2)

    @unittest.skipUnless(shutil.which('node'), 'DOM extraction fixture needs a local JS runtime')
    def test_dom_extractor_keeps_unknown_host_media_for_rejection(self):
        class Images:
            def evaluate_all(self, script):
                fixture = '''const document={baseURI:'https://weibo.com/'};
                function node(src, media) {return {getAttribute:()=>src,complete:true,naturalWidth:512,
                  closest:()=>media?{parentElement:{querySelector:()=>({})}}:null};}
                const nodes=[node('https://wx4.sinaimg.cn/a/remotePicture1.jpg',true),
                  node('https://unexpected.example/unknown.jpg',true),
                  node('https://toolbar.example/icon.svg',false)];'''
                result=subprocess.run([shutil.which('node'), '-e', fixture+'console.log(JSON.stringify(('+script+')(nodes)));'],
                    capture_output=True,text=True,check=True)
                return json.loads(result.stdout)
        class Card:
            def locator(self, selector): return Images()
        items=weibo.media_items(Card())
        self.assertEqual([item['host'] for item in items],['wx4.sinaimg.cn','unexpected.example'])

    def test_new_pending_upload_invalidates_completed_prefill(self):
        run(self.path,'weibo',self.profile,self.store);self.page.pending=1
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),2)

    def test_lost_session_cannot_reclaim_another_editor(self):
        run(self.path,'weibo',self.profile,self.store);self.page.ident='AnotherTarget'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.fills,1)

    def test_unsupported_fields_and_publish_are_refused(self):
        from platform_pack import build_pack
        for field,value in [('endpoint','publish'),('title','title'),('platform_metadata',{'ai_generated':True}),
                            ('cover_asset_id','image0'),('description','different description')]:
            modified=copy.deepcopy(self.manifest);modified['targets'][0][field]=value
            with self.assertRaises(PublishError):weibo.check_pack(build_pack(modified,'weibo',self.profile))
        self.assertEqual(self.page.uploads,[])

    def test_remote_pid_required_and_wrong_response_origin_refused(self):
        for body in [{'ret':False,'pic':{'pid':'remotePicture1'}},{'ret':True,'pic':{}},{'ret':True,'pic':{'pid':'bad"'}}]:
            response=Response('unused');response.json=lambda:body
            with self.assertRaises(PublishError):weibo.picture_id(response)
        response=Response('remotePicture1');response.url='https://picupload.weibo.com.evil.test/interface/upload.php'
        with self.assertRaises(PublishError):weibo.picture_id(response)

    def test_shared_snapshot_hash_rejection_cleans_only_own_directory(self):
        from platform_pack import build_pack
        pack=build_pack(self.manifest,'weibo',self.profile)
        (self.root/'1.png').write_bytes(b'changed')
        self.request.parent.mkdir(parents=True,exist_ok=True)
        with self.assertRaises(PublishError):stage(pack,self.request)
        self.assertFalse(self.request.with_suffix('.browser-assets').exists())
        self.assertTrue((self.root/'0.png').exists())

if __name__=='__main__': unittest.main()
