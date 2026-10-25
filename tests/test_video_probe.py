import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import PublishError
import video_probe


class Probe(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        path=Path(self.temp.name)/'video.mp4';path.write_bytes(b'original')
        self.asset=dict(absolute_path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        outer=self
        self.selected=[];self.changed=False
        class Page:
            url='about:blank'
            value=dict(width=720,height=1280,duration=4.0,size=8)
            def set_content(self,value):pass
            def locator(self,*args):return self
            def set_input_files(self,value,**kwargs):outer.selected.append(value)
            def evaluate(self,*args):
                if outer.changed:path.write_bytes(b'modified')
                return self.value
        self.page=Page()

    def test_platform_page_or_changed_snapshot_never_receives_file(self):
        self.page.url='https://weibo.com/upload/channel'
        with self.assertRaises(PublishError):video_probe.inspect(self.page,self.asset)
        self.assertEqual(self.selected,[])
        self.page.url='about:blank';Path(self.asset['absolute_path']).write_bytes(b'changed')
        with self.assertRaises(PublishError):video_probe.inspect(self.page,self.asset)
        self.assertEqual(self.selected,[])

    def test_actual_decoded_dimensions_are_preserved_and_invalid_or_changed_source_rejected(self):
        self.assertEqual(video_probe.inspect(self.page,self.asset),self.page.value)
        original=self.page.value
        for key,value in [('size',7),('width',0),('height',True),('duration',float('nan')),('duration',0)]:
            self.page.value=dict(original,**{key:value})
            with self.subTest(key=key),self.assertRaises(PublishError):video_probe.inspect(self.page,self.asset)
        self.page.value=original;self.changed=True
        with self.assertRaises(PublishError):video_probe.inspect(self.page,self.asset)


if __name__=='__main__':unittest.main()
