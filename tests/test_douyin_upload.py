import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import douyin_upload as upload
from publish_core import PublishError, digest

VID='vFixtureUpload1234567890'
URI='tos-cn-v-fixture/sourceObject'


def sample():
    local=dict(source_asset_id='video',source_sha256='a'*64,md5='b'*32,size=200)
    preview=dict(source_asset_id='video',source_sha256='a'*64,size=200,width=720,height=1280,
                 duration=4.0,preview_key=digest(['video-cn.douyin.com',URI]))
    body=dict(ResponseMetadata={},Result=dict(Results=[dict(Vid=VID,VideoMeta=dict(
        Md5='b'*32,Size=200,Width=720,Height=1280,Duration=4.0,Uri=URI))]))
    event=dict(upload.native(body),observed_at=100)
    return local,preview,body,event


def response(body,url=None,status=200):
    r=Mock(url=url or 'https://vod.bytedanceapi.com/?Action=CommitUploadInner&Version=2020-11-19&SpaceName=aweme',status=status)
    r.request.method='POST';r.body.return_value=json.dumps(body).encode()
    return r


class UploadTests(unittest.TestCase):
    def test_one_pass_source_hashes_same_bytes_and_refuses_changed_or_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'source.mp4';p.write_bytes(b'actual file bytes')
            asset=dict(id='v',absolute_path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            v=upload.source(asset)
            self.assertEqual(v['md5'],hashlib.md5(p.read_bytes(),usedforsecurity=False).hexdigest())
            self.assertEqual(v['size'],len(p.read_bytes()))
            p.write_bytes(b'changed')
            with self.assertRaises(PublishError):upload.source(asset)
            p.write_bytes(b'');asset['sha256']=hashlib.sha256(b'').hexdigest()
            with self.assertRaises(PublishError):upload.source(asset)

    def test_receipt_binds_vid_to_source_hash_and_exact_preview_object(self):
        local,preview,body,event=sample()
        v=upload.receipt([event],local,preview,now=101)
        self.assertEqual(v['vid'],VID);self.assertEqual(v['source_sha256'],'a'*64)
        self.assertNotIn('md5',v);self.assertNotIn('Uri',str(v))
        self.assertEqual(upload.receipt([event,dict(event,observed_at=101)],local,preview,now=102),v)

    def test_missing_ambiguous_failed_and_unsupported_native_body_rejected(self):
        _,_,body,_=sample()
        bad=[{},dict(ResponseMetadata={},Result=dict(Results=[])),
             dict(ResponseMetadata={},Result=dict(Results=body['Result']['Results']*2)),
             dict(ResponseMetadata=dict(Error={}),Result=body['Result'])]
        for key,value in [('Vid',''),('VideoMeta.Md5',''),('VideoMeta.Size',True),('VideoMeta.Size',0),
                          ('VideoMeta.Width',True),('VideoMeta.Height',0),('VideoMeta.Duration',float('nan')),
                          ('VideoMeta.Duration',True),('VideoMeta.Uri','https://example.org/token')]:
            b=copy.deepcopy(body);obj=b['Result']['Results'][0];keys=key.split('.')
            for part in keys[:-1]:obj=obj[part]
            obj[keys[-1]]=value;bad.append(b)
        for b in bad:
            with self.subTest(body=b),self.assertRaises(PublishError):upload.native(b)

    def test_same_duration_size_or_title_never_substitutes_original(self):
        local,preview,body,event=sample()
        for field,value in [('Md5','c'*32),('Size',201),('Width',1280),('Height',720),
                            ('Duration',4.2),('Uri','tos-cn-v-fixture/otherObject')]:
            e=copy.deepcopy(event);e['metadata'][field]=value
            with self.subTest(field=field),self.assertRaises(PublishError):upload.receipt([e],local,preview,now=101)
        for key,value in [('source_sha256','c'*64),('source_asset_id','other'),('size',True),('width',True),('duration',-0.001)]:
            p=copy.deepcopy(preview);p[key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):upload.receipt([event],local,p,now=101)

    def test_multiple_vids_clock_errors_and_empty_input_rejected(self):
        local,preview,_,event=sample()
        other=dict(event,vid='vOtherFixture1234567890',observed_at=101)
        for events in [[],[event,other],[dict(event,observed_at=200)],
                       [event,dict(event,observed_at=99)],[dict(event,observed_at=True)]]:
            with self.subTest(events=events),self.assertRaises(PublishError):upload.receipt(events,local,preview,now=101)

    def test_capture_saves_only_native_identity_without_credentials(self):
        _,_,body,_=sample();body['SessionKey']='never save';body['Result']['Results'][0]['Other']='signed secret'
        saved=[];c=upload.Capture(lambda events,errors:saved.append(copy.deepcopy((events,errors))))
        with patch.object(upload.time,'time',return_value=100):c.response(response(body))
        self.assertEqual(c.events[0]['vid'],VID);self.assertEqual(len(saved),1);self.assertFalse(c.errors)
        self.assertNotIn('never save',str(saved));self.assertNotIn('signed secret',str(saved))
        for url in ['https://evil.org/?Action=CommitUploadInner',
                    'https://vod.bytedanceapi.com.evil.org/?Action=CommitUploadInner',
                    'https://vod.bytedanceapi.com/?Action=ApplyUploadInner']:
            c.response(response(body,url=url))
        self.assertEqual(len(c.events),1)

    def test_native_errors_and_persistence_errors_are_sticky(self):
        _,_,body,_=sample()
        for r in [response(body,status=500),response(body,url='https://vod.bytedanceapi.com/?Action=CommitUploadInner&Version=wrong&SpaceName=aweme')]:
            c=upload.Capture(lambda *args:None);c.response(r);self.assertTrue(c.errors);self.assertFalse(c.events)
        def fail(*args):raise OSError('private path or token must not leak')
        c=upload.Capture(fail);c.response(response(body))
        self.assertEqual(c.errors,['native_video_commit_unrecorded']);self.assertNotIn('private path',str(c.errors))
        r=response(body);r.body.return_value=b'{"ResponseMetadata":{},"ResponseMetadata":{},"Result":{}}'
        c=upload.Capture(lambda *args:None);c.response(r);self.assertTrue(c.errors)

    def test_saved_cloud_receipt_requires_original_events_and_same_source(self):
        local,preview,_,event=sample();asset=dict(id='video',kind='video',sha256='a'*64)
        pack=dict(assets=[asset]);saved=dict(video_source=local,video_upload_events=[event],
                                           cloud_video=upload.receipt([event],local,preview,now=101))
        with patch.object(upload,'source',return_value=local),patch.object(upload.time,'time',return_value=101):
            self.assertEqual(upload.verify(pack,saved,preview)['vid'],VID)
            for change in [dict(video_upload_events=[]),dict(video_upload_errors=['lost']),dict(video_source={}),dict(cloud_video={})]:
                with self.subTest(change=change),self.assertRaises(PublishError):upload.verify(pack,dict(saved,**change),preview)
        self.assertIsNone(upload.verify(pack,{},preview))

    def test_published_video_requires_same_pack_vid_and_native_properties(self):
        local,preview,_,event=sample();pack=dict(assets=[dict(id='video',kind='video',sha256='a'*64)])
        saved=dict(pack_digest=digest(pack),video_source=local,source_video=preview,
                   video_upload_events=[event],cloud_video=upload.receipt([event],local,preview,now=101))
        details=dict(video=dict(vid=VID,width=720,height=1280,duration_ms=4000))
        with patch.object(upload.time,'time',return_value=101):
            self.assertEqual(upload.post_video_binding(details,pack,saved)['vid'],VID)
            for key,value in [('vid','vOtherFixture1234567890'),('width',1280),('height',720),('duration_ms',4200)]:
                d=copy.deepcopy(details);d['video'][key]=value
                with self.subTest(key=key),self.assertRaises(PublishError):upload.post_video_binding(d,pack,saved)
            with self.assertRaises(PublishError):upload.post_video_binding(details,dict(pack,title='different'),saved)
            with self.assertRaises(PublishError):upload.post_video_binding(details,pack,{})


if __name__=='__main__':unittest.main()
