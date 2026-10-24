"""Workspace integration tests. Skip only when optional producer repos are absent."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import validate


class ProducerContracts(unittest.TestCase):
    @unittest.skipUnless((ROOT/'sansheng-write/scripts/publish_handoff.py').is_file(),'optional Write checkout absent')
    def test_actual_write_export_passes_consumer(self):
        sys.path.insert(0,str(ROOT/'sansheng-write/scripts'))
        from publish_handoff import export_handoff
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'定稿.html').write_text('<p>前</p><!-- VIDEO:demo --><p>后</p>')
            (root/'定稿.md').write_text('<!-- VIDEO:demo -->')
            (root/'demo.mp4').write_bytes(b'contract-fixture')
            targets=[dict(id='wx',platform='wechat',account='test',content_type='article',endpoint='draft',title='测试')]
            media=[dict(id='demo',kind='video',path='demo.mp4',anchor='<!-- VIDEO:demo -->')]
            path=export_handoff(root,targets,media=media)
            self.assertEqual(validate(json.loads(path.read_text()))['slots'],1)

    @unittest.skipUnless((ROOT/'sansheng-video/scripts/export_publish.py').is_file(),'optional Video checkout absent')
    def test_actual_video_export_passes_consumer(self):
        sys.path.insert(0,str(ROOT/'sansheng-video/scripts'))
        from export_publish import export
        from package_delivery import digest
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ('video.mp4','cover.png'): (root/name).write_bytes(b'contract-fixture')
            delivery=root/'delivery.json'
            delivery.write_text(json.dumps(dict(kind='video-delivery/1',review_status='user_accepted',review_source='fixture',technical={'full_decode':True},video='video.mp4',poster='cover.png',files={n:digest(root/n) for n in ('video.mp4','cover.png')},suggested_title='测试',summary='测试')))
            target=root/'targets.json'; target.write_text(json.dumps({'targets':[dict(id='dy',platform='douyin',account='test',content_type='video',endpoint='draft')]}))
            path=export(delivery,target,root/'publish.json')
            self.assertEqual(validate(json.loads(path.read_text()))['assets'],2)


if __name__=='__main__': unittest.main()
