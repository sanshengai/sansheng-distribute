import copy
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import unittest
from unittest.mock import Mock

from douyin_post import account, item, cover_uri, observe_visible
from publish_core import PublishError


def native():
    return dict(status_code=0, items=[dict(id='1234567890123456789', user_id='987654321',
        type=3, collaborative=False, review=dict(status=2),
        visibility=dict(close_friend=0, part_see=0, status=1),
        video_info=dict(duration='123267'), cover=dict(uri='tos-cn-i-dy/example123'),
        description='系列 S1E01｜标题\n正文 #话题 ')])


class DouyinPostTests(unittest.TestCase):
    def test_native_observation_keeps_full_description_and_durable_ids(self):
        v = item(native(), '987654321', '1234567890123456789', '系列 S1E01｜标题\n正文 #话题 ')
        self.assertEqual(v['duration_ms'],123267)
        self.assertTrue(v['description'].endswith(' '))
        self.assertNotIn('content_verified',v)

    def test_author_text_type_and_visibility_are_not_inferred(self):
        changes=[('user_id','111'),('description','别的内容'),('type',True),
                 ('collaborative',True),('review',dict(status=1)),
                 ('visibility',dict(close_friend=0,part_see=0,status=0)),
                 ('visibility',dict(close_friend=False,part_see=0,status=1)),
                 ('video_info',dict(duration='0')),('cover',dict(uri='../other'))]
        for key,value in changes:
            with self.subTest(key=key,value=value):
                b=native(); b['items'][0][key]=value
                with self.assertRaises(PublishError):
                    item(b,'987654321','1234567890123456789',native()['items'][0]['description'])

    def test_missing_duplicate_or_failed_response_cannot_prove_absence(self):
        cases=[{},dict(status_code=True,items=[]),dict(status_code=1,items=[]),
               dict(status_code=0,items=[])]
        duplicate=native();duplicate['items']*=2;cases.append(duplicate)
        for b in cases:
            with self.subTest(body=b),self.assertRaises(PublishError):
                item(b,'987654321','1234567890123456789')
        for uid,rid in [(None,'123'),('987',''),(987,'123'),('987','01')]:
            with self.assertRaises(PublishError):item(native(),uid,rid)

    def test_account_requires_actual_registered_person_session(self):
        b=dict(status_code=0,uid='987654321',is_confer_login=False,is_log_as_person=True,is_registered=True)
        self.assertEqual(account(b),'987654321')
        for k,v in [('uid',987654321),('is_confer_login',True),('is_log_as_person',False),
                    ('is_registered',False),('status_code',True)]:
            c=copy.deepcopy(b);c[k]=v
            with self.subTest(key=k),self.assertRaises(PublishError):account(c)
        with self.assertRaises(PublishError):account({})

    def test_cover_uri_ignores_expiring_signature_but_requires_native_origin(self):
        self.assertEqual(cover_uri('url("https://p26-sign.douyinpic.com/tos-cn-i-dy/example123~resize.webp?signature=expired")'), 'tos-cn-i-dy/example123')
        for v in ['', 'none','url("https://evil.com/tos-cn-i-dy/example123~x")',
                  'url("https://p26-sign.douyinpic.com.evil.com/tos-cn-i-dy/example123~x")',
                  'url("https://u:p@p26-sign.douyinpic.com/tos-cn-i-dy/example123~x")']:
            with self.subTest(css=v),self.assertRaises(PublishError):cover_uri(v)

    def test_visible_card_requires_exact_full_text_cover_and_published_state(self):
        value=item(native(),'987654321','1234567890123456789')
        page=Mock();card=Mock();title=Mock();cover=Mock();published=Mock()
        page.locator.return_value.all.return_value=[card]
        def locator(selector):return title if selector.startswith('[class^="info-title') else cover
        card.locator.side_effect=locator;card.is_visible.return_value=True
        title.count.return_value=1;title.text_content.return_value=value['description']
        cover.count.return_value=1;cover.is_visible.return_value=True
        cover.evaluate.return_value='url("https://p26-sign.douyinpic.com/tos-cn-i-dy/example123~x")'
        card.get_by_text.return_value=published;published.count.return_value=1;published.is_visible.return_value=True
        observe_visible(page,value)
        for obj,attr,bad,good in [(title,'text_content','截断',value['description']),
                                  (cover,'is_visible',False,True),(published,'count',0,1),
                                  (cover,'evaluate','url("https://p26-sign.douyinpic.com/tos-cn-i-dy/other~x")',cover.evaluate.return_value)]:
            with self.subTest(field=attr):
                getattr(obj,attr).return_value=bad
                with self.assertRaises(PublishError):observe_visible(page,value)
                getattr(obj,attr).return_value=good
        page.locator.return_value.all.return_value=[card,card]
        with self.assertRaises(PublishError):observe_visible(page,value)

    def test_readback_is_readonly_partial_observation_never_content_verification(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        import douyin_post as post
        page=Mock();page.url=post.HOME
        account_body=dict(status_code=0,uid='987654321',is_confer_login=False,is_log_as_person=True,is_registered=True)
        callback=None;closed=[]
        def listen(event,fn):
            nonlocal callback
            callback=fn
        def response(path,body):
            r=Mock();r.url='https://creator.douyin.com'+path;r.status=200;r.request.method='GET';r.json.return_value=body;callback(r)
        def goto(url,**kwargs):
            page.url=url;response(post.ACCOUNT_PATH,account_body)
        def navigate(p,node):
            page.url=post.MANAGER;response(post.LIST_PATH,native())
        page.on.side_effect=listen;page.goto.side_effect=goto
        page.get_by_text.return_value.count.return_value=1
        @contextmanager
        def window(*args,**kwargs):
            try:yield page
            finally:closed.append(True)
        with patch.object(post,'background_window',window),patch.object(post,'click_background',side_effect=navigate) as click,patch.object(post,'observe_visible') as visible:
            result=post.readback(Mock(),cdp_url='http://127.0.0.1:9333',public_identity='fixture-public',remote_id='1234567890123456789',expected_description=native()['items'][0]['description'])
        self.assertIs(result['content_verified'],False)
        self.assertIs(result['source_media_verified'],False)
        self.assertIn('declaration',result['unverified_fields'])
        self.assertIn('collection_membership',result['unverified_fields'])
        page.goto.assert_called_once_with(post.HOME,wait_until='domcontentloaded',timeout=25000)
        click.assert_called_once();visible.assert_called_once();self.assertEqual(closed,[True])

    def test_invalid_query_identity_does_not_open_browser_window(self):
        from unittest.mock import patch
        import douyin_post as post
        with patch.object(post,'background_window') as window:
            for public,rid in [('', '123'),('valid-id',None),('valid-id','01'),(None,'123')]:
                with self.subTest(public=public,rid=rid),self.assertRaises(PublishError):
                    post.readback(Mock(),cdp_url='http://127.0.0.1:9333',public_identity=public,remote_id=rid)
            window.assert_not_called()
