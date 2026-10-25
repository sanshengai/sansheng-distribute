"""Exercise core -> real photo receipt parser -> one submit -> independent detail.

Native browser/network I/O is synthetic; these tests are not live acceptance.
"""
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import test_x_images as images
import test_x_publish as posts
import browser_engine as engine
import x_image_upload as cloud
import x_images
import x_post
import x_publish_browser as publisher
from platform_pack import build_pack
from publish_core import PublishError, read_json, run, validate, write_json


class UploadResponse:
    def __init__(self, command, params, body=None, data=None, status=200):
        self.url = 'https://upload.x.com/i/media/upload.json?' + urlencode(dict(command=command, **params))
        self.status = status; self.raw = json.dumps(body).encode()
        class Request:
            method = 'GET' if command == 'STATUS' else 'POST'
            post_data_buffer = None if data is None else (b'--native\r\nContent-Disposition: form-data; '
                b'name="media"; filename="blob"\r\nContent-Type: application/octet-stream\r\n\r\n'
                + data + b'\r\n--native--\r\n')
            def header_value(self, name): return 'multipart/form-data; boundary=native'
        self.request = Request()
    def body(self): return self.raw


def upload_responses(data, ident, *, multi=True):
    body = dict(media_id_string=ident, media_key='3_' + ident, expires_after_secs=3600)
    init = UploadResponse('INIT', dict(total_bytes=str(len(data)), media_type='image/png',
                                      media_category='tweet_image'), body)
    if multi:
        size = (len(data) + 1) // 2
        append = UploadResponse('APPENDMULTI', dict(media_id=ident, segment_indexes='0,1',
            max_segment_size=str(size), media_md5=hashlib.md5(data).hexdigest()), data=data, status=204)
        appendices = [append]
    else:
        size = len(data) // 2
        appendices = [UploadResponse('APPEND', dict(media_id=ident, segment_index=str(n)),
            data=part, status=204) for n, part in enumerate((data[:size], data[size:]))]
    final = UploadResponse('FINALIZE', dict(media_id=ident),
        dict(body, size=len(data), image=dict(w=1, h=1, image_type='image/png')))
    return [init, *appendices, final]


def photo_tweet(text, count=2):
    value = posts.tweet(text + ' https://t.co/pics')
    start = len(text.encode('utf-16-le')) // 2 + 1
    media = [dict(id_str=str(100+i), media_key='3_'+str(100+i), type='photo',
        url='https://t.co/pics', indices=[start, start+17], original_info=dict(width=1,height=1),
        media_url_https='https://pbs.twimg.com/media/source'+str(i)+'.png') for i in range(count)]
    value['legacy']['entities']['media'] = copy.deepcopy(media[:1])
    value['legacy']['extended_entities'] = dict(media=media)
    return value


class ImageNode(images.Node):
    def locator(self, selector):
        node=super().locator(selector)
        return ImageNode(self.page,node.kind)
    def set_input_files(self, paths, **kwargs):
        super().set_input_files(paths, **kwargs)
        # Native uploader can complete files in a different order than selection.
        for i in reversed(range(len(self.page.uploads[-1]))):
            for response in upload_responses(self.page.uploads[-1][i],str(100+i),multi=i%2==0):
                if self.page.drop_finalize and 'FINALIZE' in response.url: continue
                self.page.listeners['response'](response)


class Composer(images.Page):
    def __init__(self):
        super().__init__(); self.listeners={}; self.clicks=0; self.drop_finalize=False
    def locator(self, selector):
        if 'tweetButton' in selector: return posts.Button()
        node=super().locator(selector)
        result=ImageNode(self,node.kind); result.wait_for=lambda **kw:None
        return result
    def on(self, event, callback): self.listeners[event]=callback
    def remove_listener(self, event, callback): self.listeners.pop(event)
    def goto(self, url, **kwargs): self.url=url
    def wait_for_timeout(self, ms): pass


class Photo:
    def __init__(self, page, index): self.page=page; self.index=index
    def is_visible(self): return self.index != self.page.hidden_photo
    def locator(self, selector):
        photo=self
        class Img:
            def evaluate_all(self, script):
                index=photo.page.photo_order[photo.index]
                return [dict(src='https://pbs.twimg.com/media/source'+str(index)+'?format=png&name=small',
                             loaded=photo.page.photos_loaded)]
        return Img()


class Article(posts.Articles):
    def locator(self, selector):
        page=self.page
        if selector=='[data-testid="tweetPhoto"]':
            class Photos:
                def count(self): return len(page.photo_order)
                def all(self): return [Photo(page,i) for i in range(len(page.photo_order))]
            return Photos()
        if selector=='video,[data-testid="videoPlayer"]':
            return type('Videos',(),{'count':lambda _:page.extra_video})()
        return super().locator(selector)


class Detail(posts.DetailPage):
    def __init__(self,result):
        super().__init__(result); self.photo_order=[0,1]; self.hidden_photo=None
        self.photos_loaded=True; self.extra_video=0
    def locator(self, selector): return Article(self)


class XImagePublicationTests(unittest.TestCase):
    request = images.XImagesTests.request
    setUp_base = images.XImagesTests.setUp_base

    def setUp(self):
        images.XImagesTests.setUp(self)
        self.page=Composer(); self.mode='ok'; self.click_receipts=[]
        self.profile['adapters']['x']['enabled_capabilities']=['images:publish']
        write_json(self.profile_path,self.profile)
        self.manifest['targets'][0]['endpoint']='publish'
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'x')['key']
        self.store.authorize(self.manifest,'x','offline fixture only; no live upload or send')
        self.pack=build_pack(self.manifest,'x',self.profile)
        self.detail=Detail(photo_tweet(self.pack['fields']['composed_text']))
        self.detail.rendered_text=self.pack['fields']['composed_text']
        @contextlib.contextmanager
        def window(*args,**kw): yield self.page
        @contextlib.contextmanager
        def tab(*args,**kw): yield self.detail
        patch.object(engine,'select_page',return_value=self.page).start()
        patch.object(publisher,'background_window',side_effect=window).start()
        patch.object(x_post,'background_tab',side_effect=tab).start()
        def click(page,button,*,before_input):
            before_input()
            receipt=read_json(engine.marker_path(self.request))
            self.click_receipts.append(dict(durable=bool(receipt.get('submission_attempted_at')),
                native_input=bool(receipt.get('native_input_checkpointed_at')),
                media_count=len(receipt.get('publication_media',[]))))
            page.clicks+=1
            if self.mode=='no_receipt': raise RuntimeError('uncertain native click')
            result=photo_tweet(page.text)
            page.listeners['response'](posts.Response(dict(data=dict(create_tweet=dict(tweet_results=dict(result=result))))))
            self.assertEqual(read_json(engine.marker_path(self.request))['accepted']['remote_id'],'123456789')
            if self.mode=='lost_reply': raise RuntimeError('click reply lost')
        patch.object(publisher,'click_background',side_effect=click).start()

    def test_core_ordered_source_receipts_one_send_same_id_resume_without_files(self):
        result=run(self.path,'x',self.profile,self.store)
        self.assertEqual(result['state'],'published')
        saved=read_json(engine.marker_path(self.request))
        self.assertEqual([v['native']['media_id'] for v in saved['publication_media']],['100','101'])
        self.assertEqual(self.click_receipts,[dict(durable=True,native_input=True,media_count=2)])
        for a in self.manifest['assets']:(self.root/a['path']).unlink()
        for p in self.request.with_suffix('.browser-assets').iterdir():p.unlink()
        self.page.images=[];self.page.text='another unrelated composer'
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.clicks,1)

    def test_missing_cloud_finalize_keeps_existing_selection_without_click_or_reupload(self):
        self.page.drop_finalize=True
        with patch.object(cloud.time,'monotonic',side_effect=[0,61]):
            with self.assertRaisesRegex(PublishError,'尚未完成'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(len(self.page.uploads),1)

    def test_changed_bytes_at_last_input_stops_send(self):
        original=publisher.image_evidence;calls=[]
        def change(*args):
            value=original(*args);calls.append(1)
            if len(calls)==3:self.page.images[1]['sha256']='0'*64
            return value
        with patch.object(publisher,'image_evidence',side_effect=change):
            with self.assertRaisesRegex(PublishError,'原生点击前'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.clicks,0);self.assertEqual(len(self.page.uploads),1)

    def test_unknown_click_never_repeated(self):
        self.mode='no_receipt'
        with patch.object(publisher.time,'monotonic',side_effect=[0,0,21]):
            with self.assertRaisesRegex(PublishError,'结果未知'):run(self.path,'x',self.profile,self.store)
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1);self.assertEqual(len(self.page.uploads),1)
        self.assertEqual(self.store.status(self.key)['state'],'submission_unknown')

    def test_lost_click_reply_with_durable_id_does_not_repeat(self):
        self.mode='lost_reply'
        self.assertEqual(run(self.path,'x',self.profile,self.store)['state'],'published')
        self.assertEqual(self.page.clicks,1)

    def test_same_caption_other_cloud_image_or_order_does_not_verify(self):
        original=copy.deepcopy(self.detail.result)
        for variant in ('id','order','dimensions'):
            self.detail.result=copy.deepcopy(original);media=self.detail.result['legacy']['extended_entities']['media']
            if variant=='id':
                media[1]['id_str']='999';media[1]['media_key']='3_999'
            elif variant=='order':media.reverse();self.detail.result['legacy']['entities']['media']=copy.deepcopy(media[:1])
            else:media[1]['original_info']['width']=2
            with self.assertRaisesRegex(PublishError,'已发布图片'):run(self.path,'x',self.profile,self.store,resume=variant!='id')
        self.detail.result=original
        self.assertEqual(run(self.path,'x',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.page.clicks,1)

    def test_visible_other_image_order_hidden_unloaded_extra_video_rejected(self):
        for mode in ('order','hidden','unloaded','video'):
            self.detail.photo_order=[1,0] if mode=='order' else [0,1]
            self.detail.hidden_photo=1 if mode=='hidden' else None
            self.detail.photos_loaded=mode!='unloaded';self.detail.extra_video=int(mode=='video')
            with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store,resume=mode!='order')
        self.assertEqual(self.page.clicks,1)

    def test_changed_source_binding_rejected_on_resume(self):
        run(self.path,'x',self.profile,self.store)
        saved=read_json(engine.marker_path(self.request));saved['publication_media'][1]['source_sha256']='0'*64
        write_json(engine.marker_path(self.request),saved)
        with self.assertRaisesRegex(PublishError,'封存原图'):run(self.path,'x',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_only_prefill_permission_does_not_enable_send(self):
        self.profile['adapters']['x']['enabled_capabilities']=['images:prefill']
        with self.assertRaises(PublishError):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.clicks,0)

    def test_four_photos_one_native_selection_and_exact_order(self):
        for i in (2,3):
            (self.root/f'{i}.png').write_bytes(images.png(i))
            self.manifest['assets'].append(dict(id='img'+str(i),path=f'{i}.png',kind='image'))
            self.manifest['targets'][0]['asset_ids'].append('img'+str(i))
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.key=self.store.prepare(self.manifest,'x')['key']
        self.store.authorize(self.manifest,'x','offline four-image fixture')
        self.detail.result=photo_tweet(self.pack['fields']['composed_text'],4)
        self.detail.photo_order=[0,1,2,3]
        # The native creation response only establishes identity; detail proves all four.
        self.assertEqual(run(self.path,'x',self.profile,self.store)['state'],'published')
        self.assertEqual(self.click_receipts[0]['media_count'],4)
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.page.clicks,1)

    def test_duplicate_source_bytes_rejected_before_connection_or_upload(self):
        pack=copy.deepcopy(self.pack);pack['assets'][1]['sha256']=pack['assets'][0]['sha256']
        with self.assertRaisesRegex(PublishError,'重复原图'):engine.check_pack(pack)
        self.assertEqual(self.page.uploads,[]);self.assertEqual(self.page.clicks,0)

    def test_durable_input_checkpoint_failure_never_clicks(self):
        real_write=publisher.write_json
        def write(path,value):
            if value.get('native_input_checkpointed_at'):
                raise OSError('native input checkpoint not durable')
            return real_write(path,value)
        with patch.object(publisher,'write_json',side_effect=write):
            with self.assertRaisesRegex(PublishError,'原生点击前'):run(self.path,'x',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)
        self.assertNotIn('native_input_checkpointed_at',read_json(engine.marker_path(self.request)))

    def test_repeat_execute_never_uploads_again(self):
        run(self.path,'x',self.profile,self.store)
        import fcntl
        from publish_core import digest
        lock_path=self.store.directory/'locks'/(digest(dict(platform='x',account='editor'))+'.lock')
        with lock_path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(PublishError):engine.dispatch(self.request,'execute',self.profile,lock.fileno())
        self.assertEqual(len(self.page.uploads),1)


class PhotoReceiptTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        data=images.png(1);p=self.root/'photo.png';p.write_bytes(data)
        self.asset=dict(id='photo',path='photo.png',absolute_path=str(p),sha256=hashlib.sha256(data).hexdigest())
        self.preview=dict(source_asset_id='photo',source_sha256=self.asset['sha256'],width=1,height=1)
        capture=cloud.Capture(lambda *args:None)
        for response in upload_responses(data,'100'):capture.response(response)
        self.assertEqual(capture.errors,[]);self.events=capture.events

    def receipt(self,events):return cloud.receipts(events,[self.asset],[self.preview])

    def test_actual_multipart_hashes_bound_to_source_no_raw_bytes_or_headers_retained(self):
        self.assertEqual(self.receipt(self.events)[0]['native']['media_id'],'100')
        text=json.dumps(self.events)
        self.assertNotIn('filename',text);self.assertNotIn('Content-',text)
        self.assertNotIn('post_data',text)
        capture=cloud.Capture(lambda *args:None);response=upload_responses(images.png(1),'100')[-1]
        body=json.loads(response.raw);body['image']['debug_token']='must-not-persist'
        response.raw=json.dumps(body).encode();capture.response(response)
        self.assertNotIn('must-not-persist',json.dumps(capture.events))

    def test_sdk_finalize_may_omit_optional_image_metadata_not_request_identity(self):
        events=copy.deepcopy(self.events);events[-1]['body']={}
        self.assertEqual(self.receipt(events)[0]['native']['media_id'],'100')
        events[-1]['media_id']='999'
        with self.assertRaises(PublishError):self.receipt(events)

    def test_empty_missing_duplicate_reordered_mismatched_or_expired_receipts_rejected(self):
        variants=[[],self.events[:2],list(reversed(self.events)),self.events+self.events]
        for field,value in [('size',1),('media_id_string','999'),('media_key','3_999'),('expires_after_secs',-1)]:
            e=copy.deepcopy(self.events);e[-1]['body'][field]=value;variants.append(e)
        e=copy.deepcopy(self.events);e[1]['chunks'][0]['sha256']='0'*64;variants.append(e)
        e=copy.deepcopy(self.events);e[1]['chunks'][0]['index']=1;variants.append(e)
        e=copy.deepcopy(self.events);e[-1]['body']['image']['w']=2;variants.append(e)
        e=copy.deepcopy(self.events);e[-1]['body']['processing_info']={'state':'pending'};variants.append(e)
        for value in variants:
            with self.subTest(value=value),self.assertRaises(PublishError):self.receipt(value)
        with self.assertRaises(PublishError):cloud.receipts(self.events,[self.asset],[self.preview],now=time.time()+7200)

    def test_bad_method_body_duplicate_keys_ambiguous_parameter_and_extra_multipart_rejected(self):
        for mode in ('method','status','json','query','size','multipart','md5'):
            response=upload_responses(images.png(1),'100')[1 if mode in {'multipart','md5'} else 0]
            if mode=='method':response.request.method='GET'
            if mode=='status':response.status=True
            if mode=='json':response.raw=b'{"media_id_string":"100","media_id_string":"101"}'
            if mode=='query':response.url+='&command=INIT'
            if mode=='size':response.raw=b'x'*65537
            if mode=='multipart':response.request.post_data_buffer=b'broken'
            if mode=='md5':response.url=response.url.replace('media_md5=','media_md5=invalid')
            capture=cloud.Capture(lambda *args:None);capture.response(response)
            self.assertTrue(capture.errors,mode);self.assertEqual(capture.events,[])

    def test_wrong_source_same_length_native_chunks_rejected(self):
        data=images.png(2)
        capture=cloud.Capture(lambda *args:None)
        for r in upload_responses(data,'100'):capture.response(r)
        with self.assertRaisesRegex(PublishError,'封存字节'):self.receipt(capture.events)

    def test_write_failure_is_sticky_and_acceptance_never_precedes_durable_save(self):
        capture=cloud.Capture(lambda *args:(_ for _ in ()).throw(OSError('full disk')))
        capture.response(upload_responses(images.png(1),'100')[0]);self.assertTrue(capture.errors)
        result=posts.tweet();response=posts.Response(dict(data=dict(create_tweet=dict(tweet_results=dict(result=result)))))
        cap=x_post.Capture({},'@demo',lambda value:(_ for _ in ()).throw(OSError('full disk')))
        cap.response(response);self.assertIsNone(cap.accepted);self.assertIsNotNone(cap.error)
        saved={'state':'before'}
        with patch.object(publisher,'write_json',side_effect=OSError('full disk')):
            with self.assertRaises(OSError):publisher.checkpoint(saved,self.root/'marker.json',state='accepted')
        self.assertEqual(saved,{'state':'before'})

    def test_wait_yields_to_native_processing_without_resubmitting(self):
        capture=cloud.Capture(lambda *args:None)
        responses=upload_responses(images.png(1),'100')
        for r in responses[:2]:capture.response(r)
        class Pending:
            waits=0
            def wait_for_timeout(self,ms):
                self.waits+=1;capture.response(responses[-1])
        page=Pending();cloud.wait_complete(page,capture,1)
        self.assertEqual(page.waits,1);self.assertEqual(self.receipt(capture.events)[0]['native']['media_id'],'100')

    def test_cdn_identity_and_shared_native_media_entity_are_strict(self):
        self.assertEqual(x_post.photo_source('https://pbs.twimg.com/media/source.png'),
                         x_post.photo_source('https://pbs.twimg.com/media/source?format=png&name=small'))
        for url in ('https://evil.test/media/source.png','https://pbs.twimg.com.evil/media/source.png',
                    'https://pbs.twimg.com/media/source?format=png&format=jpg','https://pbs.twimg.com/media/source.png?format=jpg'):
            with self.assertRaises(PublishError):x_post.photo_source(url)
        for n in (1,2,4):
            value=photo_tweet('😀 完整文案',n)
            self.assertEqual(x_post.facts(value)['media_count'],n)
            value['legacy']['entities']['media']=copy.deepcopy(value['legacy']['extended_entities']['media'])
            self.assertEqual(x_post.facts(value)['text'],'😀 完整文案')
            value['legacy']['entities']['media'][-1]['indices'][0]+=1
            with self.assertRaises(PublishError):x_post.facts(value)


if __name__=='__main__': unittest.main()
