"""Synthetic native responses: one submit, durable ID, independent same-post verification."""
import contextlib
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import browser_engine as engine
import x_post
import x_publish_browser as publisher
from publish_core import PublishError, Store, read_json, run, validate, write_json
from test_browser_engine import Page


def tweet(text='完整文案', ident='123456789', handle='demo'):
    return dict(__typename='Tweet', rest_id=ident,
                core=dict(user_results=dict(result=dict(rest_id='456', core=dict(screen_name=handle),
                                                       privacy=dict(protected=False)))),
                legacy=dict(id_str=ident, user_id_str='456', full_text=text,
                            entities=dict(urls=[]), is_quote_status=False))


def detail(result):
    return dict(data=dict(threaded_conversation_with_injections_v2=dict(instructions=[
        dict(entries=[dict(content=dict(itemContent=dict(tweet_results=dict(result=result))))])
    ])))


class Response:
    def __init__(self, body, operation='CreateTweet', status=200):
        self.body = body; self.url = 'https://x.com/i/api/graphql/native-shape/'+operation
        self.status = status
        self.request = type('Request', (), {'method':'POST'})()
    def json(self):
        return self.body


class Button:
    def count(self):return 1
    def is_visible(self):return True
    def is_disabled(self):return False


class Composer(Page):
    def __init__(self):
        super().__init__(); self.listeners = {}; self.clicks = 0
    def locator(self, selector):
        if 'tweetButton' in selector:return Button()
        node = super().locator(selector)
        node.wait_for = lambda **kw: None
        return node
    def goto(self, url, **kw):self.url = url
    def on(self, event, callback):self.listeners[event] = callback
    def remove_listener(self, event, callback):self.listeners.pop(event)
    def wait_for_timeout(self, milliseconds):pass


class Text:
    def __init__(self, page):self.page = page
    def count(self):return 1
    def inner_text(self):return self.page.rendered_text
    def evaluate(self, script, mappings):return self.page.rendered_text


class Links:
    def __init__(self, page):self.page = page
    def evaluate_all(self, script):return self.page.permalinks


class Articles:
    def __init__(self, page):self.page = page
    @property
    def first(self):return self
    def wait_for(self, **kw):pass
    def all(self):return [self]
    def is_visible(self):return True
    def locator(self, selector):
        if selector == 'a[href]':return Links(self.page)
        if 'tweetText' in selector:return Text(self.page)
        return type('Media', (), {'count':lambda _:self.page.media})()


class DetailPage:
    def __init__(self, result):
        self.result = result; self.listeners = {}; self.url = ''
        self.rendered_text = result['legacy']['full_text']; self.media = 0
        self.permalinks = ['/demo/status/'+result['rest_id']]
    def on(self, event, callback):self.listeners[event] = callback
    def goto(self, url, **kw):
        self.url = url
        self.listeners['response'](Response(detail(self.result), 'TweetDetail'))
    def evaluate(self, script):return dict(outer=828, inner=828, hidden=True)
    def locator(self, selector):return Articles(self)


class NativePostFactsTests(unittest.TestCase):
    def setUp(self):self.pack = dict(fields=dict(composed_text='完整文案'))

    def test_actual_id_author_and_entire_content_required(self):
        value = x_post.detail_result(detail(tweet()), '123456789')
        self.assertEqual(x_post.verify_facts(value, self.pack, '@demo', author_id='456')['remote_id'], '123456789')
        for changes in [dict(remote_id='9'), dict(identity='@other'), dict(author_id='789'),
                        dict(text='完整文案少一个字'), dict(media_count=1), dict(protected=True),
                        dict(protected=None), dict(reply_to='1'), dict(quoted=True), dict(limited=True)]:
            with self.subTest(changes=changes), self.assertRaises(PublishError):
                x_post.verify_facts(dict(value, **changes), self.pack, '@demo', remote_id='123456789', author_id='456')

    def test_link_expansion_respects_utf16_positions_and_decodes_once(self):
        value = tweet('😀 &amp; https://t.co/abc')
        value['legacy']['entities']['urls'] = [dict(url='https://t.co/abc', expanded_url='https://example.org/read', indices=[9,25])]
        self.assertEqual(x_post.facts(value)['text'], '😀 & https://example.org/read')
        value['legacy']['entities']['urls'][0]['indices'][0] = 8
        with self.assertRaisesRegex(PublishError, '链接位置'):x_post.facts(value)

    def test_note_text_does_not_use_truncated_legacy_and_identity_cannot_disagree(self):
        value = tweet('truncated…')
        value['note_tweet'] = dict(note_tweet_results=dict(result=dict(text='完整文案', entity_set=dict(urls=[]))))
        self.assertEqual(x_post.facts(value)['text'], '完整文案')
        value['legacy']['user_id_str'] = 'other'
        with self.assertRaises(PublishError):x_post.facts(value)

    def test_title_time_or_another_tweet_never_establish_identity(self):
        for body in [dict(title='完整文案', time='2026-10-25'), detail(tweet(ident='999')), {'errors':[{}]}, {}]:
            with self.subTest(body=body), self.assertRaises(PublishError):x_post.detail_result(body, '123456789')
        body = detail(tweet());body['data']['threaded_conversation_with_injections_v2']['instructions'].append(
            dict(conflicting= tweet('different same-ID content')))
        with self.assertRaisesRegex(PublishError, '唯一'):x_post.detail_result(body, '123456789')

    def test_capture_saves_actual_id_without_claiming_content_or_publication(self):
        rows=[];capture=x_post.Capture(self.pack, '@demo', rows.append)
        value=tweet();value['legacy'].pop('full_text')
        capture.response(Response(dict(data=dict(create_tweet=dict(tweet_results=dict(result=value))))))
        self.assertEqual(rows[0]['remote_id'], '123456789')
        self.assertNotIn('content_verified', rows[0])
        self.assertIsNone(capture.error)
        capture.response(Response({}))
        self.assertIsNotNone(capture.error);self.assertEqual(len(rows), 1)

    def test_wrong_account_response_never_bound(self):
        rows=[];capture=x_post.Capture(self.pack, '@demo', rows.append)
        capture.response(Response(dict(data=dict(create_tweet=dict(tweet_results=dict(result=tweet(handle='other')))))))
        self.assertEqual(rows, []);self.assertIsNotNone(capture.error)


class XPublicationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.profile_path=self.root/'profile.json'
        self.profile={'configured_modules':['x'], 'browser_profile':str(self.root/'dedicated'),
            'browser_runtime':{'cdp_url':'http://127.0.0.1:9333'},
            'accounts':{'editor':{'platforms':['x'], 'public_identities':{'x':'@demo'}}},
            'adapters':{'x':{'enabled_capabilities':['text:publish'], 'execute':[sys.executable,
                str(ROOT/'scripts/browser_engine.py'),'execute','--profile',str(self.profile_path)],
                'verify':[sys.executable,str(ROOT/'scripts/browser_engine.py'),'verify','--profile',str(self.profile_path)]}}}
        write_json(self.profile_path,self.profile)
        (self.root/'source.txt').write_text('完整文案', encoding='utf-8')
        self.manifest={'schema_version':1,'job_id':'x-publish-fixture','producer':'files','content_status':'final',
            'source_root':str(self.root),'documents':[dict(id='source',path='source.txt',format='text')],'assets':[],'media_slots':[],
            'targets':[{'id':'x','platform':'x','account':'editor','content_type':'text','endpoint':'publish','text':'完整文案','document_id':'source'}]}
        validate(self.manifest,sealing=True);self.path=self.root/'manifest.json';write_json(self.path,self.manifest)
        self.store=Store(self.root/'state');self.addCleanup(self.store.close)
        self.key=self.store.prepare(self.manifest,'x')['key'];self.store.authorize(self.manifest,'x','offline fixture only')
        self.page=Composer();self.detail=DetailPage(tweet());self.actions=[];self.send_mode='ok'
        self.click_observations=[]
        self.addCleanup(patch.stopall)
        @contextlib.contextmanager
        def connection(profile,**kwargs):yield object()
        @contextlib.contextmanager
        def window(*a,**kw):yield self.page
        @contextlib.contextmanager
        def tab(*a,**kw):yield self.detail
        patch.object(engine,'connect',side_effect=connection).start()
        patch.object(engine,'select_page',return_value=self.page).start()
        patch.object(publisher,'background_window',side_effect=window).start()
        patch.object(x_post,'background_tab',side_effect=tab).start()
        patch('runtime.inspect',return_value={'ok':True,'python':'/private/runtime/bin/python3'}).start()
        def invoke(config,action,path,*,lock_fd):
            self.actions.append(action);return engine.dispatch(path,action,self.profile,lock_fd)
        patch('publish_core.invoke',side_effect=invoke).start()
        def click(page,button,*,before_input=None):
            receipt=read_json(engine.marker_path(self.request))
            observation=dict(durable_attempt=bool(receipt.get('submission_attempted_at')),
                             core_state=self.store.status(self.key)['state'])
            self.click_observations.append(observation)
            before_input()
            observation['native_input_checkpoint']=bool(read_json(engine.marker_path(self.request)).get('native_input_checkpointed_at'))
            page.clicks+=1
            if self.send_mode=='no_receipt':return
            value=tweet(self.page.text)
            page.listeners['response'](Response(dict(data=dict(create_tweet=dict(tweet_results=dict(result=value))))))
            observation['accepted_id']=read_json(engine.marker_path(self.request)).get('accepted',{}).get('remote_id')
            if self.send_mode=='lost_click_reply':raise RuntimeError('native reply lost')
        patch.object(publisher,'click_background',side_effect=click).start()
        self.request=self.store.directory/'requests'/f'{self.key}.json'

    def test_native_click_once_then_independent_same_id_readback_and_restart(self):
        result=run(self.path,'x',self.profile,self.store)
        self.assertEqual(result['state'],'published');self.assertEqual(result['remote_id'],'123456789')
        run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1);self.assertEqual(self.actions,['execute','verify','verify'])
        self.assertEqual(len(self.page.writes),1)
        self.assertEqual(self.click_observations,[dict(durable_attempt=True,core_state='submission_unknown',native_input_checkpoint=True,
                                                      accepted_id='123456789')])

    def test_lost_click_reply_still_saves_acceptance_before_return(self):
        self.send_mode='lost_click_reply'
        self.assertEqual(run(self.path,'x',self.profile,self.store)['state'],'published')
        self.assertEqual(self.page.clicks,1)

    def test_unknown_without_id_never_clicks_again_after_resume(self):
        self.send_mode='no_receipt'
        with patch.object(publisher.time,'monotonic',side_effect=[0,21]):
            with self.assertRaisesRegex(PublishError,'结果未知'):run(self.path,'x',self.profile,self.store)
        with self.assertRaisesRegex(PublishError,'受理身份'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_bad_detail_preserves_accepted_id_and_only_rechecks_after_correction(self):
        self.detail.result=tweet('different text')
        with self.assertRaisesRegex(PublishError,'完整文字'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['remote_id'],'123456789')
        self.assertEqual(self.store.status(self.key)['state'],'pending')
        self.detail.result=tweet()
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.page.clicks,1)

    def test_other_detail_id_or_visible_text_never_counts_as_published(self):
        self.detail.result=tweet(ident='999')
        with self.assertRaisesRegex(PublishError,'原作品身份'):run(self.path,'x',self.profile,self.store)
        self.detail.result=tweet();self.detail.rendered_text='truncated…'
        with self.assertRaisesRegex(PublishError,'可见完整文案'):run(self.path,'x',self.profile,self.store,resume=True)
        self.detail.rendered_text='完整文案';self.detail.media=1
        with self.assertRaisesRegex(PublishError,'未交接媒体'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_existing_author_text_stops_before_fill_and_click(self):
        self.page.text='author draft'
        with self.assertRaisesRegex(PublishError,'已有文字'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[]);self.assertEqual(self.page.clicks,0)

    def test_wrong_live_account_stops_before_fill_and_click(self):
        self.page.handle='@other'
        with self.assertRaisesRegex(PublishError,'实际账号'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[]);self.assertEqual(self.page.clicks,0)

    def test_durable_attempt_write_failure_prevents_native_click(self):
        def persist(path, value):
            if value.get('submission_attempted_at') and not value.get('accepted'):
                raise OSError('durable store unavailable')
            write_json(path, value)
        with patch.object(publisher,'write_json',side_effect=persist):
            with self.assertRaisesRegex(OSError,'durable store'):run(self.path,'x',self.profile,self.store)
        publisher.click_background.assert_not_called()
        self.assertEqual(self.click_observations,[])
        self.assertEqual(self.page.clicks,0)
        with self.assertRaisesRegex(PublishError,'受理身份'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,0)

    def test_pre_input_rejection_is_diagnosed_without_claiming_native_click_or_retry(self):
        with patch.object(publisher,'click_background',side_effect=PublishError('target is obscured')):
            with self.assertRaisesRegex(PublishError,'原生点击前'):run(self.path,'x',self.profile,self.store)
        saved=read_json(engine.marker_path(self.request))
        self.assertEqual(saved['click_error_phase'],'before_native_input')
        self.assertEqual(saved['click_error'],'target is obscured')
        self.assertNotIn('native_input_checkpointed_at',saved)
        self.assertNotIn('click_reply_unknown',saved)
        with self.assertRaisesRegex(PublishError,'受理身份'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,0)

    def test_failed_checkpoint_write_is_before_input_and_never_retries(self):
        def persist(path,value):
            if value.get('native_input_checkpointed_at'):
                raise OSError('checkpoint unavailable')
            write_json(path,value)
        with patch.object(publisher,'write_json',side_effect=persist), \
                patch.object(publisher.time,'monotonic',side_effect=[0,21]):
            with self.assertRaises((PublishError,OSError)):
                run(self.path,'x',self.profile,self.store)
        saved=read_json(engine.marker_path(self.request))
        self.assertEqual(saved.get('click_error_phase'),'before_native_input')
        self.assertNotIn('native_input_checkpointed_at',saved)
        self.assertNotIn('click_reply_unknown',saved)
        self.assertEqual(self.page.clicks,0)
        with self.assertRaisesRegex(PublishError,'受理身份'):
            run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,0)

    def test_last_input_guard_preserves_changed_author_text_without_sending(self):
        click=publisher.click_background.side_effect
        def changed(page,button,*,before_input):
            page.text='author changed this before native input'
            return click(page,button,before_input=before_input)
        with patch.object(publisher,'click_background',side_effect=changed):
            with self.assertRaisesRegex(PublishError,'原生点击前'):
                run(self.path,'x',self.profile,self.store)
        saved=read_json(engine.marker_path(self.request))
        self.assertEqual(saved['click_error_phase'],'before_native_input')
        self.assertNotIn('native_input_checkpointed_at',saved)
        self.assertEqual(self.page.text,'author changed this before native input')
        self.assertEqual(self.page.clicks,0)
        with self.assertRaisesRegex(PublishError,'受理身份'):
            run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual((self.page.clicks,len(self.page.writes)),(0,1))

    def test_same_handle_different_author_or_wrong_permalink_cannot_verify(self):
        self.detail.result['core']['user_results']['result']['rest_id']='789'
        self.detail.result['legacy']['user_id_str']='789'
        with self.assertRaisesRegex(PublishError,'账号与发送回执'):run(self.path,'x',self.profile,self.store)
        self.detail.result=tweet();self.detail.permalinks=['/demo/status/999']
        with self.assertRaisesRegex(PublishError,'唯一可见'):run(self.path,'x',self.profile,self.store,resume=True)
        self.detail.permalinks=['/demo/status/123456789']
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.page.clicks,1)

    def test_default_capability_is_not_silently_upgraded(self):
        self.profile['adapters']['x']['enabled_capabilities']=['text:prefill']
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.writes,[]);self.assertEqual(self.page.clicks,0)


if __name__=='__main__':unittest.main()
