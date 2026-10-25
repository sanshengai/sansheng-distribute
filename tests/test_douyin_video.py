import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import douyin_video as video
from publish_core import PublishError


class Node:
    def __init__(self,value):self.value=value;self.visible=True;self.n=1
    def count(self):return self.n
    def is_visible(self):return self.visible
    def evaluate(self,script,size):return copy.deepcopy(self.value)


class Page:
    def __init__(self,value):self.node=Node(value)
    def locator(self,selector):
        if selector!='video':raise AssertionError(selector)
        return self.node


class VideoTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.path=Path(td.name)/'video.mp4';data=b'controlled original video';self.path.write_bytes(data)
        self.asset=dict(id='source',kind='video',absolute_path=str(self.path),sha256=hashlib.sha256(data).hexdigest())
        self.value=dict(src='https://video-cn.douyin.com/signature/expiry/tos-cn-v-test/original?signature=temporary',
            sha256=self.asset['sha256'],size=len(data),width=720,height=1280,duration=4.0,ready=4)
        self.page=Page(self.value)

    def test_exact_source_proof_omits_transport_signature(self):
        actual=video.observe(self.page,self.asset)
        self.assertEqual(actual['source_sha256'],self.asset['sha256'])
        self.assertEqual(actual['source_asset_id'],'source')
        self.assertNotIn('temporary',str(actual));self.assertNotIn('signature',str(actual))
        saved=dict(source_video=actual)
        self.assertTrue(video.verify(self.page,dict(assets=[self.asset]),saved))

    def test_equal_length_different_content_rejected(self):
        self.page.node.value['sha256']='0'*64
        with self.assertRaises(PublishError):video.observe(self.page,self.asset)

    def test_source_size_and_media_shape_rejected(self):
        for key,value in [('size',True),('size',2),('width',True),('height',0),('ready',False),
                          ('duration',float('nan')),('duration',0),('duration',True)]:
            with self.subTest(key=key,value=value):
                bad=copy.deepcopy(self.value);bad[key]=value
                with self.assertRaises(PublishError):video.observe(Page(bad),self.asset)

    def test_foreign_or_ambiguous_sources_rejected(self):
        for src in ['http://video-cn.douyin.com/signature/expiry/tos-cn-v-test/original',
                    'https://video-cn.douyin.com.evil.org/signature/expiry/tos-cn-v-test/original',
                    'https://video-cn.douyin.com/unknown/original',self.value['src']+'#fragment']:
            with self.subTest(src=src):
                bad=copy.deepcopy(self.value);bad['src']=src
                with self.assertRaises(PublishError):video.observe(Page(bad),self.asset)
        for n in [0,2]:
            self.page.node.n=n
            with self.assertRaises(PublishError):video.observe(self.page,self.asset)

    def test_empty_or_oversized_source_rejected_before_native_read(self):
        self.path.write_bytes(b'')
        self.asset['sha256']=hashlib.sha256(b'').hexdigest()
        with self.assertRaises(PublishError):video.prepare(self.asset)
        self.path.write_bytes(b'ab');self.asset['sha256']=hashlib.sha256(b'ab').hexdigest()
        with patch.object(video,'MAX_BYTES',1):
            with self.assertRaises(PublishError):video.prepare(self.asset)

    def test_local_change_during_native_read_rejected(self):
        def read(*args):self.path.write_bytes(b'changed');return self.value
        with patch.object(self.page.node,'evaluate',side_effect=read):
            with self.assertRaises(PublishError):video.observe(self.page,self.asset)

    def test_saved_receipt_change_rejected_and_legacy_remains_unproven(self):
        self.assertFalse(video.verify(self.page,dict(assets=[self.asset]),{}))
        actual=video.observe(self.page,self.asset);actual['source_asset_id']='other'
        with self.assertRaises(PublishError):video.verify(self.page,dict(assets=[self.asset]),dict(source_video=actual))


if __name__=='__main__':unittest.main()
