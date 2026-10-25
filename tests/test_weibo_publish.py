"""Exercise opt-in Weibo sender via the real core account lock and same-ID reader."""
import contextlib
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import browser_engine as engine
import weibo_browser as home
import weibo_post as post
import weibo_publish_browser as publisher
from publish_core import PublishError,Store,read_json,run,validate,write_json
from test_weibo_browser import Node as HomeNode,Page as HomePage
from test_weibo_post import body,Page as DetailPage,UID

TEXT='完整文案\n保留换行'


class Public:
    def __init__(self,page):self.page=page
    def count(self):return 1
    def is_visible(self):return True
    def inner_text(self):return self.page.privacy


class Button(Public):
    def is_disabled(self):return self.page.disabled


class Node(HomeNode):
    def locator(self,selector):
        if selector=='[title="公开"]':return Public(self.page)
        return super().locator(selector)
    def get_by_role(self,role,**kwargs):
        assert role=='button' and kwargs==dict(name='发送',exact=True)
        return Button(self.page)


class Composer(HomePage):
    def __init__(self):
        super().__init__();self.uid=UID;self.listeners={};self.clicks=0;self.privacy='公开';self.disabled=False;self.closes=0
    def locator(self,selector):
        return Node(self,'profile' if selector.startswith('a:has') else 'card' if 'homeWrap' in selector else 'alert')
    def goto(self,url,**kw):self.url=url
    def close(self):self.closes+=1
    def on(self,event,fn):self.listeners[event]=fn
    def remove_listener(self,event,fn):self.listeners.pop(event)
    def wait_for_timeout(self,ms):pass


class Response:
    def __init__(self,result,**kw):
        self.body=dict(ok=1,data=result);self.url='https://weibo.com/ajax/statuses/update'
        self.status=200;self.request=type('Request',(),dict(method='POST'))()
        self.__dict__.update(kw)
    def json(self):return self.body


class WeiboSendTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.profile_path=self.root/'profile.json';self.path=self.root/'manifest.json'
        self.profile=dict(configured_modules=['weibo'],browser_profile=str(self.root/'dedicated'),
            browser_runtime=dict(cdp_url='http://127.0.0.1:9333'),
            accounts=dict(author=dict(platforms=['weibo'],public_identities=dict(weibo=UID))),
            adapters=dict(weibo=dict(enabled_capabilities=['text:publish','images:publish'],
                execute=[sys.executable,str(ROOT/'scripts/browser_engine.py'),'execute','--profile',str(self.profile_path)],
                verify=[sys.executable,str(ROOT/'scripts/browser_engine.py'),'verify','--profile',str(self.profile_path)])))
        write_json(self.profile_path,self.profile)
        for i in range(2):(self.root/f'{i}.png').write_bytes(b'fixture'+str(i).encode())
        self.manifest=dict(schema_version=1,job_id='weibo-publication-fixture',producer='files',content_status='final',
            source_root=str(self.root),documents=[],media_slots=[],
            assets=[dict(id='image'+str(i),kind='image',path=f'{i}.png') for i in range(2)],
            targets=[dict(id='weibo',platform='weibo',account='author',endpoint='publish',content_type='images',
                asset_ids=['image0','image1'],text=TEXT)])
        self.store=Store(self.root/'state');self.addCleanup(self.store.close);self.seal()
        self.page=Composer();self.anchor=Composer();self.anchor.ident='OriginalHomeTarget';self.anchor.text='author home draft'
        self.detail=DetailPage(body(TEXT,['remotePicture1','remotePicture2']))
        self.actions=[];self.mode='ok';self.observations=[]
        self.addCleanup(patch.stopall)
        @contextlib.contextmanager
        def connection(profile,**kw):yield object()
        @contextlib.contextmanager
        def compose(anchor,**kw):yield self.page
        @contextlib.contextmanager
        def detail(anchor,**kw):yield self.detail
        patch.object(home,'connect',side_effect=connection).start()
        patch.object(home,'select_page',return_value=self.anchor).start()
        patch.object(publisher,'background_window',side_effect=compose).start()
        patch.object(post,'background_window',side_effect=detail).start()
        patch('runtime.inspect',return_value=dict(ok=True,python='/isolated/python')).start()
        def invoke(config,action,path,*,lock_fd):
            self.actions.append(action);return engine.dispatch(path,action,self.profile,lock_fd)
        patch('publish_core.invoke',side_effect=invoke).start()
        def click(page,button,*,before_input=None):
            before=read_json(home.marker_path(self.request));self.observations.append(dict(
                durable_attempt=bool(before.get('submission_attempted_at')),core_state=self.store.status(self.key)['state']))
            if self.mode=='change_at_input':page.text='changed after geometric checks'
            before_input();self.observations[-1]['checkpoint']=bool(read_json(home.marker_path(self.request)).get('native_input_checkpointed_at'))
            page.clicks+=1
            if self.mode=='no_receipt':return
            result=body(page.text,[Path(v['path']).stem for v in page.media])
            if self.mode=='wrong_account':result['user']=dict(id=9999999990,idstr='9999999990')
            page.listeners['response'](Response(result))
            self.observations[-1]['accepted_id']=read_json(home.marker_path(self.request)).get('accepted',{}).get('remote_id')
            if self.mode=='duplicate':page.listeners['response'](Response(result))
            if self.mode=='lost_reply':raise RuntimeError('native click reply lost')
        patch.object(publisher,'click_background',side_effect=click).start()

    def seal(self):
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'weibo')['key'];self.store.authorize(self.manifest,'weibo','offline fixture')
        self.request=self.store.directory/'requests'/f'{self.key}.json'

    def test_images_sent_once_same_id_readback_and_sourceless_composer_closed_resume(self):
        result=run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(result['state'],'published');self.assertEqual(result['remote_id'],'9876543210')
        self.assertEqual(self.observations,[dict(durable_attempt=True,core_state='submission_unknown',checkpoint=True,accepted_id='9876543210')])
        self.assertEqual(self.anchor.text,'author home draft');self.assertEqual(self.anchor.fills,0)
        self.assertEqual(self.page.fills,1);self.assertEqual(len(self.page.uploads),2);self.assertEqual(self.page.closes,1)
        (self.root/'0.png').unlink();(self.root/'1.png').unlink()
        self.page.locator=lambda _: (_ for _ in ()).throw(AssertionError('closed composer must not be read'))
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.page.clicks,1);self.assertEqual(self.actions,['execute','verify','verify'])
        self.assertEqual(home.select_page.call_args.kwargs['target_id'],'OriginalHomeTarget')

    def test_text_has_no_upload_and_lost_click_reply_keeps_native_id(self):
        (self.root/'source.txt').write_text(TEXT,encoding='utf-8')
        self.manifest['assets']=[];self.manifest['documents']=[dict(id='source',path='source.txt',format='text')]
        self.manifest['targets'][0].update(content_type='text',asset_ids=[],document_id='source');self.seal()
        self.detail=DetailPage(body(TEXT));self.mode='lost_reply'
        self.assertEqual(run(self.path,'weibo',self.profile,self.store)['state'],'published')
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.clicks,1)
        self.assertEqual(read_json(home.marker_path(self.request))['accepted']['remote_id'],'9876543210')

    def test_unknown_without_receipt_does_not_refill_upload_or_click_on_resume(self):
        self.mode='no_receipt'
        with patch.object(publisher.time,'monotonic',side_effect=[0,21]):
            with self.assertRaisesRegex(PublishError,'结果未知'):run(self.path,'weibo',self.profile,self.store)
        with self.assertRaisesRegex(PublishError,'受理身份'):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1);self.assertEqual(len(self.page.uploads),2)
        self.assertEqual(self.page.fills,1);self.assertEqual(self.page.closes,0)

    def test_bad_readback_preserves_actual_id_then_only_queries_after_correction(self):
        self.detail.body=body('different',['remotePicture1','remotePicture2'])
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['remote_id'],'9876543210')
        self.assertEqual(self.store.status(self.key)['state'],'pending')
        self.detail.body=body(TEXT,['remotePicture1','remotePicture2'])
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.page.clicks,1);self.assertEqual(len(self.page.uploads),2)

    def test_wrong_visible_post_or_picture_order_never_reports_published(self):
        self.detail.html='truncated visible body'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.detail.html=body(TEXT)['text'];self.detail.pictures.reverse()
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_durable_attempt_failure_stops_before_click_function(self):
        def persist(path,value):
            if value.get('submission_attempted_at'):raise OSError('write failed before attempt')
            write_json(path,value)
        with patch.object(publisher,'write_json',side_effect=persist),self.assertRaises(OSError):
            run(self.path,'weibo',self.profile,self.store)
        publisher.click_background.assert_not_called();self.assertEqual(self.page.clicks,0)

    def test_checkpoint_write_failure_has_no_native_input(self):
        def persist(path,value):
            if value.get('native_input_checkpointed_at'):raise OSError('checkpoint write failed')
            write_json(path,value)
        with patch.object(publisher,'write_json',side_effect=persist):
            try:run(self.path,'weibo',self.profile,self.store)
            except (OSError,PublishError):pass
        self.assertEqual(self.page.clicks,0)
        self.assertEqual(read_json(home.marker_path(self.request))['click_error_phase'],'before_native_input')

    def test_content_changes_at_last_input_guard_stops_without_native_click(self):
        self.mode='change_at_input'
        with self.assertRaisesRegex(PublishError,'点击前检查'):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),2)

    def test_nonpublic_or_existing_composer_never_sends(self):
        self.page.privacy='仅自己可见'
        with self.assertRaisesRegex(PublishError,'公开可见性'):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)

    def test_existing_draft_stops_before_fill(self):
        self.page.text='retained author draft'
        with self.assertRaisesRegex(PublishError,'已有文案'):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.fills,0);self.assertEqual(self.page.clicks,0);self.assertEqual(self.page.uploads,[])

    def test_wrong_live_account_stops_before_owned_window_or_fill(self):
        self.anchor.uid='9999999990'
        with self.assertRaisesRegex(PublishError,'UID'):run(self.path,'weibo',self.profile,self.store)
        publisher.background_window.assert_not_called()
        self.assertEqual(self.page.fills,0);self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.clicks,0)

    def test_default_prefill_capability_never_enables_publish(self):
        self.profile['adapters']['weibo']['enabled_capabilities']=['text:prefill','images:prefill']
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        home.connect.assert_not_called();self.assertEqual(self.page.clicks,0);self.assertEqual(self.page.uploads,[])

    def test_duplicate_native_response_keeps_first_id_but_refuses_success(self):
        self.mode='duplicate'
        with self.assertRaisesRegex(PublishError,'受理身份'):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['remote_id'],'9876543210')
        self.assertEqual(self.store.status(self.key)['state'],'pending');self.assertEqual(self.page.clicks,1);self.assertEqual(self.page.closes,0)

    def test_wrong_account_native_receipt_never_binds_a_post(self):
        self.mode='wrong_account'
        with self.assertRaisesRegex(PublishError,'结果未知'):run(self.path,'weibo',self.profile,self.store)
        self.assertIsNone(self.store.status(self.key)['remote_id']);self.assertEqual(self.page.clicks,1)


class WeiboReceiptTests(unittest.TestCase):
    def test_only_exact_native_acceptance_stores_identity_without_content_claim(self):
        observed=[];capture=publisher.Capture(UID,observed.append);result=body();del result['text_raw']
        capture.response(Response(result));self.assertEqual(observed[0]['remote_id'],'9876543210')
        self.assertNotIn('content_verified',observed[0]);self.assertIsNone(capture.error)
        capture.response(Response(result));self.assertIsNotNone(capture.error);self.assertEqual(len(observed),1)

    def test_error_verification_status_wrong_method_or_missing_id_never_accept(self):
        for mode in ['error','verify','status','method','missing_id']:
            r=Response(body())
            if mode=='error':r.body['error']='not accepted'
            elif mode=='verify':r.body['data']['verify']=True
            elif mode=='status':r.status=403
            elif mode=='method':r.request=type('R',(),dict(method='GET'))()
            elif mode=='missing_id':r.body['data'].pop('idstr')
            seen=[];capture=publisher.Capture(UID,seen.append);capture.response(r)
            with self.subTest(mode=mode):self.assertEqual(seen,[]);self.assertIsNotNone(capture.error)
        r=Response(body(),url='https://other.example/ajax/statuses/update');capture=publisher.Capture(UID,lambda _:self.fail('foreign response'))
        capture.response(r);self.assertEqual(capture.count,0)


if __name__=='__main__':unittest.main()
