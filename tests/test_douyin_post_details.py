"""Published native detail contracts; fixtures contain no account or remote data."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import douyin_post_details as detail
import douyin_post as post
from publish_core import PublishError

UID='987654321';RID='1234567890123456789';VID='v0000fixturePublishedVideo'


def sample():
    caption='看图😺\n\n#学习 #英语';title='系列';description=title+' '+caption
    extra=[]
    for name in ['学习','英语']:
        def offset(text):return len(text[:text.index('#'+name)].encode('utf-16-le'))//2
        s=offset(caption);d=offset(description);length=len(('#'+name).encode('utf-16-le'))//2
        extra.append(dict(type=1,hashtag_name=name,caption_start=s,caption_end=s+length,start=d,end=d+length))
    cover='tos-cn-i-dy/verticalFixture';horizontal='tos-cn-i-dy/horizontalFixture'
    listed=dict(remote_id=RID,native_user_id=UID,description=description,duration_ms=123267,cover_uri=cover)
    aweme=dict(aweme_id=RID,item_id=1234567890123456800,group_id=1234567890123456800,
        author_user_id=int(UID),author=dict(uid=UID,short_id='fixture-public',unique_id=''),
        desc=description,item_title=title,caption=caption,aweme_type=4,images=None,image_infos=None,
        is_live_replay=False,is_pic_word=False,forward_id='0',duration=123267,
        status=dict(aweme_id=RID,in_reviewing=False,is_delete=False,is_private=False,is_prohibited=False,self_see=False,private_status=0),
        video=dict(vid=VID,width=1080,height=1920,duration=123267,
                   play_addr=dict(uri=VID),download_addr=dict(uri=VID),cover=dict(uri=cover)),
        Cover=dict(uri=cover),horizontal_cover=dict(uri=horizontal),
        user_declare_info='{"choose_value":"aigc"}',text_extra=extra,mix_info=None)
    return dict(status_code=0,aweme=aweme),listed


class DouyinDetailTests(unittest.TestCase):
    def test_pending_or_blocked_timer_cannot_pass_published_detail_readback(self):
        for code in [0, 4, 5, 2, True]:
            body, listed = sample()
            body['aweme']['timer'] = dict(status=code, public_time=1793442600)
            with self.subTest(code=code), patch.object(detail.time, 'time', return_value=1793442601), self.assertRaises(PublishError):
                detail.facts(body, listed, 'fixture-public')

    def test_online_timer_still_requires_consistent_public_time_and_all_other_fields(self):
        body, listed = sample()
        body['aweme']['timer'] = dict(status=1, public_time=1793442600)
        with patch.object(detail.time, 'time', return_value=1793442599):
            with self.assertRaises(PublishError):detail.facts(body, listed, 'fixture-public')
        with patch.object(detail.time, 'time', return_value=1793442601):
            value = detail.facts(body, listed, 'fixture-public')
            self.assertEqual(value['timer']['state'], 'online')
            self.assertFalse(value['timer']['publication_verified'])
            body['aweme']['author']['uid'] = '111'
            with self.assertRaises(PublishError):detail.facts(body, listed, 'fixture-public')

    def test_string_identity_ignores_rounded_numeric_ids_but_keeps_native_video(self):
        body,listed=sample();v=detail.facts(body,listed,'fixture-public')
        self.assertEqual(v['remote_id'],RID);self.assertEqual(v['video']['vid'],VID)
        self.assertEqual(v['topics'],['学习','英语']);self.assertEqual(v['caption'],'看图😺\n\n#学习 #英语')
        self.assertEqual(v['declaration'],'内容由AI生成');self.assertIsNone(v['collection'])
        for value in [int(RID),str(body['aweme']['item_id']),None]:
            b=copy.deepcopy(body);b['aweme']['aweme_id']=value
            with self.subTest(value=value),self.assertRaises(PublishError):detail.facts(b,listed,'fixture-public')

    def test_author_text_media_and_public_state_must_agree(self):
        changes=[('author.uid','111'),('author.short_id','other-public'),('desc','改变文案'),
                 ('aweme_type',True),('images',[]),('is_live_replay',True),('forward_id','456'),
                 ('status.aweme_id','456'),('status.in_reviewing',True),('status.is_private',True),
                 ('status.private_status',False),('duration',123268),('video.duration',1),
                 ('video.play_addr.uri','vWrongFixtureVideo'),('video.width',0),
                 ('Cover.uri','tos-cn-i-dy/other'),('video.cover.uri','tos-cn-i-dy/other')]
        for path,value in changes:
            b,l=sample();obj=b['aweme'];keys=path.split('.')
            for key in keys[:-1]:obj=obj[key]
            obj[keys[-1]]=value
            with self.subTest(path=path),self.assertRaises(PublishError):detail.facts(b,l,'fixture-public')
        for b in [{},dict(status_code=True,aweme=sample()[0]['aweme'])]:
            with self.assertRaises(PublishError):detail.facts(b,sample()[1],'fixture-public')

    def test_declaration_does_not_guess_or_accept_duplicate_fields(self):
        for declaration in ['{}','null','{"choose_value":"none"}',
                            '{"choose_value":"other","choose_value":"aigc"}',
                            '{"choose_value":"aigc","extra":true}',None]:
            b,l=sample();b['aweme']['user_declare_info']=declaration
            with self.subTest(value=declaration),self.assertRaises(PublishError):detail.facts(b,l,'fixture-public')

    def test_collection_requires_exact_durable_id_and_current_name(self):
        b,l=sample();b['aweme']['mix_info']=dict(mix_id='246813579024681357',mix_name='系列',next_info=dict(mix_name='系列'))
        v=detail.facts(b,l,'fixture-public');self.assertEqual(v['collection'],dict(id='246813579024681357',name='系列'))
        for key,value in [('mix_id',246813579024681357),('mix_name',''),('next_info',dict(mix_name='另一个系列'))]:
            c=copy.deepcopy(b);c['aweme']['mix_info'][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):detail.facts(c,l,'fixture-public')

    def test_native_topic_offsets_use_utf16_and_preserve_order(self):
        b,l=sample();self.assertEqual(detail.topics(b['aweme']),['学习','英语'])
        for key,value in [('type',True),('caption_start',3),('caption_end',500),('start',0),('hashtag_name','错误')]:
            c=copy.deepcopy(b);c['aweme']['text_extra'][0][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):detail.topics(c['aweme'])
        b['aweme']['text_extra'].reverse()
        with self.assertRaises(PublishError):detail.topics(b['aweme'])
        with self.assertRaises(PublishError):detail.utf16_slice('😺',0,1)
        self.assertEqual(detail.utf16_slice('😺',0,2),'😺')

    def test_private_cover_url_requires_native_host_and_object_identity(self):
        self.assertEqual(detail.native_image_url('https://p0-creator-media-private.douyin.com/tos-cn-i-dy/fixture~image.jpeg?signature=expiring'),'tos-cn-i-dy/fixture')
        for url in ['https://evil.com/tos-cn-i-dy/fixture~image.jpeg',
                    'https://p0-creator-media-private.douyin.com.evil.com/tos-cn-i-dy/fixture~image.jpeg',
                    'https://u:p@p0-creator-media-private.douyin.com/tos-cn-i-dy/fixture~image.jpeg',
                    'https://p0-creator-media-private.douyin.com/other/fixture']:
            with self.subTest(url=url),self.assertRaises(PublishError):detail.native_image_url(url)

    def test_detail_option_cannot_escalate_or_skip_base_readback(self):
        with patch.object(post,'background_window') as window:
            for value in ['true',1,None]:
                with self.assertRaises(PublishError):post.readback(Mock(),cdp_url='http://127.0.0.1:9333',public_identity='fixture-public',remote_id=RID,include_details=value)
            window.assert_not_called()

    def view(self,value):
        page=Mock();nodes={}
        for name in ['title','description','declaration','vertical','horizontal','video','mix']:
            n=nodes[name]=Mock();n.count.return_value=1;n.is_visible.return_value=True
        nodes['title'].input_value.return_value=value['title']
        nodes['declaration'].inner_text.return_value=value['declaration']
        nodes['mix'].count.return_value=0
        sections={}
        for role,label in [('vertical','竖封面3:4'),('horizontal','横封面4:3')]:
            n=nodes[role];n.evaluate.return_value=dict(src='https://p0-creator-media-private.douyin.com/'+value['covers'][role]+'~image.jpeg',loaded=True)
            section=Mock();section.count.return_value=1;section.is_visible.return_value=True;section.locator.return_value=n;sections[label]=section
        covers=Mock();covers.filter.side_effect=lambda **kw:sections[kw['has']]
        def locate(selector):
            if selector==detail.editor.TITLE:return nodes['title']
            if selector==detail.editor.DESCRIPTION:return nodes['description']
            if selector=='[class^="coverControl-"]':return covers
            if selector=='video':return nodes['video']
            if selector=='[class^="mix-display-"] [class^="mix-name-"]':return nodes['mix']
            raise AssertionError(selector)
        page.locator.side_effect=locate;page.get_by_text.side_effect=lambda name,**kwargs:name
        url='https://v26.douyinvod.com/fixture-preview?signature=expiring'
        state=dict(src=url,ready=4,duration=123.266667,width=720,height=1280)
        nodes['video'].evaluate.return_value=state
        receipts=[dict(video_id=VID,body=dict(status_code=0,play_url=url,duration=123.267,width='720',height='1280'))]
        doc=dict(text=value['caption'],topics=value['topics'])
        return page,nodes,receipts,doc

    def test_visible_detail_requires_fields_original_entities_and_source_scoped_preview(self):
        body,listed=sample();value=detail.facts(body,listed,'fixture-public')
        changes=[lambda n,r,d:n['title'].input_value.configure_mock(return_value='错标题'),
                 lambda n,r,d:d.update(text='错正文'),lambda n,r,d:d.update(topics=['英语','学习']),
                 lambda n,r,d:n['declaration'].inner_text.configure_mock(return_value='无需添加自主声明'),
                 lambda n,r,d:n['vertical'].evaluate.return_value.update(src='https://p0-creator-media-private.douyin.com/'+value['covers']['horizontal']+'~image.jpeg'),
                 lambda n,r,d:n['horizontal'].evaluate.return_value.update(loaded=False),
                 lambda n,r,d:n['mix'].count.configure_mock(return_value=1),
                 lambda n,r,d:r[0].update(video_id='vWrongFixtureVideo'),
                 lambda n,r,d:r[0]['body'].update(status_code=True),
                 lambda n,r,d:r[0]['body'].update(duration=1),
                 lambda n,r,d:n['video'].evaluate.return_value.update(src='https://v26.douyinvod.com/other'),
                 lambda n,r,d:n['video'].evaluate.return_value.update(width=1080),
                 lambda n,r,d:n['video'].evaluate.return_value.update(ready=0)]
        for change in [None]+changes:
            page,nodes,receipts,doc=self.view(value)
            if change:change(nodes,receipts,doc)
            with patch.object(detail.editor,'native_document',return_value=doc),patch.object(detail.editor,'declaration_control',return_value=nodes['declaration']):
                if change:
                    with self.subTest(change=changes.index(change)),self.assertRaises(PublishError):detail.observe(page,value,receipts)
                else:detail.observe(page,value,receipts)
            page.mouse.click.assert_not_called()

    def test_collection_preview_cannot_be_hidden_or_a_different_name(self):
        b,l=sample();value=detail.facts(b,l,'fixture-public');value['collection']=dict(id='246813579',name='系列')
        for name,visible in [('合集·系列',True),('合集·其他',True),('合集·系列',False)]:
            page,nodes,receipts,doc=self.view(value);nodes['mix'].count.return_value=1;nodes['mix'].text_content.return_value=name;nodes['mix'].evaluate.return_value=visible
            with patch.object(detail.editor,'native_document',return_value=doc),patch.object(detail.editor,'declaration_control',return_value=nodes['declaration']):
                if name=='合集·系列' and visible:detail.observe(page,value,receipts)
                else:
                    with self.subTest(name=name,visible=visible),self.assertRaises(PublishError):detail.observe(page,value,receipts)
