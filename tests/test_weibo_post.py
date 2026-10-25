"""Weibo native detail shape, immutable media bindings and independent rendering."""
import contextlib
import copy
import html
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import weibo_post as post
from publish_core import PublishError

UID='1234567890'; PID1='fixturePictureOne'; PID2='fixturePictureTwo'


def body(text='完整长文\n第二段', pictures=None):
    pictures=[] if pictures is None else pictures
    return dict(ok=1,id=9876543210,idstr='9876543210',mid='9876543210',mblogid='Rfixture1',
                user=dict(id=int(UID),idstr=UID),visible=dict(type=0),isLongText=True,
                longText=dict(content=text),text_raw=text,text=html.escape(text).replace('\n','<br />'),
                pic_ids=pictures,pic_num=len(pictures))


def pack(text='完整长文\n第二段', pictures=None):
    pictures=[] if pictures is None else pictures
    return dict(content_type='images' if pictures else 'text',fields=dict(composed_text=text),
                assets=[dict(id='image'+str(i),sha256=str(i)*64) for i,_ in enumerate(pictures)])


def uploaded(p):
    return [dict(id=a['id'],sha256=a['sha256'],pid=pid) for a,pid in zip(p['assets'],[PID1,PID2])]


class Value:
    def __init__(self, n=1, visible=True):self.n=n;self.visible=visible
    def count(self):return self.n
    def is_visible(self):return self.visible
    def wait_for(self, **kw):pass


class Text(Value):
    def __init__(self,page):super().__init__();self.page=page
    def inner_html(self):return self.page.html
    def evaluate(self, script):return self.page.body_visible


class Pictures:
    def __init__(self,page):self.page=page
    def evaluate_all(self, script):return self.page.pictures


class Article(Value):
    def __init__(self,page):super().__init__();self.page=page
    def is_visible(self):return self.page.visible
    def locator(self, selector):
        if selector.startswith('header a[href='):return Value(self.page.permalinks)
        if selector=='.wbpro-feed-ogText > div':return Text(self.page)
        if selector=='video':return Value(self.page.videos)
        if selector=='.picture img':return Pictures(self.page)
        raise AssertionError(selector)


class Articles:
    def __init__(self,page):self.page=page
    def all(self):return [Article(self.page)]


class Page:
    def __init__(self,b):
        self.body=b;self.listeners={};self.html=b['text'];self.visible=True
        self.permalinks=1;self.videos=0;self.body_visible=True;self.url='';self.pictures=[
            dict(src='https://wx1.sinaimg.cn/orj360/'+pid+'.jpg',loaded=True) for pid in b['pic_ids']]
    def locator(self,selector):
        if selector=='article':return Articles(self)
        if selector=='article .wbpro-feed-ogText':return Value()
        raise AssertionError(selector)
    def on(self,event,callback):self.listeners[event]=callback
    def goto(self,url,**kw):
        self.url=url
        r=type('Response',(),dict(url='https://weibo.com/ajax/statuses/show?id=9876543210',status=200,
            request=type('Request',(),dict(method='GET'))(),json=lambda _:self.body))()
        self.listeners['response'](r)


class WeiboPostTests(unittest.TestCase):
    def test_detail_long_body_and_emoji_newlines(self):
        b=body('第一段😀\n\n第二段 & 小于 < 数值');self.assertEqual(post.facts(b)['text'],b['text_raw'])
        b['text']='第一段<img alt="[开心]" src="emoji.png"/><br>第二段'
        b['text_raw']=b['longText']['content']='第一段[开心]\n第二段'
        self.assertEqual(post.facts(b)['text'],'第一段[开心]\n第二段')

    def test_short_links_topics_and_duplicate_native_entity(self):
        short='http://t.cn/native1';url='https://example.org/read?a=1&b=2'
        b=body('全文：'+short+'\n#知识图解#')
        b['url_struct']=[dict(short_url=short,long_url=url,ori_url=url)]*2
        b['text']='全文：<a href="https://weibo.cn/sinaurl?u='+quote(url,safe='')+'"><img class="icon-link" title="'+short+'"/>网页链接</a><br><a href="//s.weibo.com/weibo?q=topic">#知识图解#</a>'
        self.assertEqual(post.facts(b)['text'],'全文：'+url+'\n#知识图解#')
        changed=copy.deepcopy(b);changed['url_struct'][1]=dict(short_url=short,long_url='https://example.org/wrong')
        with self.assertRaises(PublishError):post.facts(changed)
        b['text']=b['text'].replace(quote(url,safe=''),quote('https://example.org/wrong',safe=''))
        with self.assertRaises(PublishError):post.facts(b)

    def test_truncated_list_cannot_become_full_detail(self):
        b=body();del b['longText']
        with self.assertRaises(PublishError):post.facts(b)
        b=body();b['text_raw']='截断…'
        with self.assertRaises(PublishError):post.facts(b)
        b=body();b['text']='不同的可见文字'
        with self.assertRaises(PublishError):post.facts(b)

    def test_invalid_identity_privacy_and_other_media_reject(self):
        for field,value in [('idstr','111'),('mid','111'),('mblogid','../wrong'),
            ('visible',dict(type=1)),('visible',dict(type=False)),('deleted',True),
            ('retweeted_status',dict(id='other')),('mix_media_info',dict(items=[{}])),
            ('page_info',dict(type='video')),('ok',0)]:
            b=body();b[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(PublishError):post.facts(b)
        b=body();b['user']['id']=8888
        with self.assertRaises(PublishError):post.facts(b)
        with self.assertRaises(PublishError):post.identity(body(),expected='555555')

    def test_exact_picture_identity_order_and_source_hash_binding(self):
        b=body(pictures=[PID1,PID2]);p=pack(pictures=[PID1,PID2]);u=uploaded(p)
        v=post.facts(b);accepted=post.identity(b)
        post.verify_facts(v,p,UID,accepted,u)
        for mutate in ['order','pid','source','account','id','missing_identity','text']:
            changed=copy.deepcopy(v);uploads=copy.deepcopy(u);a=copy.deepcopy(accepted)
            if mutate=='order':changed['pic_ids'].reverse()
            elif mutate=='pid':changed['pic_ids'][0]='wrongPictureIdentity'
            elif mutate=='source':uploads[0]['sha256']='f'*64
            elif mutate=='account':changed['identity']='2222222222'
            elif mutate=='id':a['remote_id']='1111111111'
            elif mutate=='missing_identity':del a['identity']
            elif mutate=='text':changed['text']='different'
            with self.subTest(mutate=mutate),self.assertRaises(PublishError):post.verify_facts(changed,p,UID,a,uploads)
        for change in [dict(pic_ids=[PID1,PID1]),dict(pic_num=1),dict(pic_ids=None)]:
            changed=copy.deepcopy(b);changed.update(change)
            with self.assertRaises(PublishError):post.facts(changed)

    def test_no_hidden_html_or_unmapped_external_link(self):
        for markup in ['<span hidden>完整长文</span>', '<script>完整长文</script>',
                '<span style="display:none">完整长文</span>', '<a href="https://evil.example/">完整长文</a>',
                '<a href="javascript://s.weibo.com/">完整长文</a>', '<img src="missing-alt.png">','<span onclick="x()">完整长文</span>']:
            with self.subTest(markup=markup),self.assertRaises(PublishError):post.rendered(markup,{})

    def test_real_detail_route_with_native_body_and_visible_same_post(self):
        b=body(pictures=[PID1,PID2]);p=pack(pictures=[PID1,PID2]);page=Page(b)
        @contextlib.contextmanager
        def window(anchor,**kw):yield page
        with patch.object(post,'background_window',side_effect=window) as owned:
            observed=post.readback(object(),p,UID,post.identity(b),uploaded(p),'http://127.0.0.1:9222')
        self.assertTrue(observed['content_verified']);self.assertEqual(observed['verified_asset_ids'],['image0','image1'])
        self.assertFalse(observed['remote_media_bytes_verified'])
        self.assertEqual(page.url,'https://weibo.com/detail/9876543210');self.assertEqual(owned.call_count,1)

    def test_visible_body_permalink_or_image_changes_cannot_verify(self):
        b=body(pictures=[PID1,PID2]);v=post.facts(b)
        for mutate in ['body','css_hidden','hidden','permalink','video','order','missing_image','unloaded','wrong_host']:
            page=Page(b)
            if mutate=='body':page.html='被替换的正文'
            elif mutate=='css_hidden':page.body_visible=False
            elif mutate=='hidden':page.visible=False
            elif mutate=='permalink':page.permalinks=0
            elif mutate=='video':page.videos=1
            elif mutate=='order':page.pictures.reverse()
            elif mutate=='missing_image':page.pictures.pop()
            elif mutate=='unloaded':page.pictures[0]['loaded']=False
            elif mutate=='wrong_host':page.pictures[0]['src']='https://evil.example/'+PID1+'.jpg'
            with self.subTest(mutate=mutate),self.assertRaises(PublishError):post.observe_visible(page,v,v['text'])

    def test_empty_inputs_fail_closed(self):
        for b in [{},None,[],dict(ok=1)]:
            with self.subTest(body=b),self.assertRaises(PublishError):post.facts(b)
        with self.assertRaises(PublishError):post.status_url(UID,'')
        with self.assertRaises(PublishError):post.rendered('',{})
        p=pack(pictures=[PID1]);self.assertRaises(PublishError,post.check_uploads,p,[])


if __name__=='__main__':unittest.main()
