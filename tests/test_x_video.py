import contextlib
import copy
import hashlib
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import test_browser_engine as base
import browser_engine as engine
import x_video
import x_video_browser
from platform_pack import build_pack
from publish_core import PublishError,read_json,write_json,run,validate


def events(size,stamp=1000):
    body={'media_id_string':'12345','media_key':'7_12345','expires_after_secs':600}
    final=dict(body,size=size,processing_info={'state':'pending','check_after_secs':1})
    ready=dict(body,size=size,processing_info={'state':'succeeded','progress_percent':100},video={'video_type':'video/mp4'})
    return [dict(command=c,status=s,observed_at=stamp+i,**({'body':b} if b else {}))
            for i,(c,s,b) in enumerate([('INIT',202,body),('APPENDMULTI',204,None),
                                      ('FINALIZE',200,final),('STATUS',200,ready)])]


class CloudReceiptTests(unittest.TestCase):
    def test_native_success_with_immutable_identity_size_and_expiry(self):
        value=x_video.cloud_receipt(events(100),100,now=1005)
        self.assertEqual(value,dict(media_id='12345',media_key='7_12345',size=100,expires_at=1600,processing='succeeded'))
    def test_missing_empty_failed_foreign_or_expired_receipt_is_rejected(self):
        for values in (None,[],events(100)[:-1],events(100)[1:]):
            with self.assertRaises(PublishError):x_video.cloud_receipt(values,100,now=1005)
        changes=[('media_id_string','99999'),('media_key','7_999'),('size',99),('size',True),
                 ('expires_after_secs',0),('processing_info',{'state':'failed'}),
                 ('processing_info',{'state':'pending'}),('video',{'video_type':'image/jpeg'}),('errors',[{'code':1}])]
        for field,value in changes:
            data=events(100);data[-1]['body'][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(PublishError):
                x_video.cloud_receipt(data,100,now=1005)
        with self.assertRaises(PublishError):x_video.cloud_receipt(events(100),100,now=1600)
    def test_extra_init_wrong_order_bad_status_future_or_missing_append_is_rejected(self):
        original=events(100)
        variants=[original[:1]+original,original[:1]+original[2:],list(reversed(original))]
        for changed in (dict(command='APPENDMULTI',status=500,observed_at=1001),
                        dict(command='OTHER',status=204,observed_at=1001),
                        dict(command='APPENDMULTI',status=204,observed_at=2000)):
            value=copy.deepcopy(original);value[1]=changed;variants.append(value)
        for value in variants:
            with self.assertRaises(PublishError):x_video.cloud_receipt(value,100,now=1005)
    def test_capture_filters_native_origin_records_each_response_and_keeps_failure(self):
        checkpoints=[];capture=x_video.Capture(lambda e,x:checkpoints.append(copy.deepcopy((e,x))))
        class Response:
            url='https://upload.x.com/i/media/upload2.json?command=INIT';status=202
            def json(self):return dict(media_id_string='12345',media_key='7_12345',expires_after_secs=600,secret='not retained')
        capture.response(Response());self.assertEqual(len(checkpoints),1)
        self.assertNotIn('secret',capture.events[0]['body'])
        for url in ('https://evil.test/i/media/upload2.json?command=INIT',
                    'https://upload.x.com.evil.test/i/media/upload2.json?command=INIT'):
            r=Response();r.url=url;capture.response(r)
        self.assertEqual(len(capture.events),1)
        r=Response();r.url+='&command=STATUS';capture.response(r)
        self.assertTrue(capture.errors)
    def test_callback_persistence_failure_cannot_silently_succeed(self):
        def fail(*args):raise OSError('disk full')
        capture=x_video.Capture(fail)
        class Response:
            url='https://upload.x.com/i/media/upload2.json?command=APPENDMULTI';status=204
        capture.response(Response());self.assertEqual(capture.errors,['native_upload_response_unrecorded'])


class Node(base.Node):
    def count(self):
        if self.kind=='file':return 1
        return super().count()
    def locator(self,selector):
        if 'fileInput' in selector:return Node(self.page,'file')
        return super().locator(selector)
    def wait_for(self,**kwargs):pass
    def is_disabled(self):return False
    def get_attribute(self,name):
        if self.kind=='file':return 'image/jpeg,image/png,video/mp4' if name=='accept' else None
        return super().get_attribute(name)
    def evaluate(self,script):
        if script!=x_video.READ:raise AssertionError(script)
        return copy.deepcopy(self.page.video)
    def set_input_files(self,path,**kwargs):
        data=Path(path).read_bytes();self.page.uploads.append(data);self.page.media=1
        self.page.video=dict(src='blob:https://x.com/00000000-0000-4000-8000-000000000001',
            sha256=hashlib.sha256(data).hexdigest(),size=len(data),width=720,height=1280,duration=4,ready=4)
        for event in events(len(data),time.time()-5):
            class Response:
                url='https://upload.x.com/i/media/upload2.json?command='+event['command']
                status=event['status']
                def json(self):return event.get('body')
            for callback in list(self.page.listeners):callback(Response())
        if self.page.crash_after_upload:raise RuntimeError('lost after native upload')


class Page(base.Page):
    def __init__(self):
        super().__init__();self.uploads=[];self.video=None;self.listeners=[];self.crash_after_upload=False
    def goto(self,url,**kwargs):self.url=url
    def on(self,event,callback):self.listeners.append(callback)
    def remove_listener(self,event,callback):self.listeners.remove(callback)
    def wait_for_function(self,*args,**kwargs):pass
    def locator(self,selector):
        return Node(self,'file' if 'fileInput' in selector else 'editor' if 'tweetTextarea' in selector else
                    'switch' if 'AccountSwitcher' in selector else 'link' if 'Profile_Link' in selector else
                    'modal' if 'aria-modal' in selector else 'alert')


class XVideoDispatchTests(unittest.TestCase):
    request=base.BrowserEngineTests.request
    def setUp(self):
        base.BrowserEngineTests.setUp(self)
        self.profile['adapters']['x']['enabled_capabilities']=['video:prefill'];write_json(self.profile_path,self.profile)
        self.page=Page();self.anchor=base.Page();self.anchor.url='https://x.com/home';self.anchor.text='existing author draft'
        self.windows=[]
        @contextlib.contextmanager
        def window(anchor,**kwargs):
            self.assertIs(anchor,self.anchor);self.assertTrue(kwargs['retain']);self.windows.append(self.page)
            yield self.page
        def select(connection,platform,*,path,target_id=None):
            if path=='/home':return self.anchor
            if target_id!=self.page.context.ident:raise PublishError('会话丢失')
            return self.page
        patch.object(x_video_browser,'background_window',side_effect=window).start()
        patch.object(engine,'select_page',side_effect=select).start()
        source=self.root/'video.mp4';source.write_bytes(b'\0\0\0\x18ftypmp42' + bytes(100))
        self.manifest['assets']=[{'id':'video','path':'video.mp4','kind':'video'}]
        self.manifest['targets'][0].update(content_type='video',asset_ids=['video'])
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'x')['key'];self.store.authorize(self.manifest,'x','offline video prefill')
    def test_unified_prefill_cloud_receipt_and_readonly_resume_never_reupload(self):
        self.assertEqual(run(self.path,'x',self.profile,self.store)['state'],'prefill_ready')
        saved=read_json(engine.marker_path(self.request))
        self.assertEqual(saved['cloud_video']['processing'],'succeeded')
        self.assertEqual(saved['native_video']['source_asset_id'],'video')
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'prefill_ready')
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(len(self.page.writes),1)
        self.assertEqual(len(self.windows),1);self.assertEqual(self.anchor.text,'existing author draft')
    def test_lost_fill_reply_restores_same_editor_without_native_selection(self):
        self.page.crash_after_fill=True
        with self.assertRaises(RuntimeError):run(self.path,'x',self.profile,self.store)
        self.page.crash_after_fill=False
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'prefill_ready')
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(len(self.page.writes),1)
    def test_upload_without_text_is_unknown_and_resume_does_not_fill(self):
        self.page.crash_after_upload=True
        with self.assertRaises(RuntimeError):run(self.path,'x',self.profile,self.store)
        self.page.crash_after_upload=False
        with self.assertRaisesRegex(PublishError,'文案已变'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.writes,[])
    def test_expired_or_lost_receipts_never_reupload_or_report_ready(self):
        run(self.path,'x',self.profile,self.store);original=read_json(engine.marker_path(self.request))
        variants=[dict(original,upload_events=[]),dict(original,upload_errors=['lost']),
                  dict(original,cloud_video=dict(original['cloud_video'],media_id='999'))]
        old=copy.deepcopy(original)
        for e in old['upload_events']:e['observed_at']-=10000
        variants.append(old)
        for value in variants:
            write_json(engine.marker_path(self.request),value)
            with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)
    def test_cloud_transcript_and_exact_preview_recover_missing_summary_receipt(self):
        run(self.path,'x',self.profile,self.store)
        saved=read_json(engine.marker_path(self.request))
        del saved['native_video'];del saved['cloud_video']
        write_json(engine.marker_path(self.request),saved)
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'prefill_ready')
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(len(self.page.writes),1)
    def test_native_hash_blob_size_duration_or_identity_changes_fail_readonly(self):
        run(self.path,'x',self.profile,self.store);original=copy.deepcopy(self.page.video)
        for field,value in [('sha256','0'*64),('size',1),('src','https://example.org/video.mp4'),
                            ('duration',float('nan')),('width',0),('ready',0)]:
            self.page.video=dict(original,**{field:value})
            with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.page.video=original;self.page.context.ident='other'
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)
    def test_second_execute_rejected_and_wrong_account_before_selection(self):
        self.page.handle='@other'
        with self.assertRaisesRegex(PublishError,'账号与任务绑定'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.uploads,[])
        with (self.root/'lock').open('w') as lock:
            with self.assertRaisesRegex(PublishError,'已尝试'):
                engine.dispatch(self.request,'execute',self.profile,lock.fileno())
    def test_unmapped_cover_or_extra_assets_rejected(self):
        pack=build_pack(self.manifest,'x',self.profile)
        for changed in (dict(pack,assets=[]),dict(pack,assets=pack['assets']*2),
                        dict(pack,platform_metadata={'ai_generated':True}),dict(pack,endpoint='publish')):
            with self.assertRaises(PublishError):engine.check_pack(changed)
        self.assertEqual(self.page.uploads,[])


if __name__=='__main__':unittest.main()
