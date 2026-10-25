"""Core/adapter orchestration; native primitives have separate tests and live evidence."""
import contextlib
import copy
from pathlib import Path
import unittest
from unittest.mock import patch
import test_weibo_browser as base
from browser_assets import checked_path
import weibo_video_browser as engine
from publish_core import PublishError,read_json,write_json,run,validate


class Page(base.Page):
    def __init__(self):
        super().__init__();self.selected=[];self.filled=0;self.document=None;self.video=False
        self.lose_after_select=False;self.lose_after_fill=False;self.listeners=[];self.pending=False
    def goto(self,url,**kwargs):self.url=url
    def on(self,event,callback):self.listeners.append(callback)
    def remove_listener(self,event,callback):self.listeners.remove(callback)
    def evaluate(self,script):return self.video
    def locator(self,selector):
        owner=self
        class Empty:
            def wait_for(self,**kwargs):pass
            def evaluate_all(self,*args):return not owner.pending
            def all(self):return []
        return Empty()


class Dispatch(unittest.TestCase):
    request=base.WeiboTests.request
    def setUp(self):
        base.WeiboTests.setUp(self)
        self.profile['adapters']['weibo']['enabled_capabilities']=['video:prefill'];write_json(self.profile_path,self.profile)
        self.page=Page();self.anchor=Page();self.anchor.url='https://weibo.com/';self.anchor.text='existing author draft'
        self.windows=[];self.connections=[]
        @contextlib.contextmanager
        def connect(profile,**kwargs):self.connections.append(kwargs['writing']);yield object()
        @contextlib.contextmanager
        def window(anchor,**kwargs):
            self.assertIs(anchor,self.anchor);self.assertTrue(kwargs['retain']);self.windows.append(self.page);yield self.page
        def select(connection,platform,*,path,target_id=None):
            if path=='/':return self.anchor
            if target_id!=self.page.ident:raise PublishError('原会话丢失')
            return self.page
        def identity(page,expected):
            if page.uid!=expected:raise PublishError('UID 不符')
        def select_file(page,asset,checkpoint):
            checkpoint('before_file_selection',{'source_size':Path(checked_path(asset)).stat().st_size})
            page.selected.append(checked_path(asset));page.video=True
            checkpoint('selected_once',{'source_sha256':asset['sha256']})
            if page.lose_after_select:raise RuntimeError('lost after selection')
        def observe_video(page,asset,saved,**kwargs):
            checked_path(asset)
            if not page.video:raise PublishError('视频已变')
            return dict(media_id='12345678',source_sha256=asset['sha256'])
        def fill(page,pack,checkpoint):
            if page.document is not None:raise PublishError('已有作者文案')
            page.document=copy.deepcopy(pack['fields']);page.filled+=1
            if page.lose_after_fill:raise RuntimeError('lost after fill')
        def observe_fields(page,pack,**kwargs):
            if page.document!=pack['fields']:raise PublishError('文案不符')
            return page.document
        for owner,name,fn in [(engine,'connect',connect),(engine,'background_window',window),(engine,'select_page',select),
                              (engine.home,'assert_identity',identity),(engine.video,'assert_identity',identity),
                              (engine.video,'select_once',select_file),(engine.video,'observe_source',observe_video),
                              (engine.fields,'fill',fill),(engine.fields,'observe',observe_fields)]:
            patch.object(owner,name,side_effect=fn).start()
        source=self.root/'video.mp4';source.write_bytes(b'\0\0\0\x18ftypmp42'+bytes(100))
        self.manifest['assets']=[dict(id='video',kind='video',path='video.mp4')]
        self.manifest['targets'][0].update(content_type='video',asset_ids=['video'],title='完整标题',
            platform_metadata=dict(creation_type='原创',declaration='内容为自主创作',ai_generated=False))
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'weibo')['key'];self.store.authorize(self.manifest,'weibo','offline prefill')

    def test_unified_core_and_resume_use_same_target_without_another_upload(self):
        self.assertEqual(run(self.path,'weibo',self.profile,self.store)['state'],'prefill_ready')
        saved=read_json(engine.home.marker_path(self.request));self.assertEqual(saved['state'],'filled')
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'prefill_ready')
        self.assertEqual(len(self.page.selected),1);self.assertEqual(self.page.filled,1)
        self.assertEqual(len(self.windows),1);self.assertEqual(self.anchor.text,'existing author draft')
        self.assertEqual(self.connections,[True,False,False])

    def test_lost_reply_after_fill_recovers_without_writing(self):
        self.page.lose_after_fill=True
        with self.assertRaises(RuntimeError):run(self.path,'weibo',self.profile,self.store)
        self.page.lose_after_fill=False
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'prefill_ready')
        self.assertEqual(len(self.page.selected),1);self.assertEqual(self.page.filled,1)

    def test_incomplete_upload_attempt_is_not_replayed_or_filled_on_resume(self):
        self.page.lose_after_select=True
        with self.assertRaises(RuntimeError):run(self.path,'weibo',self.profile,self.store)
        self.page.lose_after_select=False
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.selected),1);self.assertEqual(self.page.filled,0)
        with (self.root/'lock').open('w') as lock:
            with self.assertRaisesRegex(PublishError,'已有尝试'):engine.dispatch(self.request,'execute',self.profile,lock.fileno())

    def test_changed_title_or_account_invalidates_previous_ready(self):
        run(self.path,'weibo',self.profile,self.store)
        self.page.document['title']='author edit'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.page.uid='987654321'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.selected),1)

    def test_deleted_original_can_resume_from_immutable_owned_snapshot(self):
        run(self.path,'weibo',self.profile,self.store);(self.root/'video.mp4').unlink()
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'prefill_ready')
        snapshot=Path(self.page.selected[0]);snapshot.chmod(0o600);snapshot.write_bytes(b'changed snapshot')
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.selected),1)

    def test_new_pending_file_invalidates_previous_ready_without_reupload(self):
        run(self.path,'weibo',self.profile,self.store);self.page.pending=True
        with self.assertRaisesRegex(PublishError,'新增待处理文件'):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.selected),1)

    def test_incomplete_native_cover_is_pending_and_never_ready_or_reuploaded(self):
        # Create a real core attempt first; simulate interruption BEFORE cover completion.
        source=self.root/'cover.png';source.write_bytes(b'cover')
        self.manifest['assets'].append(dict(id='cover',kind='image',path='cover.png'))
        target=self.manifest['targets'][0];target['asset_ids']=['video','cover'];target['cover_asset_id']='cover'
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'weibo')['key'];self.store.authorize(self.manifest,'weibo','offline prefill')
        prepared=dict(width=1600,height=900)
        def stop(page,asset,prepared,checkpoint):
            checkpoint('cover_selected',{'prepared':prepared,'requests':[],'responses':[],'errors':[]})
            raise PublishError('native crop requires correction')
        meta=dict(width=1920,height=1080,duration=4.0)
        with patch.object(engine.video_probe,'inspect',return_value=meta),\
             patch.object(engine.cover,'prepare',return_value=prepared),\
             patch.object(engine.cover,'check_video_ratio'),\
             patch.object(engine.cover,'upload',side_effect=stop):
            native=dict(meta,media_id='12345678')
            with patch.object(engine.video,'observe_source',return_value=native):
                with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
                marker=engine.home.marker_path(self.request);before=marker.read_bytes()
                with patch.object(engine.cover,'incomplete',return_value=dict(stage='cover_editing_required')):
                    result=run(self.path,'weibo',self.profile,self.store,resume=True)
                    self.assertEqual(result['state'],'pending')
                    self.assertEqual(self.store.db.execute('SELECT count(*) FROM account_locks WHERE job_key=?',(self.key,)).fetchone()[0],0)
                    native['media_id']='87654321'
                    with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
                    native['media_id']='12345678'
                    self.page.pending=True
                    with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
                    self.page.pending=False
                    self.page.document['title']='other author edit'
                    with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
                self.assertEqual(marker.read_bytes(),before)
                self.assertEqual(len(self.page.selected),1);self.assertEqual(self.page.filled,1)

    def test_wrong_cover_direction_rejects_before_native_platform_upload(self):
        source=self.root/'cover.png';source.write_bytes(b'cover')
        self.manifest['assets'].append(dict(id='cover',kind='image',path='cover.png'))
        target=self.manifest['targets'][0];target['asset_ids']=['video','cover'];target['cover_asset_id']='cover'
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'weibo')['key'];self.store.authorize(self.manifest,'weibo','offline prefill')
        meta=dict(width=720,height=1280,duration=4.0)
        with patch.object(engine.video_probe,'inspect',return_value=meta),\
             patch.object(engine.video,'observe_source',return_value=dict(meta,media_id='12345678')),\
             patch.object(engine.cover,'prepare',return_value=dict(width=1600,height=900)),\
             patch.object(engine.cover,'upload',return_value={}),patch.object(engine.cover,'observe'):
            with self.assertRaisesRegex(PublishError,'方向不符'):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.selected,[]);self.assertEqual(self.page.filled,0)


if __name__=='__main__':unittest.main()
