"""The real core/upload/submit/readback path, with synthetic native browser I/O."""
import contextlib
import copy
import unittest
from unittest.mock import patch

import test_x_video as upload
import test_x_publish as post
import browser_engine as engine
import x_post
import x_publish_browser as publisher
from publish_core import PublishError, read_json, write_json, run, validate
from platform_pack import build_pack


def video_tweet(text):
    result = post.tweet(text+' https://t.co/clip')
    start = len(text.encode('utf-16-le'))//2+1
    media = dict(id_str='12345', media_key='7_12345', type='video',
                 url='https://t.co/clip', indices=[start,start+17],
                 original_info=dict(width=720,height=1280),
                 video_info=dict(duration_millis=4000), ext_media_availability=dict(status='Available'))
    result['legacy']['entities']['media'] = [copy.deepcopy(media)]
    result['legacy']['extended_entities'] = dict(media=[media])
    return result


class Article(post.Articles):
    def locator(self, selector):
        if selector in ('[data-testid="videoPlayer"]','[data-testid="tweetPhoto"]',
                        '[data-testid="tweetPhoto"]:not(:has([data-testid="videoPlayer"]))'):
            page = self.page
            class Media:
                @property
                def first(self):return self
                def wait_for(self,**kwargs):pass
                def count(self):
                    if selector=='[data-testid="videoPlayer"]':return page.players
                    return page.photos if ':not' in selector else page.photos+page.video_wrappers
                def is_visible(self):return page.player_visible
            return Media()
        return super().locator(selector)


class Detail(post.DetailPage):
    def __init__(self, result):
        super().__init__(result);self.players=1;self.photos=0;self.video_wrappers=0;self.player_visible=True
    def locator(self, selector):return Article(self)


class XVideoPublicationTests(unittest.TestCase):
    request = upload.XVideoDispatchTests.request

    def setUp(self):
        upload.XVideoDispatchTests.setUp(self)
        self.profile['adapters']['x']['enabled_capabilities']=['video:publish']
        write_json(self.profile_path,self.profile)
        self.manifest['targets'][0]['endpoint']='publish'
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'x')['key']
        self.store.authorize(self.manifest,'x','offline fixture, no live platform')
        self.pack=build_pack(self.manifest,'x',self.profile)
        self.detail=Detail(video_tweet(self.pack['fields']['composed_text']))
        self.detail.rendered_text=self.pack['fields']['composed_text']
        self.clicks=0;self.click_receipts=[];self.mode='ok'
        patch.object(publisher,'background_window',side_effect=upload.x_video_browser.background_window.side_effect).start()
        @contextlib.contextmanager
        def detail_window(anchor,**kwargs):yield self.detail
        patch.object(x_post,'background_tab',side_effect=detail_window).start()
        patch.object(x_post,'background_window',side_effect=detail_window).start()
        def click(page,button,*,before_input):
            before_input()
            saved=read_json(engine.marker_path(self.request))
            self.assertTrue(saved['submission_attempted_at']);self.assertTrue(saved['native_input_checkpointed_at'])
            self.assertEqual(saved['publication_media']['native']['media_id'],'12345')
            self.clicks+=1
            result=video_tweet(page.text)
            body={} if self.mode=='missing_receipt' else dict(data=dict(create_tweet=dict(tweet_results=dict(result=result))))
            for callback in list(page.listeners):callback(post.Response(body))
            self.click_receipts.append(read_json(engine.marker_path(self.request)).get('accepted'))
            if self.mode=='lost_reply':raise RuntimeError('lost native input reply')
        patch.object(publisher,'click_background',side_effect=click).start()

    def test_processed_original_sent_once_same_media_readback_and_source_free_resume(self):
        result=run(self.path,'x',self.profile,self.store)
        self.assertEqual(result['state'],'published');self.assertEqual(result['remote_id'],'123456789')
        self.assertEqual(self.click_receipts[0]['remote_id'],'123456789')
        (self.root/'video.mp4').unlink()
        self.page.context.ident='closed-composer'
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual((self.clicks,len(self.page.uploads),len(self.page.writes)),(1,1,1))
        self.assertEqual(self.anchor.text,'existing author draft')

    def test_lost_reply_retains_actual_id_without_second_selection_or_send(self):
        self.mode='lost_reply'
        self.assertEqual(run(self.path,'x',self.profile,self.store)['state'],'published')
        self.assertEqual(self.clicks,1);self.assertEqual(len(self.page.uploads),1)

    def test_missing_acceptance_never_reuploads_or_reclicks_on_resume(self):
        self.mode='missing_receipt'
        with self.assertRaisesRegex(PublishError,'结果未知'):run(self.path,'x',self.profile,self.store)
        with self.assertRaisesRegex(PublishError,'受理身份'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.clicks,1);self.assertEqual(len(self.page.uploads),1)

    def lose_acceptance(self):
        self.mode='missing_receipt'
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store)
        self.page.url='https://x.com/home'

    def test_candidate_recovers_same_video_without_upload_or_send(self):
        self.lose_acceptance()
        result=run(self.path,'x',self.profile,self.store,resume=True,
                   recover_remote_id='123456789',recovery_platform='x')
        self.assertEqual(result['state'],'published');self.assertEqual(result['remote_id'],'123456789')
        self.assertEqual((self.clicks,len(self.page.uploads)),(1,1))
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'published')

    def test_candidate_requires_native_attempt_source_and_independent_same_content(self):
        self.lose_acceptance();marker=engine.marker_path(self.request);original=read_json(marker)
        for field,value in [('native_input_checkpointed_at',None),('native_response_count',0),
                            ('native_response_count',2),('submission_protocol',1)]:
            write_json(marker,dict(original,**{field:value}))
            with self.subTest(field=field),self.assertRaises(PublishError):
                run(self.path,'x',self.profile,self.store,resume=True,recover_remote_id='123456789',recovery_platform='x')
            self.assertIsNone(self.store.status(self.key)['remote_id'])
        write_json(marker,original)
        for platform,candidate in [('wechat','123456789'),('weibo','123456789'),('x','999')]:
            with self.subTest(platform=platform,candidate=candidate),self.assertRaises(PublishError):
                run(self.path,'x',self.profile,self.store,resume=True,recover_remote_id=candidate,recovery_platform=platform)
        changed=copy.deepcopy(original);changed['publication_media']['source_sha256']='0'*64;write_json(marker,changed)
        with self.assertRaises(PublishError):
            run(self.path,'x',self.profile,self.store,resume=True,recover_remote_id='123456789',recovery_platform='x')
        self.assertEqual((self.clicks,len(self.page.uploads)),(1,1))

    def test_wrong_post_media_keeps_actual_post_identity_and_rejects_publication(self):
        self.detail.result['legacy']['extended_entities']['media'][0]['id_str']='999'
        self.detail.result['legacy']['entities']['media'][0]['id_str']='999'
        with self.assertRaisesRegex(PublishError,'已发布视频身份'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['remote_id'],'123456789')
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.clicks,1);self.assertEqual(len(self.page.uploads),1)

    def test_native_player_must_be_visible_unique_and_have_no_extra_picture(self):
        self.detail.player_visible=False
        with self.assertRaisesRegex(PublishError,'可见播放器'):run(self.path,'x',self.profile,self.store)
        self.detail.player_visible=True;self.detail.players=2
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.detail.players=1;self.detail.photos=1
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.detail.photos=0
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.clicks,1)

    def test_native_video_photo_wrapper_is_allowed_but_additional_photo_is_rejected(self):
        self.detail.video_wrappers=1
        self.assertEqual(run(self.path,'x',self.profile,self.store)['state'],'published')
        self.detail.photos=1
        with self.assertRaisesRegex(PublishError,'额外图片'):
            run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual((self.clicks,len(self.page.uploads)),(1,1))

    def test_incomplete_cloud_transcript_stops_before_final_send(self):
        with patch.object(upload.x_video,'cloud_receipt',side_effect=PublishError('incomplete upload receipt')):
            with self.assertRaisesRegex(PublishError,'upload receipt'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.clicks,0);self.assertEqual(len(self.page.uploads),1)
        result=run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(result['state'],'prepared')
        self.assertEqual(self.clicks,0)
        self.assertEqual(len(self.page.uploads),1)

    def test_tampered_source_binding_rejected_during_readonly_resume(self):
        run(self.path,'x',self.profile,self.store)
        saved=read_json(engine.marker_path(self.request));saved['publication_media']['source_sha256']='0'*64
        write_json(engine.marker_path(self.request),saved)
        with self.assertRaisesRegex(PublishError,'封存原片'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.clicks,1)

    def test_changed_preview_after_initial_ready_stops_before_native_send(self):
        original=publisher.video_evidence;calls=[]
        def observe(page,pack,saved):
            value=original(page,pack,saved);calls.append(value)
            if len(calls)==2:page.video['sha256']='0'*64
            return value
        with patch.object(publisher,'video_evidence',side_effect=observe):
            with self.assertRaisesRegex(PublishError,'实际字节'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.clicks,0);self.assertEqual(len(self.page.uploads),1)

    def test_default_prefill_capability_does_not_enable_video_send(self):
        self.profile['adapters']['x']['enabled_capabilities']=['video:prefill']
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.clicks,0)

    def test_last_input_guard_rechecks_video_before_click_without_reupload(self):
        click=publisher.click_background.side_effect
        def changed(page,button,*,before_input):
            page.video['sha256']='0'*64
            return click(page,button,before_input=before_input)
        with patch.object(publisher,'click_background',side_effect=changed):
            with self.assertRaisesRegex(PublishError,'原生点击前'):
                run(self.path,'x',self.profile,self.store)
        saved=read_json(engine.marker_path(self.request))
        self.assertEqual(saved['click_error_phase'],'before_native_input')
        self.assertNotIn('native_input_checkpointed_at',saved)
        self.assertEqual((self.clicks,len(self.page.uploads)),(0,1))
        with self.assertRaisesRegex(PublishError,'实际字节'):
            run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual((self.clicks,len(self.page.uploads)),(0,1))


class VideoFactsTests(unittest.TestCase):
    def setUp(self):
        self.pack=dict(content_type='video',fields=dict(composed_text='😀 正文 https://example.org/read'),
                       assets=[dict(id='clip',sha256='1'*64)])
        self.result=video_tweet(self.pack['fields']['composed_text'])
        self.expected=dict(source_asset_id='clip',source_sha256='1'*64,native=dict(
            media_id='12345',media_key='7_12345',type='video',width=720,height=1280,
            duration_millis=4000,availability='Available'))

    def test_only_proven_native_attachment_suffix_is_removed(self):
        value=x_post.facts(self.result)
        self.assertEqual(value['text'],self.pack['fields']['composed_text'])
        self.assertEqual(x_post.verify_facts(value,self.pack,'@demo',expected_media=self.expected),value)
        for changes in (dict(indices=[0,17]),dict(id_str='999'),dict(media_key='other'),dict(url='https://example.org/read')):
            result=copy.deepcopy(self.result);result['legacy']['entities']['media'][0].update(changes)
            with self.assertRaises(PublishError):x_post.facts(result)

    def test_extended_note_does_not_use_legacy_media_entity_offsets(self):
        self.result['note_tweet']=dict(note_tweet_results=dict(result=dict(
            text=self.pack['fields']['composed_text'],entity_set=dict(urls=[]))))
        self.assertEqual(x_post.facts(self.result)['text'],self.pack['fields']['composed_text'])

    def test_identity_type_dimensions_duration_and_availability_are_required(self):
        original=x_post.facts(self.result)
        for field,value in [('media_id','9'),('media_key','other'),('type','photo'),('width',0),
                            ('height',720),('duration_millis',3998),('availability','Unavailable')]:
            changed=copy.deepcopy(original);changed['media'][0][field]=value
            with self.subTest(field=field),self.assertRaises(PublishError):
                x_post.verify_facts(changed,self.pack,'@demo',expected_media=self.expected)
        for expected in (None,{},dict(self.expected,source_asset_id='other'),dict(self.expected,source_sha256='0'*64)):
            with self.assertRaises(PublishError):x_post.verify_facts(original,self.pack,'@demo',expected_media=expected)

    def test_fractional_millisecond_rounding_only(self):
        for duration in (3999,4000,4001):
            value=x_post.facts(self.result);value['media'][0]['duration_millis']=duration
            self.assertEqual(x_post.verify_facts(value,self.pack,'@demo',expected_media=self.expected),value)
        for duration in (3998,4002,0,True,4000.0,None):
            value=x_post.facts(self.result);value['media'][0]['duration_millis']=duration
            with self.subTest(duration=duration),self.assertRaises(PublishError):
                x_post.verify_facts(value,self.pack,'@demo',expected_media=self.expected)


if __name__=='__main__':unittest.main()
