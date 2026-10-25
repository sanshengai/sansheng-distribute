import contextlib
import copy
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import douyin_browser as douyin
from publish_core import PublishError, Store, read_json, run, validate, write_json


class Node:
    def __init__(self, page, kind, values=None): self.page, self.kind, self.values = page, kind, values
    def filter(self, **kwargs):
        if self.values is not None and 'has_text' in kwargs:
            return Node(self.page,self.kind,[v for v in self.values if kwargs['has_text'].search(v)])
        return self
    def count(self):
        p=self.page
        if self.values is not None:return len(self.values)
        if self.kind=='old':return 1 if p.old_content else 0
        if self.kind=='video':return 1 if p.uploads else 0
        if self.kind=='modal':return int(p.modal)
        if self.kind=='chosen':return int(bool(p.pending_declaration))
        return 1
    def is_visible(self):return self.count()==1
    def get_attribute(self,name):return 'video/mp4' if name=='accept' else None
    def evaluate(self,script):
        if self.kind=='file':return not self.page.pending_file
        if self.kind=='video':return dict(src=self.page.preview,duration=self.page.duration,ready=4)
        if self.kind=='description' and script==douyin.DESCRIPTION_READ:return self.page.description
        if self.kind=='description' and script==douyin.NATIVE_DOCUMENT_READ:
            return dict(text=self.page.description,topics=list(self.page.topics))
        if self.kind=='description' and script==douyin.CARET_AT_END:return self.page.caret_at_end
        raise AssertionError(self.kind)
    def evaluate_all(self,script):return bool(self.page.pending_file)
    def all(self):return []
    def input_value(self):return self.page.title
    def inner_text(self):
        return {'number':'抖音号：'+self.page.uid,'description':self.page.description,
            'declaration':self.page.declaration,'chosen':self.page.pending_declaration,
            'selection':('请选择合集' if self.page.collection=='请选择合集' else self.page.collection+'\n共8个作品'),
            'selected_name':self.page.collection}.get(self.kind,'')
    def fill(self,value):
        self.page.fills+=1
        if self.kind=='title':self.page.title=value
        elif self.kind=='description':self.page.description=value;self.page.topics=[]
        else:raise AssertionError(self.kind)
    def press(self,key,**kwargs):
        if self.kind!='description' or key!='Enter':raise AssertionError(key)
        self.page.description+='\n'
    def press_sequentially(self,value,**kwargs):
        if self.kind!='description':raise AssertionError(self.kind)
        self.page.description+=value
    def set_input_files(self,path,**kwargs):
        self.page.uploads.append(Path(path).read_bytes());self.page.url='https://creator.douyin.com'+douyin.EDITOR
        if self.page.lose_upload:raise RuntimeError('upload outcome lost')
    def locator(self,selector):
        if selector=='xpath=ancestor::*[@role="option"][1]':return self
        if selector.startswith('xpath='):return Node(self.page,'section')
        if 'selectText-' in selector:return Node(self.page,'declaration')
        if selector=='.semi-select-selection-text':return Node(self.page,'selection')
        if selector=='[class^="selected-item-title-"]' or selector=='.semi-select-selection-text [class^="selected-item-title-"]':
            return Node(self.page,'selected_name',[] if self.page.collection=='请选择合集' else [self.page.collection])
        if selector.startswith('label:has'):return Node(self.page,'chosen')
        if selector=='label':return Node(self.page,'declaration_option',list(douyin.DECLARATIONS))
        raise AssertionError(selector)
    def get_by_text(self,text,**kwargs):return Node(self.page,'declaration_option',[text])
    def get_by_role(self,role,**kwargs):return Node(self.page,'confirm')
    def click(self,**kwargs):
        p=self.page
        if self.kind=='declaration':p.modal=True
        elif self.kind=='declaration_option':
            p.choice_clicks+=1
            if p.remount_every_choice or (p.remount_first_choice and p.choice_clicks==1):
                p.declaration=self.values[0];p.pending_declaration=''
            else:p.pending_declaration=self.values[0]
        elif self.kind=='confirm':p.declaration=p.pending_declaration;p.modal=False
        elif self.kind=='collection':p.dropdown=True
        elif self.kind=='option':p.collection=self.values[0];p.dropdown=False
        elif self.kind=='add_topic':p.description+='#'
        elif self.kind=='topic':
            p.topics.append(self.values[0]);p.description+=' '
        else:raise AssertionError(self.kind)
    def wait_for(self,**kwargs):
        if self.page.lost_identity:raise RuntimeError('identity page unavailable')


class Identity:
    url=douyin.HOME
    def __init__(self,page):self.page=page
    def goto(self,*args,**kwargs):
        if self.page.identity_goto_error:raise RuntimeError('homepage unavailable')
    def get_by_text(self,*args,**kwargs):return Node(self.page,'number')
    def close(self):self.page.identity_closed+=1


class Page:
    def __init__(self):
        self.url='https://creator.douyin.com'+douyin.UPLOAD;self.uid='fixture_123'
        self.title='';self.description='';self.declaration='请选择自主声明';self.collection='请选择合集'
        self.pending_declaration='';self.options=['A collection'];self.uploads=[];self.fills=0
        self.old_content=False;self.pending_file=False;self.modal=False;self.dropdown=False
        self.lose_upload=False;self.lost_identity=False;self.identity_goto_error=False
        self.identity_closed=0;self.identity_opened=0;self.context=self;self.ident='DouyinFixtureTarget'
        self.remount_first_choice=False;self.remount_every_choice=False;self.choice_clicks=0
        self.topics=[];self.topic_options=['知识图解','AI'];self.caret_at_end=True
        self.preview='https://video-cn.douyin.com/sign/expiry/tos-cn-v-fixture/fixtureObject?rotating=1';self.duration=4
    @property
    def pages(self):return [self]
    def new_page(self):self.identity_opened+=1;return Identity(self)
    def new_cdp_session(self,page):
        owner=self
        class Session:
            def send(self,method):return {'targetInfo':{'targetId':owner.ident}}
            def detach(self):pass
        return Session()
    def bring_to_front(self):raise AssertionError('must not activate Chrome')
    def wait_for_function(self,*args,**kwargs):pass
    def locator(self,selector):
        if selector==douyin.TITLE:return Node(self,'title')
        if selector==douyin.DESCRIPTION:return Node(self,'description')
        if selector==douyin.COLLECTION:return Node(self,'collection')
        if selector==douyin.TITLE+', '+douyin.DESCRIPTION+', video':return Node(self,'old')
        if selector=='input[type="file"]':return Node(self,'file')
        if selector=='video':return Node(self,'video')
        if selector==douyin.DECLARATION_MODAL:return Node(self,'modal')
        if selector==douyin.TOPIC_NAME:return Node(self,'topic',self.topic_options)
        if selector.startswith('[role="option"]'):return Node(self,'option',self.options)
        return Node(self,'alert')
    def get_by_text(self,text,**kwargs):
        return Node(self,'add_topic' if text=='#添加话题' else 'label' if text=='自主声明' else 'reupload')


class DouyinTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.path=self.root/'manifest.json';self.profile_path=self.root/'profile.json'
        self.profile={'configured_modules':['douyin'],'browser_profile':str(self.root/'chrome'),
            'browser_runtime':{'cdp_url':'http://127.0.0.1:9333'},
            'accounts':{'a':{'platforms':['douyin'],'public_identities':{'douyin':'fixture_123'}}},
            'adapters':{'douyin':{'enabled_capabilities':['video:prefill'],
                'execute':[sys.executable,str(ROOT/'scripts/browser_engine.py'),'execute','--profile',str(self.profile_path)],
                'verify':[sys.executable,str(ROOT/'scripts/browser_engine.py'),'verify','--profile',str(self.profile_path)]}}}
        write_json(self.profile_path,self.profile);(self.root/'fixture.mp4').write_bytes(b'local sealed fixture')
        self.manifest={'schema_version':1,'job_id':'douyin-bundled-test','producer':'files','content_status':'final',
            'source_root':str(self.root),'documents':[],'media_slots':[],
            'assets':[{'id':'video','kind':'video','path':'fixture.mp4'}],
            'targets':[{'id':'douyin','platform':'douyin','account':'a','endpoint':'prefill','content_type':'video',
                'asset_ids':['video'],'title':'系列 S1E01｜样本','description':'完整简介\n保留换行',
                'links':[{'label':'资料','url':'https://example.org/read'}],
                'platform_metadata':{'ai_generated':False,'declaration':'无需添加自主声明','collection_name':'A collection'}}]}
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.store=Store(self.root/'state');self.addCleanup(self.store.close)
        self.key=self.store.prepare(self.manifest,'douyin')['key'];self.store.authorize(self.manifest,'douyin','test prefill')
        self.page=Page();self.actions=[];self.addCleanup(patch.stopall)
        @contextlib.contextmanager
        def connect(profile,**kwargs):
            self.assertTrue(kwargs['writing'])
            class Browser:contexts=[self.page]
            yield Browser()
        patch.object(douyin,'connect',side_effect=connect).start()
        @contextlib.contextmanager
        def background_tab(page):
            identity=page.new_page()
            try:yield identity
            finally:identity.close()
        patch.object(douyin,'background_tab',side_effect=background_tab).start()
        patch('runtime.inspect',return_value={'ok':True,'python':'/isolated/python'}).start()
        def invoke(config,action,path,*,lock_fd):
            self.actions.append(action);return douyin.dispatch(path,action,self.profile,lock_fd)
        patch('publish_core.invoke',side_effect=invoke).start()
    @property
    def request(self):return self.store.directory/'requests'/f'{self.key}.json'

    def test_complete_native_prefill_and_independent_resume_no_reupload(self):
        result=run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(result['state'],'prefill_ready');self.assertEqual(self.page.uploads,[b'local sealed fixture'])
        self.assertEqual(self.page.declaration,'无需添加自主声明');self.assertEqual(self.page.collection,'A collection')
        self.assertNotIn('rotating',str(read_json(douyin.marker_path(self.request))))
        run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(self.page.fills,2);self.assertEqual(len(self.page.uploads),1)
        self.assertEqual(self.page.identity_opened,self.page.identity_closed)

    def test_wrong_actual_account_never_uploads(self):
        self.page.uid='wrong_123'
        with self.assertRaisesRegex(PublishError,'实际登录账号'):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.identity_opened,self.page.identity_closed)
        self.assertFalse(douyin.marker_path(self.request).exists())

    def test_native_first_choice_remount_reselects_same_value_once(self):
        self.page.remount_first_choice=True
        result=run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(result['state'],'prefill_ready');self.assertEqual(self.page.choice_clicks,2)
        self.assertEqual(self.page.declaration,'无需添加自主声明')
        self.assertEqual(len(self.page.uploads),1)

    def test_repeated_native_remount_stops_after_same_choice_once(self):
        self.page.remount_every_choice=True
        with self.assertRaisesRegex(PublishError,'声明单选读回不符'):
            run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.choice_clicks,2);self.assertEqual(len(self.page.uploads),1)

    def test_wrong_editor_origin_stops_before_upload(self):
        self.page.url='https://creator.douyin.com.evil.example'+douyin.UPLOAD
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.uploads,[])

    def test_native_origin_check_rejects_source_swap(self):
        self.page.url='https://creator.douyin.com.evil.example'+douyin.UPLOAD
        with self.assertRaises(PublishError):douyin.empty_upload(self.page)
        self.assertEqual(self.page.uploads,[])

    def test_original_attempt_rejects_execute_before_connect(self):
        run(self.path,'douyin',self.profile,self.store)
        with tempfile.TemporaryFile() as lock, patch.object(douyin,'connect',side_effect=AssertionError('must not connect')):
            with self.assertRaisesRegex(PublishError,'已有尝试'):
                douyin.dispatch(self.request,'execute',self.profile,lock.fileno())
        self.assertEqual(len(self.page.uploads),1)

    def test_identity_tab_closed_on_navigation_or_wait_failure(self):
        for field in ['identity_goto_error','lost_identity']:
            with self.subTest(field=field):
                setattr(self.page,field,True)
                with self.assertRaises(RuntimeError):douyin.assert_identity(self.page,'fixture_123')
                setattr(self.page,field,False)
        self.assertEqual(self.page.identity_opened,self.page.identity_closed)

    def test_existing_editor_or_pending_file_preserved(self):
        for old,pending in [(True,False),(False,True)]:
            self.page.old_content,self.page.pending_file=old,pending
            with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
            self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.fills,0)

    def test_lost_upload_outcome_is_query_only(self):
        self.page.lose_upload=True
        with self.assertRaises(RuntimeError):run(self.path,'douyin',self.profile,self.store)
        self.page.lose_upload=False
        with self.assertRaisesRegex(PublishError,'尚未完成全部字段'):run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.fills,0)

    def test_missing_or_duplicate_collection_never_counts_complete(self):
        self.page.options=['A collection','A collection']
        with self.assertRaisesRegex(PublishError,'指定合集'):run(self.path,'douyin',self.profile,self.store)
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_absent_collection_never_counts_complete(self):
        self.page.options=[]
        with self.assertRaisesRegex(PublishError,'指定合集'):run(self.path,'douyin',self.profile,self.store)
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_empty_description_native_placeholder_only(self):
        from platform_pack import build_pack
        run(self.path,'douyin',self.profile,self.store)
        pack=build_pack(self.manifest,'douyin',self.profile)
        pack['fields']['composed_text']=''
        self.page.description=''
        douyin.verify_fields(self.page,pack,read_json(douyin.marker_path(self.request)))
        self.page.description='\u200bchanged'
        with self.assertRaises(PublishError):
            douyin.verify_fields(self.page,pack,read_json(douyin.marker_path(self.request)))

    def test_nonempty_description_hidden_character_is_not_normalized(self):
        run(self.path,'douyin',self.profile,self.store)
        self.page.description+='\u200b'
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)

    def test_changed_fields_media_identity_or_collection_reject_ready(self):
        run(self.path,'douyin',self.profile,self.store)
        for field,value in [('title','changed'),('description','changed'),('declaration','内容由AI生成'),
                            ('collection','other'),('preview','https://video-cn.douyin.com/other'),
                            ('preview','https://video-cn.douyin.com.evil.example/video'),('duration',0)]:
            original=getattr(self.page,field);setattr(self.page,field,value)
            with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
            setattr(self.page,field,original)
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.fills,2)

    def test_rotated_signed_query_keeps_same_preview_identity(self):
        run(self.path,'douyin',self.profile,self.store)
        self.page.preview=self.page.preview.replace('rotating=1','rotating=2')
        self.assertEqual(run(self.path,'douyin',self.profile,self.store,resume=True)['state'],'prefill_ready')

    def test_signed_path_prefix_rotation_keeps_same_tos_object(self):
        run(self.path,'douyin',self.profile,self.store)
        self.page.preview=self.page.preview.replace('/sign/expiry/','/new-sign/new-expiry/')
        self.assertEqual(run(self.path,'douyin',self.profile,self.store,resume=True)['state'],'prefill_ready')

    def test_completion_after_last_marker_write_lost_is_read_back_only(self):
        run(self.path,'douyin',self.profile,self.store)
        marker=douyin.marker_path(self.request);saved=read_json(marker);saved['state']='uploaded';write_json(marker,saved)
        self.assertEqual(run(self.path,'douyin',self.profile,self.store,resume=True)['state'],'prefill_ready')
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.fills,2)

    def test_changed_sealed_source_stops_before_upload(self):
        (self.root/'fixture.mp4').write_bytes(b'changed source')
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.identity_opened,0)

    def test_sealed_source_and_staged_snapshot_must_match(self):
        with patch('browser_assets.file_hash',return_value='0'*64):
            with self.assertRaisesRegex(PublishError,'快照'):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.uploads,[])

    def test_unmapped_fields_or_final_send_refused_before_upload(self):
        from platform_pack import build_pack
        for field,value in [('endpoint','publish'),('tags',['空 白']),('platform_metadata',{'collection_id':'12'}),
                            ('platform_metadata',{'ai_generated':'true'}),('platform_metadata',{}),
                            ('platform_metadata',{'ai_generated':True,'declaration':'无需添加自主声明'})]:
            modified=copy.deepcopy(self.manifest);modified['targets'][0][field]=value
            with self.assertRaises(PublishError):douyin.check_pack(build_pack(modified,'douyin',self.profile))
        self.assertEqual(self.page.uploads,[])

    def with_topics(self, names=('知识图解','AI')):
        self.manifest['targets'][0]['tags']=list(names)
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'douyin')['key']
        self.store.authorize(self.manifest,'douyin','test native topics prefill')

    def test_topics_native_entities_complete_text_links_and_query_only_resume(self):
        self.with_topics()
        result=run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(result['state'],'prefill_ready')
        self.assertEqual(self.page.topics,['知识图解','AI'])
        self.assertEqual(self.page.description,'完整简介\n保留换行\n\n#知识图解 #AI \n\n资料：https://example.org/read')
        before=(self.page.description,self.page.fills,len(self.page.uploads))
        run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(before,(self.page.description,self.page.fills,len(self.page.uploads)))

    def test_plain_hashtag_text_is_not_native_binding(self):
        self.with_topics();run(self.path,'douyin',self.profile,self.store)
        self.page.topics=[]
        with self.assertRaisesRegex(PublishError,'标题或简介已改变'):
            run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_topic_order_or_extra_topic_never_reports_ready(self):
        self.with_topics();run(self.path,'douyin',self.profile,self.store)
        for topics in (['AI','知识图解'],['知识图解','AI','other']):
            self.page.topics=topics
            with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_missing_native_topic_stops_without_fallback_or_reupload(self):
        self.with_topics();self.page.topic_options=[]
        with self.assertRaisesRegex(PublishError,'指定原生话题'):run(self.path,'douyin',self.profile,self.store)
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.topics,[])

    def test_duplicate_native_topic_matches_are_not_guessed(self):
        self.with_topics();self.page.topic_options=['知识图解','知识图解']
        with self.assertRaisesRegex(PublishError,'指定原生话题'):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.topics,[])

    def test_topics_preserve_authored_spaces_and_hidden_characters(self):
        self.manifest['targets'][0]['description']='  authored\u200b  '
        self.with_topics();run(self.path,'douyin',self.profile,self.store)
        self.assertTrue(self.page.description.startswith('  authored\u200b  \n\n'))
        self.page.description=self.page.description.replace('authored\u200b','authored')
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)

    def test_caret_moved_before_append_preserves_existing_text(self):
        self.with_topics();self.page.caret_at_end=False
        with self.assertRaisesRegex(PublishError,'光标不在末尾'):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.description,'完整简介\n保留换行');self.assertEqual(self.page.topics,[])

    def test_duplicate_empty_or_unsupported_topics_rejected_before_connect(self):
        from platform_pack import build_pack
        for tags in (['AI','#AI'],['#'],['空 白'],['隐\u200b藏'],list('abcdef')):
            changed=copy.deepcopy(self.manifest);changed['targets'][0]['tags']=tags
            with self.assertRaises(PublishError):douyin.check_pack(build_pack(changed,'douyin',self.profile))
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.identity_opened,0)


if __name__=='__main__':unittest.main()
