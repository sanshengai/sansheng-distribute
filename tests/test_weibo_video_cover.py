import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import PublishError
import weibo_video_cover as cover


class Cover(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        source=Path(self.temp.name)/'cover.png';source.write_bytes(b'fixture')
        self.asset=dict(id='cover',absolute_path=str(source),sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        self.prepared=dict(width=1600,height=900,source_asset_id='cover',source_sha256=self.asset['sha256'],source_size=7)
        self.crop=dict(source_sha256=self.asset['sha256'],source_size=7,width=1600,height=900,aspect_ratio=16/9,
            crop=dict(x=0,y=0,width=1600,height=900,rotate=0,scaleX=1,scaleY=1),native_size=100,native_sha256='a'*64)
        self.saved=dict(prepared=self.prepared,crop=self.crop,errors=[],requests=[dict(size=100,sha256='a'*64,content_type='application/octet-stream')],
            responses=[dict(status=200,ret=True,pid='NativeCoverPid123')])

    def test_decoded_cover_requires_explicit_16_by_9_before_upload(self):
        class Page:
            result=dict(width=1600,height=900)
            def evaluate(self,*args):return self.result
        page=Page();self.assertEqual(cover.prepare(page,self.asset),self.prepared)
        for value in [dict(width=1200,height=900),dict(width=0,height=0),dict(width=8192,height=4608)]:
            page.result=value
            with self.assertRaises(PublishError):cover.prepare(page,self.asset)
        page.result=dict(width=675,height=900)
        self.assertEqual(cover.prepare(page,self.asset)['width'],675)

    def test_landscape_and_portrait_cover_must_match_video_before_upload(self):
        cover.check_video_ratio(self.prepared,dict(width=1920,height=1080))
        portrait=dict(self.prepared,width=675,height=900)
        cover.check_video_ratio(portrait,dict(width=720,height=1280))
        for image,video in [(self.prepared,dict(width=720,height=1280)),
                            (portrait,dict(width=1920,height=1080)),
                            (self.prepared,dict(width=1080,height=1080))]:
            with self.assertRaises(PublishError):cover.check_video_ratio(image,video)

    def test_partial_cover_requires_exact_source_and_no_completion_request(self):
        class Page:
            value=self.crop
            def evaluate(inner,*args):return inner.value
        page=Page();saved=dict(prepared=self.prepared,requests=[],responses=[],errors=[])
        self.assertEqual(cover.incomplete(page,self.asset,self.prepared,saved)['stage'],'cover_editing_required')
        for key,value in [('requests',[{}]),('responses',[{}]),('errors',['unknown']),('prepared',{})]:
            with self.assertRaises(PublishError):cover.incomplete(page,self.asset,self.prepared,dict(saved,**{key:value}))
        page.value=dict(self.crop,source_sha256='b'*64)
        with self.assertRaises(PublishError):cover.incomplete(page,self.asset,self.prepared,saved)

    def test_changed_source_or_native_cropping_stops_before_complete(self):
        cover.verify_crop(self.crop,self.prepared)
        for location,key,value in [('root','source_sha256','b'*64),('root','source_size',8),('root','width',1200),
                                   ('root','aspect_ratio',4/3),('root','native_size',0),('root','native_sha256','bad'),
                                   ('crop','x',10),('crop','height',800),('crop','scaleX',-1),('crop','rotate',90)]:
            observed=copy.deepcopy(self.crop);node=observed if location=='root' else observed['crop'];node[key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):cover.verify_crop(observed,self.prepared)

    def test_unique_cloud_receipt_must_bind_actual_crop_output(self):
        with patch.object(cover,'preview',return_value={'pid':'NativeCoverPid123'}) as preview:
            cover.observe(None,self.asset,self.prepared,self.saved);preview.assert_called_once()
        cases=[]
        for key,value in [('errors',['unrecorded']),('requests',[]),('responses',[]),('prepared',{})]:
            saved=copy.deepcopy(self.saved);saved[key]=value;cases.append(saved)
        saved=copy.deepcopy(self.saved);saved['requests']*=2;cases.append(saved)
        for key,value in [('sha256','b'*64),('size',99),('content_type','text/plain')]:
            saved=copy.deepcopy(self.saved);saved['requests'][0][key]=value;cases.append(saved)
        for key,value in [('status',500),('ret',False),('pid','bad')]:
            saved=copy.deepcopy(self.saved);saved['responses'][0][key]=value;cases.append(saved)
        for saved in cases:
            with patch.object(cover,'preview') as preview,self.assertRaises(PublishError):cover.observe(None,self.asset,self.prepared,saved)
            preview.assert_not_called()

    def test_remote_preview_requires_exact_pid_origin_and_loaded_image(self):
        class Node:
            value=dict(src='https://wx4.sinaimg.cn/bmiddle/NativeCoverPid123.jpg',complete=True,width=440,height=247)
            def count(self):return 1
            def is_visible(self):return True
            def locator(self,*args):return self
            def evaluate(self,*args):return self.value
        class Page:
            def get_by_role(self,*args,**kwargs):return node
        node=Node();page=Page();cover.preview(page,'NativeCoverPid123')
        original=copy.deepcopy(node.value)
        for key,value in [('src','https://wx4.sinaimg.cn.evil.test/bmiddle/NativeCoverPid123.jpg'),
                          ('src','https://wx4.sinaimg.cn/bmiddle/WrongCoverPid.jpg'),
                          ('src','blob:https://weibo.com/preview'),('complete',False),('width',0),('height',True)]:
            node.value=dict(original,**{key:value})
            with self.subTest(key=key),self.assertRaises(PublishError):cover.preview(page,'NativeCoverPid123')


if __name__=='__main__':unittest.main()
