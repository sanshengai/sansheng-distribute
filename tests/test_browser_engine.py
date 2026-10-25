"""Bounded text prefill contracts; browser DOM candidates need separate live acceptance."""
import contextlib
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import browser_engine as engine
from publish_core import PublishError, Store, read_json, run, validate, write_json


class Node:
    def __init__(self, page, kind):self.page,self.kind=page,kind
    def count(self):
        return self.page.media if self.kind=='media' else self.page.modals if self.kind=='modal' else 1
    def inner_text(self):return 'Display name\n'+self.page.handle if self.kind=='switch' else self.page.text
    def get_attribute(self,name):return '/'+self.page.handle[1:]
    def is_visible(self):return True
    def all(self):return []
    def evaluate_all(self,expression):return bool(self.page.pending_files)
    def locator(self,selector):return Node(self.page,'editor' if 'tweetTextarea' in selector else 'media')
    def fill(self,text):
        self.page.writes.append(text);self.page.text=text
        if self.page.crash_after_fill:raise RuntimeError('connection lost after browser accepted input')


class Page:
    def __init__(self):
        self.url='https://x.com/compose/post';self.handle='@demo';self.text='\n'
        self.media=0;self.pending_files=0;self.modals=1;self.writes=[];self.crash_after_fill=False
        self.context=Context(self)
    def evaluate(self,script):return {'outer':828,'inner':828,'hidden':True}
    def locator(self,selector):
        return Node(self,'switch' if 'AccountSwitcher' in selector else 'link' if 'Profile_Link' in selector else
                    'modal' if 'aria-modal' in selector else 'alert')


class Context:
    def __init__(self,page):self.pages=[page];self.ident='ActualChromeTargetA'
    def new_cdp_session(self,page):
        context=self
        class Session:
            def send(self,method):return {'targetInfo':{'targetId':context.ident}}
            def detach(self):pass
        return Session()


class BrowserEngineTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.profile_path=self.root/'profile.json'
        self.profile={'configured_modules':['x'],'browser_profile':str(self.root/'dedicated'),
            'browser_runtime':{'cdp_url':'http://127.0.0.1:9333'},
            'accounts':{'editor':{'platforms':['x'],'public_identities':{'x':'@demo'}}},
            'adapters':{'x':{'enabled_capabilities':['text:prefill'],'execute':[sys.executable,
                str(ROOT/'scripts/browser_engine.py'),'execute','--profile',str(self.profile_path)],
                'verify':[sys.executable,str(ROOT/'scripts/browser_engine.py'),'verify','--profile',str(self.profile_path)]}}}
        write_json(self.profile_path,self.profile)
        (self.root/'source.txt').write_text('完整文案\n保留第二行',encoding='utf-8')
        self.manifest={'schema_version':1,'job_id':'x-local-fixture','producer':'files','content_status':'final',
            'source_root':str(self.root),'documents':[{'id':'source','path':'source.txt','format':'text'}],'assets':[],'media_slots':[],
            'targets':[{'id':'x','platform':'x','account':'editor','content_type':'text','endpoint':'prefill',
                        'document_id':'source','text':'完整文案\n保留第二行','tags':['测试'],'links':[{'label':'文档','url':'https://example.org/read'}]}]}
        validate(self.manifest,sealing=True);self.path=self.root/'manifest.json';write_json(self.path,self.manifest)
        self.store=Store(self.root/'state');self.addCleanup(self.store.close)
        self.key=self.store.prepare(self.manifest,'x')['key'];self.store.authorize(self.manifest,'x','offline text prefill only')
        self.page=Page();self.actions=[]
        self.addCleanup(patch.stopall)
        @contextlib.contextmanager
        def connection(profile,**kwargs):
            class Browser:contexts=[self.page.context]
            yield Browser()
        patch.object(engine,'connect',side_effect=connection).start()
        patch('runtime.inspect',return_value={'ok':True,'python':'/private/runtime/bin/python3'}).start()
        def invoke(config,action,path,*,lock_fd):
            self.actions.append(action);return engine.dispatch(path,action,self.profile,lock_fd)
        patch('publish_core.invoke',side_effect=invoke).start()

    @property
    def request(self):return self.store.directory/'requests'/f'{self.key}.json'

    def test_same_session_independent_readback_and_no_repeat_fill(self):
        result=run(self.path,'x',self.profile,self.store)
        self.assertEqual(result['state'],'prefill_ready')
        self.assertEqual(result['remote_id'],'browser-editor:ActualChromeTargetA')
        receipt=read_json(engine.marker_path(self.request))
        self.assertEqual(receipt['tracking_scope'],'chrome_session_only')
        self.assertEqual(engine.marker_path(self.request).stat().st_mode & 0o777,0o600)
        run(self.path,'x',self.profile,self.store)
        self.assertEqual(len(self.page.writes),1)
        self.assertEqual(self.actions,['execute','verify','verify'])

    def test_lost_reply_after_fill_recovers_without_writing_again(self):
        self.page.crash_after_fill=True
        with self.assertRaises(RuntimeError):run(self.path,'x',self.profile,self.store)
        self.assertTrue(engine.marker_path(self.request).exists())
        self.page.crash_after_fill=False
        result=run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(result['state'],'prefill_ready');self.assertEqual(len(self.page.writes),1)

    def test_existing_text_is_never_overwritten(self):
        self.page.text='existing author draft'
        with self.assertRaisesRegex(PublishError,'已有文字'):
            run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[])
        self.assertFalse(engine.marker_path(self.request).exists())

    def test_existing_media_is_never_replaced(self):
        self.page.media=1
        with self.assertRaisesRegex(PublishError,'已有媒体'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[])

    def test_pending_file_selection_is_never_replaced(self):
        self.page.pending_files=1
        with self.assertRaisesRegex(PublishError,'已有媒体'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[])

    def test_author_changes_draft_during_identity_roundtrips(self):
        original=engine.target_id
        def changed(page):
            ident=original(page);page.text='new author draft';return ident
        with patch.object(engine,'target_id',side_effect=changed):
            with self.assertRaisesRegex(PublishError,'已有文字'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.text,'new author draft');self.assertEqual(self.page.writes,[])
        self.assertTrue(engine.marker_path(self.request).exists())

    def test_added_media_invalidates_previous_text_prefill(self):
        run(self.path,'x',self.profile,self.store)
        self.page.media=1
        with self.assertRaisesRegex(PublishError,'已有媒体'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.writes),1)


    def test_wrong_account_or_origin_stops_before_mutation(self):
        for handle,url in (('@other','https://x.com/compose/post'),('@demo','https://x.com.evil.test/compose/post')):
            page=Page();page.handle=handle;page.url=url
            with self.assertRaises(PublishError):engine.assert_identity(page,'@demo')
            self.assertEqual(page.writes,[])

    def test_wrong_live_account_stops_actual_execute_path(self):
        self.page.handle='@other'
        with self.assertRaisesRegex(PublishError,'账号与任务绑定不符'):
            run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[])
        self.assertFalse(engine.marker_path(self.request).exists())

    def test_closed_session_or_changed_text_cannot_be_claimed(self):
        run(self.path,'x',self.profile,self.store)
        self.page.context.ident='DifferentTarget'
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.page.context.ident='ActualChromeTargetA';self.page.text='edited by author'
        with self.assertRaisesRegex(PublishError,'文案已变'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.writes),1)

    def test_second_execute_and_multiple_modals_are_rejected(self):
        run(self.path,'x',self.profile,self.store)
        with (self.root/'lock').open('w') as lock:
            with self.assertRaisesRegex(PublishError,'已尝试'):engine.dispatch(self.request,'execute',self.profile,lock.fileno())
        self.page.modals=2
        with self.assertRaisesRegex(PublishError,'不是唯一'):engine.editor(self.page)

    def test_unsupported_endpoint_or_unmapped_fields_refused_in_local_preflight(self):
        from platform_pack import build_pack
        for field,value in (('endpoint','draft'),('title','independent title'),('platform_metadata',{'ai_generated':True})):
            manifest=copy.deepcopy(self.manifest);manifest['targets'][0][field]=value
            with self.assertRaises(PublishError):engine.check_pack(build_pack(manifest,'x',self.profile))
        self.assertEqual(self.page.writes,[]);self.assertEqual(self.actions,[])

    def test_empty_images_publish_refused_without_browser_connection(self):
        from platform_pack import build_pack
        pack=build_pack(self.manifest,'x',self.profile)
        for content_type in ('images',):
            with self.subTest(content_type=content_type), self.assertRaisesRegex(PublishError,'一至四张'):
                engine.check_pack(dict(pack,endpoint='publish',content_type=content_type))
        self.assertEqual(self.actions,[])

    def test_nontext_document_is_refused(self):
        from platform_pack import build_pack
        manifest=copy.deepcopy(self.manifest);manifest['documents'][0]['format']='markdown'
        with self.assertRaises(PublishError):engine.check_pack(build_pack(manifest,'x',self.profile))


if __name__=='__main__':unittest.main()
