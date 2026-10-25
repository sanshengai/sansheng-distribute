import contextlib
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import browser_engine as engine
import douyin_browser as home
import douyin_publish_browser as sender
from platform_pack import build_pack
from publish_core import PublishError,Store,read_json,write_json,validate,run
from test_douyin_browser import Page as BasePage,Node,Identity

UID='1234567890123456789';RID='1234567890123456788'


def choices():
    values=[('公开','0',True),('好友可见','2',False),('仅自己可见','1',False),
            ('立即发布','0',True),('定时发布','1',False),('不同时发布','0',True),('同时发布到','1',False)]
    return [dict(text=text,inputs=[dict(type='checkbox',checked=checked,disabled=False,value=value)]) for text,value,checked in values]


def response(body=None,path='/web/api/media/aweme/create_v2/'):
    value=Mock(url='https://creator.douyin.com'+path,status=200)
    value.request.method='POST';value.body.return_value=json.dumps(body or dict(status_code=0,item_id=RID)).encode()
    return value


class Page(BasePage):
    def __init__(self):
        super().__init__();self.commit_response='ok';self.clicks=0;self.closes=0;self.rows=choices();self.clock=0
    def goto(self,url,**kw):self.url=url
    def close(self):self.closes+=1
    def wait_for_timeout(self,ms):self.clock+=1
    def get_by_role(self,role,**kw):
        if role=='button' and kw.get('name')=='发布':return Mock(count=lambda:1,is_visible=lambda:True,is_disabled=lambda:False)
        raise AssertionError((role,kw))
    def locator(self,selector):
        if selector=='label':return Mock(evaluate_all=lambda script:copy.deepcopy(self.rows))
        return super().locator(selector)


class NativeReceiptTests(unittest.TestCase):
    def test_both_routes_persist_exact_string_id_before_memory_success(self):
        for path,body in [('/web/api/media/aweme/create_v2/',dict(status_code=0,item_id=RID)),
                          ('/web/api/media/aweme/create/',dict(status_code=0,aweme=dict(aweme_id=RID,author=dict(uid=UID))))]:
            seen=[];capture=sender.Capture(UID,lambda value:seen.append((capture.accepted,value)))
            capture.response(response(body,path))
            self.assertIsNone(seen[0][0]);self.assertEqual(capture.accepted['remote_id'],RID)
            self.assertEqual(capture.accepted['bound_native_user_id'],UID)
            self.assertFalse(capture.error)

    def test_failed_ambiguous_rounded_or_duplicate_key_receipt_never_accepted(self):
        bad=[dict(status_code=True,item_id=RID),dict(status_code=8,item_id=RID),
             dict(status_code=0,item_id=int(RID)),dict(status_code=0),dict(status_code=0,item_id=RID,error={}),
             dict(status_code=0,item_id=RID,aweme=dict(aweme_id='999')),
             dict(status_code=0,item_id=RID,aweme=dict(aweme_id=RID,author=dict(uid='999')))]
        for body in bad:
            capture=sender.Capture(UID,Mock());capture.response(response(body))
            self.assertIsNone(capture.accepted);self.assertTrue(capture.error);capture.save.assert_not_called()
        value=response();value.body.return_value=b'{"status_code":0,"item_id":"1","item_id":"2"}'
        capture=sender.Capture(UID,Mock());capture.response(value)
        self.assertIsNone(capture.accepted);self.assertTrue(capture.error)

    def test_persistence_failure_and_second_response_are_sticky(self):
        capture=sender.Capture(UID,Mock(side_effect=OSError('disk failure')));capture.response(response())
        self.assertIsNone(capture.accepted);self.assertTrue(capture.error)
        capture=sender.Capture(UID,Mock());capture.response(response());capture.response(response())
        self.assertEqual(capture.count,2);self.assertTrue(capture.error);self.assertEqual(capture.save.call_count,1)

    def test_other_hosts_and_endpoints_are_not_accepted(self):
        for url in ['https://creator.douyin.com.evil.org/web/api/media/aweme/create_v2/',
                    'https://creator.douyin.com/web/api/media/aweme/update_v2/']:
            capture=sender.Capture(UID,Mock());value=response();value.url=url;capture.response(value)
            self.assertEqual(capture.count,0);capture.save.assert_not_called()

    def test_checkpoint_keeps_memory_unchanged_when_durable_write_fails(self):
        saved=dict(state='before')
        with patch.object(sender,'write_json',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):sender.checkpoint(saved,Path('/unused'),native_input_checkpointed_at=1)
        self.assertEqual(saved,dict(state='before'))

    def test_native_options_missing_private_scheduled_crosspost_or_paid_rejected(self):
        self.assertEqual(sender.selected_options(choices()),dict(visibility='public',schedule='immediate',cross_post=False))
        bad=[[],choices()[:-1],choices()+[choices()[0]],choices()+[dict(text='无试看',inputs=[])]]
        for name in ['公开','立即发布','不同时发布']:
            value=choices();next(r for r in value if r['text']==name)['inputs'][0]['checked']=False;bad.append(value)
        value=choices();value[0]['inputs'][0]['checked']=1;bad.append(value)
        for value in bad:
            with self.assertRaises(PublishError):sender.selected_options(value)


class DouyinSendTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.path=self.root/'manifest.json';self.profile_path=self.root/'profile.json'
        self.profile=dict(configured_modules=['douyin'],browser_profile=str(self.root/'chrome'),
            browser_runtime=dict(cdp_url='http://127.0.0.1:9333'),
            accounts=dict(a=dict(platforms=['douyin'],public_identities=dict(douyin='fixture_123'))),
            adapters=dict(douyin=dict(enabled_capabilities=['video:publish'],
                execute=[sys.executable,str(ROOT/'scripts/browser_engine.py'),'execute','--profile',str(self.profile_path)],
                verify=[sys.executable,str(ROOT/'scripts/browser_engine.py'),'verify','--profile',str(self.profile_path)])))
        write_json(self.profile_path,self.profile);(self.root/'fixture.mp4').write_bytes(b'local sealed fixture')
        self.manifest=dict(schema_version=1,job_id='douyin-publication-offline',producer='files',content_status='final',
            source_root=str(self.root),documents=[],media_slots=[],assets=[dict(id='video',kind='video',path='fixture.mp4')],
            targets=[dict(id='douyin',platform='douyin',account='a',endpoint='publish',content_type='video',
                asset_ids=['video'],title='系列 S1E01｜样本',description='完整简介\n保留换行',
                links=[dict(label='资料',url='https://example.org/read')],
                platform_metadata=dict(ai_generated=True,collection_name='A collection'))])
        self.store=Store(self.root/'state');self.addCleanup(self.store.close);self.seal()
        self.page=Page();self.anchor=Page();self.anchor.ident='OriginalCreatorTarget';self.anchor.title='untouched author draft'
        self.actions=[];self.mode='ok';self.uid=UID;self.durable=[];self.addCleanup(patch.stopall)
        @contextlib.contextmanager
        def connect(profile,**kwargs):yield object()
        @contextlib.contextmanager
        def background(anchor,**kwargs):yield self.page
        @contextlib.contextmanager
        def identity(page):yield Identity(page)
        patch.object(home,'connect',side_effect=connect).start()
        patch.object(home,'select_page',return_value=self.anchor).start()
        patch.object(home,'background_tab',side_effect=identity).start()
        patch.object(sender,'background_window',side_effect=background).start()
        patch.object(sender,'native_account',side_effect=lambda *a:self.uid).start()
        patch.object(sender.time,'monotonic',side_effect=lambda:self.page.clock).start()
        patch('runtime.inspect',return_value=dict(ok=True,python='/isolated/python')).start()
        def invoke(config,action,path,*,lock_fd):
            self.actions.append(action);return engine.dispatch(path,action,self.profile,lock_fd)
        patch('publish_core.invoke',side_effect=invoke).start()
        def click(page,button,*,before_input):
            self.durable.append(dict(attempt=bool(read_json(self.marker).get('submission_attempted_at')),
                                     core_state=self.store.status(self.key)['state']))
            if self.mode=='change_at_input':page.description+='changed'
            if self.mode=='account_at_input':self.uid='999999999'
            before_input();page.clicks+=1
            self.durable[-1]['checkpoint']=bool(read_json(self.marker).get('native_input_checkpointed_at'))
            if self.mode=='no_receipt':return
            for listener in list(page.listeners):listener(response())
            self.durable[-1]['accepted']=read_json(self.marker).get('accepted',{}).get('remote_id')
            if self.mode=='duplicate':
                for listener in list(page.listeners):listener(response())
            if self.mode=='lost_reply':raise RuntimeError('click completion lost')
        patch.object(sender,'click_background',side_effect=click).start()
        def readback(*args,**kwargs):
            if self.mode=='reviewing':raise PublishError('not yet publicly reviewed')
            pack=build_pack(self.manifest,'douyin',self.profile,check_files=False)
            fields=sender.public_text(pack);details=dict(fields,remote_id=RID,native_user_id=UID,
                topics=home.topic_names(pack),declaration='内容由AI生成',collection=dict(id='456',name='A collection'),
                covers={},video=dict(vid='vFixtureNative1234567890',width=720,height=1280,duration_ms=4000))
            if self.mode=='wrong_caption':details['caption']+='changed'
            if self.mode=='wrong_video':details['video']['vid']='vOtherNative1234567890'
            return dict(remote_id=RID,native_user_id=UID,observed_account_identity='fixture_123',
                        description=fields['description'],native_review_status=2,native_visibility_status=1,details=details)
        patch.object(sender.post,'readback',side_effect=readback).start()

    def seal(self):
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'douyin')['key'];self.store.authorize(self.manifest,'douyin','offline candidate only')
        self.request=self.store.directory/'requests'/f'{self.key}.json';self.marker=home.marker_path(self.request)

    def test_core_send_receipt_and_independent_readback_then_resume_without_source_or_composer(self):
        result=run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(result['state'],'published');self.assertEqual(result['remote_id'],RID)
        self.assertEqual(self.durable,[dict(attempt=True,core_state='submission_unknown',checkpoint=True,accepted=RID)])
        self.assertEqual(self.page.clicks,1);self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.closes,1)
        self.assertEqual(self.anchor.title,'untouched author draft');self.assertEqual(self.anchor.uploads,[])
        (self.root/'fixture.mp4').unlink()
        self.assertEqual(run(self.path,'douyin',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.page.clicks,1);self.assertEqual(self.actions,['execute','verify','verify'])
        self.assertEqual(home.select_page.call_args.kwargs['target_id'],'OriginalCreatorTarget')

    def test_missing_native_upload_receipt_stops_before_send_and_never_reuploads(self):
        self.page.commit_response=None
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,0);self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.closes,0)

    def test_no_send_receipt_preserves_unknown_and_never_clicks_twice(self):
        self.mode='no_receipt'
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['state'],'submission_unknown')
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1);self.assertEqual(len(self.page.uploads),1)

    def test_accepted_but_review_unavailable_keeps_same_id_pending(self):
        self.mode='reviewing'
        result=run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(result['state'],'pending');self.assertEqual(result['remote_id'],RID)
        self.mode='ok';self.assertEqual(run(self.path,'douyin',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.page.clicks,1)

    def test_caption_changed_at_last_input_is_not_clicked(self):
        self.mode='change_at_input'
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)
        self.assertNotIn('native_input_checkpointed_at',read_json(self.marker))

    def test_account_changed_at_last_input_is_not_clicked(self):
        self.mode='account_at_input'
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)

    def test_native_private_or_scheduled_selection_is_not_modified_or_sent(self):
        self.page.rows[0]['inputs'][0]['checked']=False
        before=copy.deepcopy(self.page.rows)
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.clicks,0);self.assertEqual(self.page.rows,before)

    def test_resume_with_changed_native_author_does_not_query_another_work(self):
        run(self.path,'douyin',self.profile,self.store)
        sender.post.readback.reset_mock();self.uid='999999999'
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        sender.post.readback.assert_not_called();self.assertEqual(self.page.clicks,1)

    def test_resume_requires_durable_input_checkpoint_and_original_public_options(self):
        run(self.path,'douyin',self.profile,self.store)
        good=read_json(self.marker);sender.post.readback.reset_mock()
        cases=[dict(good,native_input_checkpointed_at=None),dict(good,publication_options={}),
               dict(good,native_input_checkpointed_at=True),
               dict(good,native_input_checkpointed_at=good['accepted']['observed_at']+1)]
        for saved in cases:
            write_json(self.marker,saved)
            with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        sender.post.readback.assert_not_called();self.assertEqual(self.page.clicks,1)

    def test_checkpoint_disk_failure_does_not_record_input_or_click(self):
        original=sender.write_json
        def write(path,value):
            if 'native_input_checkpointed_at' in value:raise OSError('disk full')
            return original(path,value)
        with patch.object(sender,'write_json',side_effect=write):
            with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.page.clicks,0);self.assertNotIn('native_input_checkpointed_at',read_json(self.marker))

    def test_lost_click_reply_with_durable_id_does_not_repeat_submission(self):
        self.mode='lost_reply';self.assertEqual(run(self.path,'douyin',self.profile,self.store)['state'],'published')
        self.assertEqual(self.page.clicks,1);self.assertEqual(read_json(self.marker)['accepted']['remote_id'],RID)

    def test_duplicate_native_receipt_keeps_id_but_refuses_verified_success(self):
        self.mode='duplicate'
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['remote_id'],RID)
        self.assertEqual(self.store.status(self.key)['state'],'pending');self.assertEqual(self.page.closes,0)

    def test_post_readback_cannot_accept_changed_caption_or_same_duration_other_vid(self):
        self.mode='wrong_caption'
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        self.mode='wrong_video'
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1);self.assertEqual(self.store.status(self.key)['remote_id'],RID)

    def test_unsupported_declaration_rejected_before_browser_or_upload(self):
        self.manifest['targets'][0]['platform_metadata'].update(ai_generated=False,declaration='无需添加自主声明');self.seal()
        with self.assertRaises(PublishError):run(self.path,'douyin',self.profile,self.store)
        sender.native_account.assert_not_called();self.assertEqual(self.page.uploads,[])

    def test_existing_attempt_is_not_reused_for_another_execute(self):
        run(self.path,'douyin',self.profile,self.store)
        with tempfile.TemporaryFile() as lock:
            with self.assertRaises(PublishError):sender.dispatch(self.request,'execute',self.profile,lock.fileno())
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.clicks,1)


if __name__=='__main__':unittest.main()
