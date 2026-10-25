import copy
import hashlib
import json
import struct
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
import test_browser_engine as base
import browser_engine as engine
import x_images
from platform_pack import build_pack
from publish_core import PublishError,read_json,write_json,run,validate


def png(i):
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(bytes((0,i,127,255))))+chunk(b'IEND',b'')


class Node(base.Node):
    def locator(self,selector):
        if 'fileInput' in selector:return Node(self.page,'file')
        return Node(self.page,'editor' if 'tweetTextarea' in selector else 'media')
    def evaluate(self,script):
        if script!=x_images.READ:raise AssertionError(script)
        if self.page.invalid_native or self.page.pending_files:raise RuntimeError('native attachments pending')
        return copy.deepcopy(self.page.images)
    def is_disabled(self):return self.page.file_disabled
    def get_attribute(self,name):
        if self.kind=='file':return {'multiple':'','accept':self.page.accept}.get(name)
        return super().get_attribute(name)
    def set_input_files(self,paths,**kwargs):
        self.page.uploads.append([Path(p).read_bytes() for p in paths]);self.page.images=[]
        for i,data in enumerate(self.page.uploads[-1]):
            self.page.images.append({'src':'blob:https://x.com/00000000-0000-4000-8000-'+str(i).zfill(12),
                'sha256':hashlib.sha256(data).hexdigest(),'size':len(data),'width':1,'height':1})
        self.page.media=len(self.page.images)
        if self.page.crash_after_upload:raise RuntimeError('reply lost after native accepted images')


class Page(base.Page):
    def __init__(self):
        super().__init__();self.images=[];self.uploads=[];self.invalid_native=False;self.file_disabled=False
        self.crash_after_upload=False;self.accept='image/jpeg,image/png,image/webp,video/mp4'
    def locator(self,selector):
        original=super().locator(selector);return Node(self,original.kind)
    def evaluate(self,script,arg=None):
        if script==x_images.DECODE:return {'width':1,'height':1}
        return {'outer':828,'inner':828,'hidden':True}
    def wait_for_function(self,*args,**kwargs):pass


class XImagesTests(unittest.TestCase):
    setUp_base=base.BrowserEngineTests.setUp
    request=base.BrowserEngineTests.request
    def setUp(self):
        self.setUp_base();self.page=Page()
        self.profile['adapters']['x']['enabled_capabilities'].append('images:prefill')
        write_json(self.profile_path,self.profile)
        self.manifest.update(job_id='x-images-test',assets=[])
        for i in range(2):
            (self.root/f'{i}.png').write_bytes(png(i))
            self.manifest['assets'].append({'id':'img'+str(i),'path':f'{i}.png','kind':'image'})
        self.manifest['targets'][0].update(content_type='images',asset_ids=['img0','img1'])
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'x')['key'];self.store.authorize(self.manifest,'x','offline images prefill')

    def pack(self):return build_pack(self.manifest,'x',self.profile)

    def test_actual_dispatch_two_images_and_readonly_resume(self):
        result=run(self.path,'x',self.profile,self.store)
        self.assertEqual(result['state'],'prefill_ready')
        observed=json.loads(self.store.db.execute('SELECT data FROM events WHERE job_key=? ORDER BY rowid DESC LIMIT 1',(self.key,)).fetchone()[0])
        self.assertEqual(observed['verified_asset_ids'],['img0','img1'])
        saved=read_json(engine.marker_path(self.request))
        self.assertEqual([x['source_asset_id'] for x in saved['images']],['img0','img1'])
        self.assertEqual(observed['media_tracking_scope'],'local_blob_preview_only')
        self.assertFalse(observed['platform_draft_saved']);self.assertFalse(observed['final_click_available'])
        run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(len(self.page.writes),1)

    def test_lost_attachment_reply_recovers_exact_bytes_without_upload(self):
        self.page.crash_after_upload=True
        with self.assertRaises(RuntimeError):run(self.path,'x',self.profile,self.store)
        self.page.crash_after_upload=False
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'prefill_ready')
        self.assertEqual(len(self.page.uploads),1)

    def test_missing_wrong_order_or_altered_source_never_ready(self):
        run(self.path,'x',self.profile,self.store);saved=read_json(engine.marker_path(self.request))
        original=copy.deepcopy(self.page.images)
        variants=[original[:1],list(reversed(original)),[dict(original[0],sha256='0'*64),original[1]],
                  [dict(original[0],src='https://example.org/image.png'),original[1]]]
        for values in variants:
            self.page.images=values
            with self.assertRaises(PublishError):x_images.verify(self.page,self.pack(),saved)
        self.assertEqual(len(self.page.uploads),1)

    def test_replaced_blob_even_same_image_is_not_original_receipt(self):
        run(self.path,'x',self.profile,self.store)
        self.page.images[0]['src']='blob:https://x.com/99999999-0000-4000-8000-000000000000'
        with self.assertRaisesRegex(PublishError,'身份已改变'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_pending_or_unknown_native_attachment_rejected(self):
        run(self.path,'x',self.profile,self.store);self.page.invalid_native=True
        with self.assertRaisesRegex(PublishError,'预览尚未完整'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_changed_original_source_stops_before_fill_or_upload(self):
        (self.root/'1.png').write_bytes(png(100))
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.writes,[])

    def test_changed_snapshot_at_boundary_never_uploaded(self):
        original=x_images.prepare
        def change(page,assets):
            original(page,assets);Path(assets[-1]['absolute_path']).chmod(0o600);Path(assets[-1]['absolute_path']).write_bytes(png(44))
        with patch.object(x_images,'prepare',side_effect=change):
            with self.assertRaisesRegex(PublishError,'快照'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.uploads,[])

    def test_existing_media_preserved_before_text_mutation(self):
        self.page.media=1
        with self.assertRaisesRegex(PublishError,'已有媒体'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[]);self.assertEqual(self.page.uploads,[])

    def test_decode_failure_before_any_fill_or_upload(self):
        original=self.page.evaluate
        def bad(script,arg=None):
            if script==x_images.DECODE:return dict(width=0,height=1)
            return original(script,arg)
        self.page.evaluate=bad
        with self.assertRaisesRegex(PublishError,'无法解码'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[]);self.assertEqual(self.page.uploads,[])

    def test_empty_extra_or_unsupported_assets_never_silently_dropped(self):
        pack=self.pack()
        for assets in ([],pack['assets']*3,[dict(pack['assets'][0],kind='video')],
                       [dict(pack['assets'][0],path='animation.gif')]):
            invalid=copy.deepcopy(pack);invalid['assets']=assets
            with self.assertRaises(PublishError):engine.check_pack(invalid)

    def test_disguised_gif_and_apng_rejected_before_decode(self):
        for data in (b'GIF89a'+b'0'*50,b'\x89PNG\r\n\x1a\nacTL'+b'0'*50):
            path=self.root/'disguised.png';path.write_bytes(data)
            with self.assertRaises(PublishError):x_images.prepare(self.page,[dict(id='bad',path='disguised.png',absolute_path=str(path),sha256=hashlib.sha256(data).hexdigest())])

    def test_animation_names_inside_static_payload_are_not_animation_chunks(self):
        def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        data=png(1);static=data[:-12]+chunk(b'tEXt',b'label\x00acTL is ordinary text')+data[-12:]
        self.assertTrue(x_images.static_image(static,'.png'))
        animated=data[:-12]+chunk(b'acTL',struct.pack('>II',2,0))+data[-12:]
        self.assertFalse(x_images.static_image(animated,'.png'))
        def webp(kind,payload):
            body=kind+len(payload).to_bytes(4,'little')+payload+(b'\x00' if len(payload)%2 else b'')
            return b'RIFF'+(len(body)+4).to_bytes(4,'little')+b'WEBP'+body
        self.assertTrue(x_images.static_image(webp(b'VP8L',b'ANIM'),'.webp'))
        self.assertFalse(x_images.static_image(webp(b'ANIM',b'00'),'.webp'))
        self.assertFalse(x_images.static_image(webp(b'VP8X',b'\x02'+b'0'*9),'.webp'))
        for invalid in (b'',b'RIFF',data[:-1]):
            self.assertFalse(x_images.static_image(invalid,'.png'))


if __name__=='__main__':unittest.main()
