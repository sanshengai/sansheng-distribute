"""Synthetic contract negatives, distinct from the private live readonly proof."""
import contextlib
import copy
import html
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import weibo_video_post as video
from publish_core import PublishError, digest
import weibo_video_upload as upload
import weibo_video_cover as cover
from test_weibo_video_upload import receipts

UID='1234567890'; RID='9876543210'; VID='8765432109'; BID='Rfixture1'
PID='fixtureVideoCover'; SHORT='http://t.cn/nativeVideo'
STREAM='https://f.video.weibocdn.com/fixture.mp4?token=synthetic'
LONG='https://video.weibo.com/show?fid=1034:'+VID


def body():
    text='第一段😀\n\n成套图解 '+SHORT
    return dict(ok=1,id=int(RID),idstr=RID,mid=RID,mblogid=BID,
        user=dict(id=int(UID),idstr=UID),visible=dict(type=0),pic_num=0,
        isLongText=True,text_raw=text,longText=dict(content=text),
        text='第一段😀<br><br>成套图解 <a href="'+LONG+'"><img src="icon.png"/>视频</a>',
        url_struct=[dict(short_url=SHORT,long_url=LONG,url_type=39,
            ori_url='sinaweibo://video/vvs?mid='+RID+'&object_id=1034:'+VID+'&url_type=39&object_type=video&pos=1')]*2,
        page_info=dict(type='11',object_type='video',object_id='1034:'+VID,page_id='230444'+VID,
            author_id=UID,authorid=UID,page_pic='https://wx1.sinaimg.cn/orj480/'+PID+'.jpg',short_url=SHORT,
            media_info=dict(media_id=VID,author_mid=RID,media_type='video',
                author_info=dict(id=int(UID),idstr=UID),video_title='知识图解',duration=45,
                playback_list=[dict(play_info=dict(url=STREAM))])))


def edit_body():
    return dict(ok=1,data=dict(video_info=dict(oid='1034:'+VID,mid=int(RID),mid_str=RID,
        video_type='normal',biz_type='video',video_visibility=0,publishing=False,editable=1,
        titles=[dict(title='知识图解',default=True)],covers=[dict(type='default',url='https://wx2.sinaimg.cn/large/'+PID+'.jpg')],
        homemade_info=dict(type=0),width=1920,height=1080,duration=45.95,current_playlists=[])))


class Node:
    def __init__(self,page,kind):self.page=page;self.kind=kind
    def count(self):return self.page.title_count if self.kind=='title' else 1
    def is_visible(self):return self.page.visible
    def wait_for(self,**kwargs):pass
    def input_value(self):return self.page.title
    def inner_html(self):return self.page.html
    def evaluate(self,script):return self.page.body_visible
    def evaluate_all(self,script):return self.page.players
    def all(self):return [Node(self.page,'article')]*self.page.article_count
    def locator(self,selector):
        if selector.startswith('header a[href='):
            if selector!='header a[href="https://weibo.com/'+UID+'/'+BID+'"]':raise AssertionError(selector)
            return Node(self.page,'header')
        if selector=='.wbpro-feed-ogText > div':return Node(self.page,'text')
        if selector=='video':return Node(self.page,'players')
        raise AssertionError(selector)


class Page:
    def __init__(self,editing=False):
        self.editing=editing;self.raw=edit_body() if editing else body();self.url='';self.redirect=None
        self.title='知识图解';self.title_count=1;self.visible=True;self.body_visible=True
        self.html=body()['text'];self.article_count=1;self.closed=False;self.response_method='GET'
        self.players=[dict(src=STREAM,ready=4,width=1280,height=720,visible=True)]
    def locator(self,selector):
        if selector=='article':return Node(self,'articles')
        if selector=='article .wbpro-feed-ogText':return Node(self,'text')
        if selector=='input[placeholder="填写标题（0～30个字）"]':return Node(self,'title')
        raise AssertionError(selector)
    def on(self,event,callback):self.callback=callback
    def goto(self,url,**kwargs):
        self.url=self.redirect or url
        response_url=('https://weibo.com/ajax/multimedia/getVideoEditInfo?media_id=1034:'+VID
                      if self.editing else 'https://weibo.com/ajax/statuses/show?id='+RID)
        raw=self.raw
        response=type('Response',(),dict(url=response_url,status=200,
            request=type('Request',(),dict(method=self.response_method))(),json=lambda _:raw))()
        self.callback(response)
    def wait_for_timeout(self,*args):pass


class VideoFacts(unittest.TestCase):
    def test_complete_native_transport_and_unknown_declaration(self):
        value=video.facts(body())
        self.assertEqual(value['authored_text'],'第一段😀\n\n成套图解')
        self.assertEqual(value['text'],value['authored_text']+' '+LONG)
        self.assertIsNone(value['statement'])
        self.assertEqual(value['media_id'],VID)

    def test_empty_identity_and_video_identity_rejected(self):
        for bad in (None,{},[],dict(ok=0)):
            with self.subTest(bad=bad),self.assertRaises(PublishError):video.facts(bad)
        for key,value in [('media_id',int(VID)),('author_mid','111'),('media_type','audio'),
                          ('author_info',dict(id=7,idstr=UID)),('duration',float('nan')),('video_title','')]:
            b=body();b['page_info']['media_info'][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):video.facts(b)
        for key,value in [('type',11),('authorid','111'),('object_id','1034:111'),('page_id','230444111')]:
            b=body();b['page_info'][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):video.facts(b)

    def test_private_truncated_mixed_and_hidden_content_rejected(self):
        for key,value in [('visible',dict(type=1)),('pic_num',False),('pic_ids',['picture']),
                          ('longText',None),('text','截断'),('mix_media_info',dict(items=[{}])),
                          ('text','<span hidden>第一段</span>')]:
            b=body();b[key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):video.facts(b)

    def test_attachment_wrong_post_media_query_and_conflict_rejected(self):
        for key,value in [('ori_url',body()['url_struct'][0]['ori_url'].replace('mid='+RID,'mid=111')),
                          ('long_url',LONG+'&fid=1034:111'),('url_type',True),('ori_url','https://example.com/')]:
            b=body();b['url_struct']=copy.deepcopy(b['url_struct']);b['url_struct'][0][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):video.facts(b)
        b=body();b['url_struct']=[]
        with self.assertRaises(PublishError):video.facts(b)

    def test_suffix_and_whitespace_not_arbitrarily_removed(self):
        b=body();b['text_raw']=b['longText']['content']='文案  '+SHORT
        b['text']='文案  <a href="'+LONG+'">视频</a>'
        self.assertEqual(video.facts(b)['authored_text'],'文案 ')
        b['text_raw']=b['longText']['content']=SHORT+' 后记'
        b['text']='<a href="'+LONG+'">视频</a> 后记'
        self.assertIsNone(video.facts(b)['authored_text'])

    def test_explicit_statement_cannot_be_guessed_or_contradictory(self):
        b=body();b['content_auth_list']=[dict(mblog_statement=1)]
        self.assertEqual(video.facts(b)['statement'],1)
        for values in ([dict(mblog_statement=True)],[dict(mblog_statement=9)],
                       [dict(mblog_statement=1),dict(mblog_statement=4)],{}):
            b['content_auth_list']=values
            with self.subTest(values=values),self.assertRaises(PublishError):video.facts(b)

    def test_cover_identity_rejects_untrusted_origin(self):
        for url in ('https://sinaimg.cn.evil.org/'+PID+'.jpg','http://wx1.sinaimg.cn/'+PID+'.jpg',
                    'https://wx1.sinaimg.cn/'+PID+'.jpg?different=1'):
            with self.subTest(url=url),self.assertRaises(PublishError):video.image_pid(url)

    def test_edit_exact_identity_cover_creation_and_fractional_duration(self):
        public=video.facts(body());e=edit_body()
        for empty in (None,{},[],dict(ok=1,data=dict(video_info={}))):
            with self.subTest(empty=empty),self.assertRaises(PublishError):video.edit_facts(empty,public)
        self.assertEqual(video.edit_facts(e,public)['duration'],45.95)
        for label,n in video.CREATION_TYPES.items():
            e['data']['video_info']['homemade_info']['type']=n
            self.assertEqual(video.edit_facts(e,public)['creation_type'],label)
        for key,value in [('mid_str','111'),('oid','1034:111'),('duration',46),('video_visibility',True),
                          ('titles',[dict(title='不同',default=True)]),('covers',[]),
                          ('homemade_info',dict(type=True)),('width',0),('current_playlists',None)]:
            e=edit_body();e['data']['video_info'][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):video.edit_facts(e,public)


class NativeObservation(unittest.TestCase):
    def test_visible_player_must_bind_native_playback_list(self):
        b=body();p=Page();v=video.facts(b)
        self.assertEqual(video.observe_visible(p,v,b)['width'],1280)
        for key,value in [('src','https://f.video.weibocdn.com/wrong.mp4'),('ready',0),('visible',False),('height',0)]:
            p=Page();p.players[0][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):video.observe_visible(p,v,b)
        for key,value in [('body_visible',False),('article_count',0),('article_count',2),('players',[]),('html','别的正文')]:
            p=Page();setattr(p,key,value)
            with self.subTest(key=key),self.assertRaises(PublishError):video.observe_visible(p,v,b)
        b=body();p=Page();p.players[0]['src']='https://weibocdn.com.evil.org/video.mp4'
        b['page_info']['media_info']['playback_list'][0]['play_info']['url']=p.players[0]['src']
        with self.assertRaises(PublishError):video.observe_visible(p,v,b)

    def observe(self,public,edit):
        pages=iter([public,edit])
        @contextlib.contextmanager
        def window(*args,**kwargs):
            p=next(pages)
            try:yield p
            finally:p.closed=True
        with patch.object(video,'background_window',window):
            return video.observe_existing(None,UID,RID,'http://fixture')

    def test_canonical_redirect_and_two_owned_windows_close_without_source_claim(self):
        p=Page();e=Page(True);p.redirect='https://weibo.com/'+UID+'/'+BID
        result=self.observe(p,e)
        self.assertTrue(p.closed and e.closed)
        self.assertFalse(result['source_binding_verified'])
        self.assertFalse(result['remote_media_bytes_verified'])
        self.assertNotIn('content_verified',result)

    def test_unbound_redirect_wrong_identity_and_non_get_close_on_failure(self):
        for kind in ('redirect','identity','method'):
            p=Page();e=Page(True)
            if kind=='redirect':p.redirect='https://weibo.com/'+UID+'/OtherPost'
            if kind=='identity':p.raw.update(id=111,idstr='111',mid='111')
            if kind=='method':p.response_method='POST'
            with self.subTest(kind=kind),self.assertRaises(PublishError):self.observe(p,e)
            self.assertTrue(p.closed)

    def test_editor_visible_title_and_native_identity_must_match(self):
        for kind in ('title','duplicate','media'):
            p=Page();e=Page(True)
            if kind=='title':e.title='不同'
            if kind=='duplicate':e.title_count=2
            if kind=='media':e.raw['data']['video_info']['oid']='1034:111'
            with self.subTest(kind=kind),self.assertRaises(PublishError):self.observe(p,e)
            self.assertTrue(p.closed and e.closed)


def source_fixture():
    pack=dict(platform='weibo',content_type='video',endpoint='publish',cover_asset_id='cover',
        fields=dict(title='知识图解',composed_text='第一段😀\n\n成套图解'),platform_metadata=dict(creation_type='原创'),
        assets=[dict(id='video',kind='video',sha256='a'*64,path='gone-video.mp4'),
                dict(id='cover',kind='image',sha256='b'*64,path='gone-cover.png')])
    events=receipts()
    for event in events:
        for node in (event['fields'],event['body']):
            if 'media_id' in node:node['media_id']=VID
    prepared=dict(width=1600,height=900,source_size=7,source_asset_id='cover',source_sha256='b'*64)
    crop=dict(source_sha256='b'*64,source_size=7,width=1600,height=900,aspect_ratio=16/9,
        crop=dict(x=0,y=0,width=1600,height=900,rotate=0,scaleX=1,scaleY=1),native_size=100,native_sha256='c'*64)
    saved=dict(pack_digest=digest(pack),identity=UID,source_sha256='a'*64,source_size=12,
        upload_events=events,upload_errors=[],native_video=dict(media_id=VID,size=12,chunk_count=2,
        tracking_scope='native_current_file_and_cloud_receipt',source_sha256='a'*64,source_asset_id='video',
        width=1920,height=1080,duration=45.95),cover=dict(prepared=prepared,crop=crop,errors=[],
        requests=[dict(size=100,sha256='c'*64,content_type='application/octet-stream')],
        responses=[dict(status=200,ret=True,pid=PID)]))
    public=video.facts(body());observed=dict(public=public,edit=video.edit_facts(edit_body(),public))
    return pack,dict(identity=UID,remote_id=RID,mblogid=BID),saved,observed


class SourceBinding(unittest.TestCase):
    def bind(self,fixture):return video.bind_sources(fixture[0],UID,*fixture[1:])

    def test_original_and_editor_absent_but_exact_cloud_and_crop_proofs_bind(self):
        result=self.bind(source_fixture())
        self.assertTrue(result['source_binding_verified'])
        self.assertFalse(result['remote_media_bytes_verified'])
        self.assertEqual([a['asset_id'] for a in result['asset_bindings']],['video','cover'])
        self.assertNotIn('content_verified',result)
        self.assertNotIn('state',result)

    def test_missing_prior_native_file_proof_cannot_be_rebuilt_from_public_video(self):
        for field,value in [('native_video',{}),('upload_events',[]),('upload_errors',['missing']),
                            ('source_sha256','f'*64),('source_size',0),('pack_digest','wrong'),('identity','111')]:
            f=source_fixture();f[2][field]=value
            with self.subTest(field=field),self.assertRaises(PublishError):self.bind(f)

    def test_public_post_and_original_video_must_not_be_interchanged(self):
        for node,key,value in [('accepted','remote_id','111'),('accepted','identity','111'),('accepted','mblogid','WrongPost'),
                               ('public','media_id','111'),('public','title','不同标题'),('public','authored_text','不同文案'),
                               ('edit','remote_id','111'),('edit','duration',46),('edit','width',1280),
                               ('edit','creation_type','转载')]:
            f=source_fixture();target={'accepted':f[1],'public':f[3]['public'],'edit':f[3]['edit']}[node];target[key]=value
            with self.subTest(node=node,key=key),self.assertRaises(PublishError):self.bind(f)

    def test_cloud_media_and_persisted_source_metadata_cannot_diverge(self):
        for key,value in [('media_id','111'),('source_asset_id','other'),('source_sha256','f'*64),
                          ('chunk_count',3),('size',13),('tracking_scope','native_cloud_file_accepted_only'),
                          ('width',False),('duration',float('nan'))]:
            f=source_fixture();f[2]['native_video'][key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):self.bind(f)
        f=source_fixture();f[2]['native_video']['media_id']='12345678'
        for event in f[2]['upload_events']:
            for node in (event['fields'],event['body']):
                if 'media_id' in node:node['media_id']='12345678'
        with self.assertRaises(PublishError):self.bind(f)

    def test_cover_must_bind_original_crop_upload_pid_and_both_remote_views(self):
        for change in ('missing','source','crop','upload','public_pid','edit_pid','ratio','dimensions'):
            f=source_fixture();c=f[2]['cover']
            if change=='missing':f[2]['cover']=None
            elif change=='source':c['prepared']['source_sha256']='f'*64
            elif change=='crop':c['crop']['crop']['x']=1
            elif change=='upload':c['responses'][0]['pid']='WrongCoverPid'
            elif change=='public_pid':f[3]['public']['cover_pid']='WrongCoverPid'
            elif change=='edit_pid':f[3]['edit']['cover_pid']='WrongCoverPid'
            elif change=='ratio':c['prepared']['width']=675
            elif change=='dimensions':c['prepared']['width']=True
            with self.subTest(change=change),self.assertRaises(PublishError):self.bind(f)

    def test_optional_default_cover_not_claimed_as_a_local_asset(self):
        f=source_fixture();f[0]['cover_asset_id']=None;f[0]['assets']=f[0]['assets'][:1]
        f[2]['pack_digest']=digest(f[0]);del f[2]['cover']
        result=self.bind(f);self.assertEqual(len(result['asset_bindings']),1)
        f[0]['assets'].append(dict(id='extra',kind='image',sha256='b'*64));f[2]['pack_digest']=digest(f[0])
        with self.assertRaises(PublishError):self.bind(f)

    def test_empty_sources_or_observation_rejected(self):
        for index,value in [(0,{}),(1,None),(2,{}),(3,{})]:
            f=list(source_fixture());f[index]=value
            with self.subTest(index=index),self.assertRaises(PublishError):self.bind(f)


if __name__=='__main__':unittest.main()
