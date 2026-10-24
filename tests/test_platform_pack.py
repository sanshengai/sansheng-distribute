import copy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import PublishError, validate
from platform_pack import build_pack


def fixture(root,platform='x',kind='text'):
    (root/'source.txt').write_text('final source',encoding='utf-8')
    assets=[]
    for ident,k,ext in [('image','image','png'),('video','video','mp4'),('subtitle','subtitle','srt')]:
        (root/f'{ident}.{ext}').write_bytes(b'fixture not real media')
        assets.append(dict(id=ident,path=f'{ident}.{ext}',kind=k))
    ids=['video','image','subtitle'] if kind=='video' else ['image']
    m=dict(schema_version=1,job_id='j',producer='offline-tests',content_status='final',source_root=str(root),documents=[dict(id='source',path='source.txt',format='text')],assets=assets,
           targets=[dict(id='t',platform=platform,account='owner',content_type=kind,endpoint='prefill',text='分享正文',asset_ids=ids)],media_slots=[])
    # Unused video must be absent from source asset list, per core contract.
    m['assets']=[a for a in assets if a['id'] in ids]
    validate(m,sealing=True)
    return m


class PlatformPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def test_all_platforms_preserve_fields_assets_and_metadata(self):
        for platform in ('x','weibo','douyin','bilibili','tencent_video','wechat_channels','xiaohongshu'):
            m=fixture(self.root,platform,'video');t=m['targets'][0]
            t.update(title='标题',description='独立简介',tags=['AI','生图'],links=[{'label':'看板','url':'https://example.com/board'}],cover_asset_id='image',subtitle_asset_ids=['subtitle'],platform_metadata={'category':'作者选择'})
            p=build_pack(m,'t')
            self.assertEqual(p['original_target'],t)
            self.assertEqual(p['fields']['description'],'独立简介')
            self.assertIn('https://example.com/board',p['fields']['composed_text'])
            self.assertIn('#AI#' if platform=='weibo' else '#AI',p['fields']['composed_text'])
            self.assertEqual(p['subtitle_asset_ids'],['subtitle']); self.assertEqual(len(p['assets']),3)

    def test_pack_checks_do_not_mutate_prior_packs_or_shared_defaults(self):
        m=fixture(self.root,'bilibili','video')
        first=build_pack(m,'t'); saved=copy.deepcopy(first)
        second=build_pack(m,'t')
        self.assertEqual(first,saved);self.assertEqual(second,first)
        self.assertEqual(sum('全部字幕' in x for x in second['checks']),1)
        m['targets'][0]['asset_ids'].remove('subtitle');m['assets']=[x for x in m['assets'] if x['id']!='subtitle']
        plain=build_pack(m,'t')
        self.assertFalse(any('全部字幕' in x for x in plain['checks']))

    def test_empty_and_invalid_types_rejected(self):
        with self.assertRaises(PublishError): build_pack({},'t')
        for field,value in [('title',5),('tags','AI'),('tags',['']),('links',['https://example.com']),('links',[{'url':'javascript:evil'}]),('links',[{'url':'https://user:pass@example.com'}]),('platform_metadata',[]),('subtitle_asset_ids',[])]:
            m=fixture(self.root,'bilibili','video');m['targets'][0][field]=value
            with self.subTest(field=field),self.assertRaises(PublishError):build_pack(m,'t')

    def test_explicit_limits_fail_without_truncating_or_default_limit(self):
        m=fixture(self.root);m['targets'][0]['text']='字'*10000
        self.assertEqual(len(build_pack(m,'t')['fields']['composed_text']),10000)
        profile={'platform_policies':{'x':{'max_lengths':{'composed_text':10}}}}
        with self.assertRaisesRegex(PublishError,'不截断'):build_pack(m,'t',profile)
        self.assertEqual(len(m['targets'][0]['text']),10000)
        for policy in ({'required_fields':['title']},{'require_cover':True},{'require_subtitles':True},{'max_assets':{'image':0}},{'allowed_extensions':{'image':['.jpg']}},{'max_lenghts':{}},{'max_lengths':{'title':True}}):
            with self.subTest(policy=policy),self.assertRaises(PublishError): build_pack(m,'t',{'platform_policies':{'x':policy}})

    def test_resume_snapshot_does_not_need_original_files(self):
        m=fixture(self.root);(self.root/'source.txt').unlink();(self.root/'image.png').unlink()
        self.assertEqual(build_pack(m,'t',check_files=False)['target_id'],'t')
        with self.assertRaises(PublishError):build_pack(m,'t')

if __name__=='__main__':unittest.main()
