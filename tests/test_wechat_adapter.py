import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import PublishError,digest,validate,write_json,file_hash
from wechat_adapter import dispatch
from wechat_adapter import article_equivalent
from wechat_media import Components, component_info
from wechat_links import project_links


class FakeApi:
    def __init__(self): self.articles={}; self.creates=0
    def _authorized_call(self,app,secret,callback): return callback('fixture-token')
    def upload_html_images(self,body,base,token): return body,[]
    def upload_cover(self,cover,token): return {'media_id':'cover-fixture'}
    def add_draft(self,token,article):
        self.creates+=1; self.articles['draft-fixture']=article.copy(); return 'draft-fixture'
    def get_draft(self,**kwargs): return {'news_item':[self.articles[kwargs['media_id']]]}


class WechatTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'article.html').write_text('<h1>标题</h1><p>正文</p>')
        (self.root/'cover.png').write_bytes(b'fixture-cover')
        env=self.root/'credentials.env'; env.write_text('WECHAT_APP_ID=fixture-app\nWECHAT_APP_SECRET=fixture-secret\n')
        self.profile={'accounts':{'main':{'wechat_env_file':str(env),'wechat_appid_sha256':hashlib.sha256(b'fixture-app').hexdigest()}}}
        self.manifest=dict(schema_version=1,job_id='wx',producer='test',content_status='final',source_root=str(self.root),
                           documents=[dict(id='article',path='article.html',format='html')],
                           assets=[dict(id='cover',kind='image',path='cover.png')],media_slots=[],
                           targets=[dict(id='wx',platform='wechat',account='main',content_type='article',endpoint='draft',document_id='article',asset_ids=['cover'],cover_asset_id='cover',title='标题')])
        self.api=FakeApi(); self.request=self.root/'request.json'
        self.save_request()

    def save_request(self):
        validate(self.manifest,sealing=True)
        path=self.root/'manifest.json'; write_json(path,self.manifest)
        write_json(self.request,dict(manifest_path=str(path),manifest_digest=digest(self.manifest),target=self.manifest['targets'][0],job={}))

    def tearDown(self): self.temp.cleanup()

    def visual_receipt(self,receipt=None):
        receipt=receipt or dict(manifest_digest=digest(self.manifest),remote_id='draft-fixture',components=[])
        evidence=self.root/'visual.txt'; evidence.write_text('Offline fixture, not a real visual review.')
        receipt['layout_review']=dict(reviewed=True,content_sha256=hashlib.sha256(self.api.articles['draft-fixture']['content'].encode()).hexdigest(),
                                      evidence=[dict(path=str(evidence),sha256=file_hash(evidence))])
        write_json(self.request.with_suffix('.native-media.json'),receipt)
        return receipt

    def test_draft_requires_independent_readback(self):
        self.assertEqual(dispatch(self.request,self.profile,self.api,action='execute')['state'],'pending')
        self.assertEqual(dispatch(self.request,self.profile,self.api,action='verify')['stage'],'visual_review_required')
        self.visual_receipt()
        self.assertEqual(dispatch(self.request,self.profile,self.api,action='verify')['state'],'draft_ready')
        with self.assertRaisesRegex(PublishError,'只允许读回'): dispatch(self.request,self.profile,self.api,action='execute')
        self.assertEqual(self.api.creates,1)

    def test_media_draft_not_reported_complete(self):
        (self.root/'article.html').write_text('<p>前【发布媒体:theme】后</p>')
        (self.root/'audio.mp3').write_bytes(b'fixture-audio')
        self.manifest['assets'].append(dict(id='theme',kind='audio',path='audio.mp3'))
        self.manifest['media_slots']=[dict(id='theme',asset_id='theme',document_id='article')]
        self.save_request()
        dispatch(self.request,self.profile,self.api,action='execute')
        result=dispatch(self.request,self.profile,self.api,action='verify')
        self.assertEqual(result['state'],'pending'); self.assertFalse(result['content_verified'])
        self.assertEqual(result['media_slot_ids'],['theme'])

    def test_account_drift_and_readback_body_drift_refused(self):
        self.profile['accounts']['main']['wechat_appid_sha256']='wrong'
        with self.assertRaisesRegex(PublishError,'账号绑定'): dispatch(self.request,self.profile,self.api,action='execute')
        self.assertEqual(self.api.creates,0)
        self.profile['accounts']['main']['wechat_appid_sha256']=hashlib.sha256(b'fixture-app').hexdigest()
        dispatch(self.request,self.profile,self.api,action='execute')
        self.api.articles['draft-fixture']['content']='<p>被改变</p>'
        with self.assertRaisesRegex(PublishError,'差异'): dispatch(self.request,self.profile,self.api,action='verify')

    def test_unknown_id_never_recreates(self):
        with self.assertRaisesRegex(PublishError,'禁止再次建稿'): dispatch(self.request,self.profile,self.api,action='verify')
        self.assertEqual(self.api.creates,0)

    def test_explicit_link_projection_and_historical_repair(self):
        original='<p>前</p><p><a href="https://example.org/a">看板</a></p><p>后</p>'
        (self.root/'article.html').write_text(original)
        self.save_request()
        dispatch(self.request,self.profile,self.api,action='execute')
        projected,stripped,changes=project_links(original)
        self.api.articles['draft-fixture']['content']=stripped
        with self.assertRaisesRegex(PublishError,'差异'):
            dispatch(self.request,self.profile,self.api,action='verify')
        self.profile['accounts']['main']['external_link_policy']='visible_url'
        result=dispatch(self.request,self.profile,self.api,action='verify')
        self.assertEqual(result['stage'],'external_link_repair_required')
        self.assertFalse(result['content_verified'])
        self.assertEqual(result['link_work'],changes)
        for bad in ('',stripped.replace('前','改'),projected.replace('example.org','attacker.example')):
            self.api.articles['draft-fixture']['content']=bad
            with self.assertRaisesRegex(PublishError,'差异'):
                dispatch(self.request,self.profile,self.api,action='verify')
        self.api.articles['draft-fixture']['content']=projected
        self.assertEqual(dispatch(self.request,self.profile,self.api,action='verify')['stage'],'visual_review_required')
        self.visual_receipt()
        self.assertEqual(dispatch(self.request,self.profile,self.api,action='verify')['state'],'draft_ready')
        self.assertEqual(self.api.creates,1)

    def test_projection_before_upload_preserves_urls(self):
        content='<p><a href="https://example.org/?x=1&amp;y=2"><strong>看板</strong></a></p>'
        (self.root/'article.html').write_text(content)
        self.save_request()
        self.profile['accounts']['main']['external_link_policy']='visible_url'
        dispatch(self.request,self.profile,self.api,action='execute')
        result=self.api.articles['draft-fixture']['content']
        self.assertIn('<strong>看板</strong>（https://example.org/?x=1&amp;y=2）',result)
        self.assertEqual(project_links(''),('','',[]))
        internal='<a href="https://' + 'mp.weixin.qq.com' + '/s/' + 'valid">微信文章</a>'
        self.assertEqual(project_links(internal),(internal,internal,[]))
        self.assertEqual(dispatch(self.request,self.profile,self.api,action='verify')['stage'],'visual_review_required')

    def test_observed_wechat_video_iframe_is_narrowly_recognized(self):
        # Structure captured from real draft/get; identifiers replaced with fixtures.
        sample='<iframe data-src="https://mp.weixin.qq.com/mp/readtemplate?t=pages/video_player_tmpl&amp;action=mpvideo&amp;auto=0&amp;vid=wxv_12345" data-mpvid="wxv_12345" data-vidtype="2" data-ratio="0.5625" data-w="1080" class="video_iframe rich_pages"></iframe>'
        self.assertEqual(component_info(sample)[0]['identities'],{'data-mpvid':'wxv_12345'})
        for bad in (sample.replace('https://mp.weixin.qq.com/','https://evil.example/'),
                    sample.replace('data-mpvid="wxv_12345"','data-mpvid="wxv_99999"'),
                    sample.replace('pages/video_player_tmpl','pages/other'),
                    sample.replace('action=mpvideo','action=other'),
                    sample.replace('data-vidtype="2"','data-vidtype="1"'),
                    sample.replace('data-src=', 'src='), '<iframe></iframe>'):
            with self.subTest(bad=bad), self.assertRaises(PublishError):
                component_info(bad)
        self.assertEqual(component_info(''),[])

    def test_media_roundtrip_uses_position_and_source_bound_observation(self):
        # Synthetic native component: tests comparison only, never an API construction recipe.
        component='<mp-common-mpaudio voice_encode_fileid="fixture-only"></mp-common-mpaudio>'
        (self.root/'article.html').write_text('<p>前【发布媒体:theme】后</p>')
        (self.root/'audio.mp3').write_bytes(b'fixture-audio')
        self.manifest['assets'].append(dict(id='theme',kind='audio',path='audio.mp3'))
        self.manifest['media_slots']=[dict(id='theme',asset_id='theme',document_id='article')]
        self.save_request(); dispatch(self.request,self.profile,self.api,action='execute')
        receipt=dict(manifest_digest=digest(self.manifest),remote_id='draft-fixture',components=[dict(slot_id='theme',kind='audio',
                     asset_sha256=self.manifest['assets'][-1]['sha256'],remote_media_id='fixture-only',remote_id_attribute='voice_encode_fileid',upload_evidence='offline-fixture',
                     component_sha256=hashlib.sha256(component.encode()).hexdigest(),saved=True,reopened=True,loaded=True,played=True)])
        write_json(self.request.with_suffix('.native-media.json'),receipt)
        self.api.articles['draft-fixture']['content']='<p>前'+component+'后</p>'
        self.visual_receipt(receipt)
        self.assertEqual(dispatch(self.request,self.profile,self.api,action='verify')['state'],'draft_ready')
        self.api.articles['draft-fixture']['content']='<p>前后'+component+'</p>'
        with self.assertRaisesRegex(PublishError,'位置'): dispatch(self.request,self.profile,self.api,action='verify')
        self.api.articles['draft-fixture']['content']='<p>前'+component+'后</p>'
        inspected=dispatch(self.request,self.profile,self.api,action='inspect-media')
        self.assertEqual(inspected['components'][0]['identities']['voice_encode_fileid'],'fixture-only')
        receipt['components'][0]['played']=False; write_json(self.request.with_suffix('.native-media.json'),receipt)
        waiting=dispatch(self.request,self.profile,self.api,action='verify')
        self.assertEqual(waiting['media_work'],[dict(slot_id='theme',action='verify_existing_player')])
        receipt['components'][0]['played']=True
        for key,value in [('asset_sha256','changed'),('component_sha256','wrong'),('remote_media_id','other')]:
            original=receipt['components'][0][key]; receipt['components'][0][key]=value
            write_json(self.request.with_suffix('.native-media.json'),receipt)
            with self.assertRaises(PublishError): dispatch(self.request,self.profile,self.api,action='verify')
            receipt['components'][0][key]=original
        receipt['components']=[]; write_json(self.request.with_suffix('.native-media.json'),receipt)
        with self.assertRaisesRegex(PublishError,'数量'): dispatch(self.request,self.profile,self.api,action='verify')

    def test_partial_media_can_resume_without_reinserting_completed_slot(self):
        from wechat_media import reconcile
        a='<mpaudio voice_encode_fileid="fixture-a"></mpaudio>'
        slots=[dict(id='a',asset_id='a'),dict(id='b',asset_id='b')]
        assets={k:dict(kind='audio',sha256=k) for k in ('a','b')}
        receipt=dict(manifest_digest='fixture',remote_id='draft-fixture',components=[dict(slot_id='a',kind='audio',asset_sha256='a',
                     remote_media_id='fixture-a',remote_id_attribute='voice_encode_fileid',upload_evidence='fixture',
                     component_sha256=hashlib.sha256(a.encode()).hexdigest(),saved=True,reopened=True,loaded=True,played=True)])
        restored,remaining=reconcile('前'+a+'中【发布媒体:b】后',slots,assets,receipt,manifest_digest='fixture',remote_id='draft-fixture')
        self.assertEqual(restored,'前【发布媒体:a】中【发布媒体:b】后')
        self.assertEqual(remaining,[dict(slot_id='b',action='insert')])
        video='<mpvideo vid="fixture-v"></mpvideo>'
        receipt['components'][0].update(component_sha256=hashlib.sha256(video.encode()).hexdigest(),remote_media_id='fixture-v',remote_id_attribute='vid')
        with self.assertRaisesRegex(PublishError,'类型'):
            reconcile('前'+video+'中【发布媒体:b】后',slots,assets,receipt,manifest_digest='fixture',remote_id='draft-fixture')

    def test_visual_review_is_bound_to_actual_html(self):
        dispatch(self.request,self.profile,self.api,action='execute'); self.visual_receipt()
        self.api.articles['draft-fixture']['content']='<span style="display:none">标题正文</span>'
        with self.assertRaisesRegex(PublishError,'视觉检查'): dispatch(self.request,self.profile,self.api,action='verify')

    def test_image_position_and_unknown_player_refused(self):
        self.assertFalse(article_equivalent({'content':'前<img src="image">后'}, {'content':'前后<img src="image">'}))
        for body in ('<iframe src="unknown"></iframe>','<mpaudio>','<mpaudio><mpaudio/></mpaudio>'):
            with self.assertRaises(PublishError): Components(body)


if __name__=='__main__': unittest.main()
