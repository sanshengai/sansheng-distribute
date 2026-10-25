import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import PublishError
import weibo_video_fields as fields
import weibo_browser
import browser_engine


def pack():
    return dict(platform='weibo',endpoint='prefill',content_type='video',document=None,subtitle_asset_ids=[],
        cover_asset_id='cover',platform_metadata=dict(creation_type='原创',declaration='内容为自主创作',ai_generated=False),
        fields=dict(title='测试视频',text='',description='完整描述',composed_text='完整描述\n\n#工具#\n\nhttps://example.com/',tags=['工具']),
        assets=[dict(id='video',kind='video',path='v.mp4'),dict(id='cover',kind='image',path='cover.png')])


class Pack(unittest.TestCase):
    def test_supported_explicit_fields_pass_both_preflight_routes(self):
        for check in (fields.check,weibo_browser.check_pack,browser_engine.check_pack):check(pack())
        p=pack();p['cover_asset_id']=None;p['assets']=p['assets'][:1];fields.check(p)

    def test_creation_type_cannot_replace_honest_ai_declaration(self):
        for key,value in [('creation_type',None),('creation_type',{}),('declaration',None),
                          ('declaration',[]),('ai_generated','false'),('ai_generated',True)]:
            p=pack();p['platform_metadata'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(PublishError):fields.check(p)
        p=pack();p['platform_metadata'].update(ai_generated=True,declaration='内容由AI生成');fields.check(p)
        p['platform_metadata']['ai_generated']=False
        with self.assertRaises(PublishError):fields.check(p)

    def test_unmapped_assets_fields_or_endpoints_stop_before_browser(self):
        for key,value in [('endpoint','publish'),('document',{'format':'html'}),('subtitle_asset_ids',['s']),
                          ('cover_asset_id',None),('content_type','text')]:
            p=pack();p[key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):fields.check(p)
        for key in ('collection_name','schedule','activity','original'):
            p=pack();p['platform_metadata'][key]='unsupported'
            with self.assertRaises(PublishError):fields.check(p)
        for asset in [dict(id='audio',kind='audio',path='a.mp3'),dict(id='extra',kind='image',path='x.png')]:
            p=pack();p['assets'].append(asset)
            with self.assertRaises(PublishError):fields.check(p)

    def test_complete_copy_lengths_and_plain_topic_names_are_not_silently_changed(self):
        for key,value in [('title',''),('title','😀'*16),('composed_text','x'*1001),('text','another description'),
                          ('tags',['工具','工具']),('tags',['not a topic']),('tags',['#']),('tags',['内部#井号'])]:
            p=pack();p['fields'][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):fields.check(p)


class FieldReadback(unittest.TestCase):
    def setUp(self):
        self.pack=pack();self.value=dict(title=self.pack['fields']['title'],description=self.pack['fields']['composed_text'],
            radios=[dict(label=x,checked=x=='原创') for x in ('原创','二创','转载')],declaration='内容为自主创作')

    def test_exact_full_copy_and_two_distinct_declarations(self):
        with patch.object(fields,'snapshot',return_value=self.value):self.assertEqual(fields.observe(None,self.pack),self.value)

    def test_modified_text_or_wrong_declaration_does_not_report_ready(self):
        for key,value in [('title','改动'),('description','缺少链接'),('declaration','内容由AI生成'),
                          ('radios',[dict(label=x,checked=True) for x in ('原创','二创','转载')])]:
            observed=copy.deepcopy(self.value);observed[key]=value
            with patch.object(fields,'snapshot',return_value=observed),self.assertRaises(PublishError):fields.observe(None,self.pack)

    def test_existing_author_fields_are_preserved(self):
        for key,value in [('title','原作者标题'),('description','原作者文案'),('declaration','无'),
                          ('radios',[dict(label='原创',checked=True)])]:
            current=dict(title='',description='',declaration='内容声明',radios=[]);current[key]=value
            with patch.object(fields,'snapshot',return_value=current),self.assertRaises(PublishError):fields.empty(None)


if __name__=='__main__':unittest.main()
