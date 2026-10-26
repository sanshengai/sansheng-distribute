"""Native composer/input contract and unified one-attempt video sender tests."""
import contextlib
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import browser_engine
import weibo_browser as home
import weibo_publish_browser as shared
import weibo_video_browser as prefill
import weibo_video_publish_browser as sender
import weibo_video_submission as native
from publish_core import PublishError,Store,read_json,write_json,run,validate
from test_weibo_video_post import source_fixture,UID,RID,VID,PID,body


def snapshot(pack):
    statements=[dict(name=label,type=i) for label,i in [('无',0),('内容由AI生成',1),('内容为虚构演绎',2),('内容为转载',3),('内容为自主创作',4)]]
    statement=next(x['type'] for x in statements if x['name']==pack['platform_metadata']['declaration'])
    state=dict(mblog_statement=statement,visible=0,schedule_timestamp=0,schedule_id='',toolAction='channel',
        ispost=False,content=pack['fields']['composed_text'],pic_id=[],media={},vote={},position=None,
        authority=0,approval_state=[],selectedDeclarationRequired='',selectedDeclarationOptional=[],
        declarationReprintSource='',statementConfig=None,share_id='',sgid='',shopConfig=None,publishMarkdown=None)
    return dict(statement=statements,composers=[dict(state=state,publish=True,autoPublish=False,action='channel',
        doneDisabled=False,isAiClip=False,publishType='',statementAuth=True)])


def params(pack,statement):
    media=dict(type='video',media_id=VID,titles=[dict(title=pack['fields']['title'],default='true')],
        covers=[dict(pid=PID,source=1,width=1600,height=900)],free_duration=dict(start=0,end=30),
        resource=dict(video_down=1,allow_clip=1),homemade=dict(channel_ids=[''],type=0))
    value=dict(content=pack['fields']['composed_text'],visible='0',share_id='',vote='',media=json.dumps(media))
    if statement:value['mblog_statement']=str(statement)
    return value


class Request:
    url='https://weibo.com/ajax/statuses/update';method='POST'
    headers={'content-type':'application/x-www-form-urlencoded;charset=UTF-8'}
    def __init__(self,values):self.post_data=urlencode(values,doseq=True)


class InputContract(unittest.TestCase):
    def setUp(self):
        self.pack,self.accepted,self.saved,self.observed=source_fixture()
        self.pack['platform_metadata'].update(declaration='内容为自主创作',ai_generated=False)
        self.saved['pack_digest']=shared.digest(self.pack)
        self.snapshot=dict(native.composer_state(snapshot(self.pack),self.pack),cover_pid=PID)

    def test_native_current_composer_and_exact_serialized_source_pass(self):
        result=native.native_input(Request(params(self.pack,4)),self.pack,self.saved,self.snapshot)
        self.assertEqual(result['media_id'],VID);self.assertEqual(result['statement'],4)
        self.assertEqual(result['cover_pid'],PID)

    def test_native_fingerprint_and_exact_empty_portrait_cover_slot(self):
        p=params(self.pack,4);p['fp']='synthetic-native-fingerprint'
        native.native_input(Request(p),self.pack,self.saved,self.snapshot)
        self.saved['native_video'].update(width=1080,height=1920)
        c=self.saved['cover'];c['prepared'].update(width=675,height=900)
        c['crop'].update(width=675,height=900,aspect_ratio=3/4)
        c['crop']['crop'].update(width=675,height=900)
        media=json.loads(p['media']);media['covers'][0].update(width=675,height=900)
        media['covers'].append(dict(type=1,source=1));p['media']=json.dumps(media)
        native.native_input(Request(p),self.pack,self.saved,self.snapshot)
        for second in (dict(type=True,source=1),dict(type=2,source=1),dict(type=1,source=1,pid='Other'),
                       dict(type=1,source=11),{}):
            changed=copy.deepcopy(media);changed['covers'][1]=second
            with self.subTest(second=second),self.assertRaises(PublishError):
                native.native_input(Request(dict(p,media=json.dumps(changed))),self.pack,self.saved,self.snapshot)
        for fp in ('','x'*16385,['one','two']):
            with self.subTest(fp_type=type(fp).__name__),self.assertRaises(PublishError):
                native.native_input(Request(dict(p,fp=fp)),self.pack,self.saved,self.snapshot)

    def test_empty_composer_missing_fields_and_changed_scope_rejected(self):
        for value in ({},dict(composers=[],statement=[])):
            with self.assertRaises(PublishError):native.composer_state(value,self.pack)
        for location,key,value in [('composer','publish',False),('composer','autoPublish',True),('composer','isAiClip',True),
            ('state','visible',False),('state','schedule_timestamp',1),('state','media',dict(type='video')),
            ('state','content','改变'),('state','mblog_statement',1),('state','selectedDeclarationRequired','new')]:
            s=snapshot(self.pack);node=s['composers'][0] if location=='composer' else s['composers'][0]['state'];node[key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):native.composer_state(s,self.pack)
        s=snapshot(self.pack);del s['composers'][0]['state']['position']
        with self.assertRaises(PublishError):native.composer_state(s,self.pack)
        s=snapshot(self.pack);s['statement'].append(dict(name='内容为自主创作',type=4))
        with self.assertRaises(PublishError):native.composer_state(s,self.pack)

    def test_duplicate_or_unknown_fields_and_wrong_text_visibility_declaration_rejected(self):
        for key,value in [('content','改变'),('visible','1'),('vote','poll'),('media',['{}','{}']),
                          ('mblog_statement','1'),('schedule_timestamp','100'),('content',['same','same'])]:
            p=params(self.pack,4);p[key]=value
            with self.subTest(key=key),self.assertRaises(PublishError):native.native_input(Request(p),self.pack,self.saved,self.snapshot)
        p=params(self.pack,4);p['media']='{"type":"video","type":"video"}'
        with self.assertRaises(PublishError):native.native_input(Request(p),self.pack,self.saved,self.snapshot)

    def test_media_identity_cover_creation_and_unmapped_features_rejected(self):
        for key,value in [('media_id','111'),('titles',[dict(title='不同',default='true')]),('covers',[dict(pid='WrongCoverPid',source=1)]),
                          ('homemade',dict(channel_ids=[''],type=True)),('contribution',dict(channel_ids=[''],type=0)),
                          ('resource',dict(video_down=1,allow_clip=True)),('playlist',dict(album_ids='123')),
                          ('free_duration',dict(start=False,end=30))]:
            p=params(self.pack,4);m=json.loads(p['media']);m[key]=value;p['media']=json.dumps(m)
            with self.subTest(key=key),self.assertRaises(PublishError):native.native_input(Request(p),self.pack,self.saved,self.snapshot)

    def test_native_origin_method_content_type_and_missing_source_rejected(self):
        for key,value in [('url','https://weibo.com.evil.org/ajax/statuses/update'),('method','GET'),
                          ('headers',{'content-type':'application/json'}),('post_data','')]:
            r=Request(params(self.pack,4));setattr(r,key,value)
            with self.subTest(key=key),self.assertRaises(PublishError):native.native_input(r,self.pack,self.saved,self.snapshot)
        self.saved['native_video']={}
        with self.assertRaises(PublishError):native.native_input(Request(params(self.pack,4)),self.pack,self.saved,self.snapshot)

    def test_default_switch_shape_rejects_missing_extra_or_changed_author_option(self):
        class Nodes:
            def evaluate_all(self,*args):return self.value
        class Page:
            def locator(self,*args):return nodes
        nodes=Nodes();nodes.value=[dict(checked=False,on='true',off='false')]*2+[dict(checked=True,on='1',off='0')]
        native.default_options(Page())
        for values in ([],nodes.value[:2],nodes.value*2,[dict(checked=True,on='true',off='false')]*3):
            nodes.value=values
            with self.assertRaises(PublishError):native.default_options(Page())


class Button:
    def count(self):return 1
    def is_visible(self):return True
    def is_disabled(self):return False
    def scroll_into_view_if_needed(self,**kwargs):pass


class Page:
    ident='OwnedVideoTarget';uid=UID
    def __init__(self):self.listeners={};self.clicks=0;self.closes=0;self.url='https://weibo.com/'
    def on(self,event,fn):self.listeners[event]=fn
    def remove_listener(self,event,fn):self.listeners.pop(event)
    def get_by_role(self,role,**kw):
        assert role=='button' and kw==dict(name='发布',exact=True)
        return Button()
    def close(self):self.closes+=1
    def wait_for_timeout(self,*args):pass


class SenderCore(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.path=self.root/'manifest.json';self.profile_path=self.root/'profile.json';self.addCleanup(patch.stopall)
        (self.root/'video.mp4').write_bytes(b'\0\0\0\x18ftypmp42');(self.root/'cover.png').write_bytes(b'fixture')
        self.profile=dict(configured_modules=['weibo'],browser_profile=str(self.root/'dedicated'),
            browser_runtime=dict(cdp_url='http://127.0.0.1:9333'),accounts=dict(author=dict(platforms=['weibo'],public_identities=dict(weibo=UID))),
            adapters=dict(weibo=dict(enabled_capabilities=['video:publish'],
                execute=[sys.executable,str(ROOT/'scripts/browser_engine.py'),'execute','--profile',str(self.profile_path)],
                verify=[sys.executable,str(ROOT/'scripts/browser_engine.py'),'verify','--profile',str(self.profile_path)])))
        write_json(self.profile_path,self.profile)
        self.manifest=dict(schema_version=1,job_id='weibo-video-native-sender-fixture',producer='files',content_status='final',
            source_root=str(self.root),documents=[],media_slots=[],assets=[dict(id='video',kind='video',path='video.mp4'),dict(id='cover',kind='image',path='cover.png')],
            targets=[dict(id='weibo',platform='weibo',account='author',endpoint='publish',content_type='video',
            asset_ids=['video','cover'],cover_asset_id='cover',title='知识图解',text='第一段😀\n\n成套图解',
            platform_metadata=dict(creation_type='原创',declaration='内容由AI生成',ai_generated=True))])
        validate(self.manifest,sealing=True);write_json(self.path,self.manifest)
        self.store=Store(self.root/'state');self.addCleanup(self.store.close)
        self.key=self.store.prepare(self.manifest,'weibo')['key'];self.store.authorize(self.manifest,'weibo','offline fixture only')
        self.request=self.store.directory/'requests'/f'{self.key}.json'
        self.page=Page();self.anchor=Page();self.anchor.ident='OriginalHomeTarget'
        self.uploads=0;self.fills=0;self.actions=[];self.mode='ok';self.readback=source_fixture()[3];self.readback['public']['statement']=1
        @contextlib.contextmanager
        def connect(profile,**kw):yield object()
        @contextlib.contextmanager
        def filled(anchor,pack,assets,bound,marker,endpoint):
            self.uploads+=1;self.fills+=1;_,_,saved,_=source_fixture();saved.update(bound,target_id=self.page.ident)
            saved['source_sha256']=saved['native_video']['source_sha256']=pack['assets'][0]['sha256']
            for k in ('prepared','crop'):saved['cover'][k]['source_sha256']=pack['assets'][1]['sha256']
            write_json(marker,saved);yield self.page,saved
        def identity(page,uid):
            if page.uid!=uid:raise PublishError('wrong native account')
        def ready(page,pack,*args):
            if self.mode=='changed' or (self.mode=='changed_at_input' and self.ready_calls):raise PublishError('author changed title')
            self.ready_calls+=1
            return dict(native.composer_state(snapshot(pack),pack),cover_pid=PID,default_options=[])
        def click(page,button,*,before_input):
            self.assertTrue(read_json(home.marker_path(self.request)).get('submission_attempted_at'))
            self.assertEqual(self.store.status(self.key)['state'],'submission_unknown')
            before_input();page.clicks+=1
            p=params(self.current_pack(),1)
            if self.mode=='bad_input':p['content']='different'
            page.listeners['request'](Request(p))
            if self.mode=='no_receipt':return
            result=body() if self.mode!='media_id_only' else dict(media_id=VID)
            r=type('Response',(),dict(url='https://weibo.com/ajax/statuses/update',status=200,
                request=Request(p),json=lambda _:dict(ok=1,data=result)))()
            page.listeners['response'](r)
            if self.mode=='duplicate':page.listeners['response'](r)
            if self.mode=='lost_reply':raise RuntimeError('native reply lost')
        self.ready_calls=0
        patch.object(home,'connect',side_effect=connect).start();patch.object(home,'select_page',return_value=self.anchor).start()
        patch.object(browser_engine,'target_id',side_effect=lambda page:page.ident).start()
        patch.object(home,'assert_identity',side_effect=identity).start();patch.object(prefill,'filled_editor',side_effect=filled).start()
        patch.object(sender,'ready',side_effect=ready).start();patch.object(sender,'click_background',side_effect=click).start()
        patch.object(sender.post,'observe_existing',side_effect=lambda *args:copy.deepcopy(self.readback)).start()
        patch('runtime.inspect',return_value=dict(ok=True,python='/isolated/python')).start()
        def invoke(config,action,path,*,lock_fd):
            self.actions.append(action);return browser_engine.dispatch(path,action,self.profile,lock_fd)
        patch('publish_core.invoke',side_effect=invoke).start()

    def current_pack(self):
        from platform_pack import build_pack
        return build_pack(read_json(self.path),'weibo',self.profile,check_files=False)

    def readback_result(self):
        return read_json(home.marker_path(self.request).with_suffix('.weibo-video-readback.json'))['result']

    def test_unified_sender_same_id_source_bound_recovery_after_editor_and_sources_gone(self):
        result=run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(result['state'],'published');self.assertEqual(result['remote_id'],RID)
        proof=self.readback_result();self.assertTrue(proof['content_verified']);self.assertFalse(proof['remote_media_bytes_verified'])
        self.assertEqual(self.page.closes,1)
        (self.root/'video.mp4').unlink();(self.root/'cover.png').unlink()
        self.page.get_by_role=lambda *args,**kw:(_ for _ in ()).throw(AssertionError('closed editor must not be read'))
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual((self.uploads,self.fills,self.page.clicks),(1,1,1));self.assertEqual(self.actions,['execute','verify','verify'])

    def test_accepted_id_survives_lost_click_reply(self):
        self.mode='lost_reply';self.assertEqual(run(self.path,'weibo',self.profile,self.store)['state'],'published')
        self.assertEqual(read_json(home.marker_path(self.request))['accepted']['remote_id'],RID)

    def test_unknown_or_media_id_only_never_reuploads_or_reclicks(self):
        self.mode='no_receipt'
        with patch.object(sender.time,'monotonic',side_effect=[0,21]),self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual((self.uploads,self.fills,self.page.clicks),(1,1,1))

    def test_media_id_cannot_substitute_public_post_id(self):
        self.mode='media_id_only'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertNotIn('accepted',read_json(home.marker_path(self.request)))
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_changed_at_input_or_checkpoint_failure_stops_before_click(self):
        self.mode='changed_at_input'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual((self.uploads,self.fills),(1,1))

    def test_bad_native_input_preserves_id_but_never_reports_success(self):
        self.mode='bad_input'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['remote_id'],RID)
        self.assertTrue(read_json(home.marker_path(self.request)).get('receipt_error'))
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_wrong_readback_keeps_id_then_recovers_only_same_post(self):
        self.readback['edit']['width']=1280
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['remote_id'],RID)
        self.readback['edit']['width']=1920
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual((self.uploads,self.fills,self.page.clicks),(1,1,1))

    def test_missing_or_changed_public_declaration_stays_pending(self):
        self.readback['public']['statement']=None
        result=run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(result['state'],'pending');self.assertFalse(self.readback_result()['content_verified'])
        self.readback['public']['statement']=4
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'pending')
        self.readback['public']['statement']=1
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'published')
        self.assertEqual(self.page.clicks,1)

    def test_default_disabled_and_restricted_accounts_never_start_browser(self):
        self.profile['adapters']['weibo']['enabled_capabilities']=[]
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.uploads,0)

    def test_restricted_account_or_wrong_login_never_uploads(self):
        self.profile['accounts']['author']['restricted']=True
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.uploads,0)

    def test_wrong_logged_in_account_never_uploads(self):
        self.anchor.uid='9999999999'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.uploads,0)

    def test_duplicate_native_response_preserves_first_id_and_refuses_success(self):
        self.mode='duplicate'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.store.status(self.key)['remote_id'],RID)
        self.assertEqual(read_json(home.marker_path(self.request))['native_response_count'],2)
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_first_attempt_checkpoint_failure_stops_before_click(self):
        original=shared.write_json
        def fail(path,value):
            if Path(path)==home.marker_path(self.request) and value.get('submission_attempted_at'):raise OSError('disk full')
            return original(path,value)
        with patch.object(shared,'write_json',side_effect=fail),self.assertRaises(OSError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.uploads,1)

    def test_native_input_checkpoint_failure_stops_before_click(self):
        original=shared.write_json
        def fail(path,value):
            if Path(path)==home.marker_path(self.request) and value.get('native_input_checkpointed_at'):raise OSError('disk full')
            return original(path,value)
        with patch.object(shared,'write_json',side_effect=fail),self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        self.assertEqual(self.page.clicks,0)
        self.assertNotIn('native_input_checkpointed_at',read_json(home.marker_path(self.request)))
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.uploads,1)

    def test_failed_native_input_receipt_write_retains_id_but_not_success(self):
        original=shared.write_json
        def fail(path,value):
            if Path(path)==home.marker_path(self.request) and value.get('native_input_receipt'):raise OSError('disk full')
            return original(path,value)
        with patch.object(shared,'write_json',side_effect=fail),self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        saved=read_json(home.marker_path(self.request));self.assertEqual(saved['accepted']['remote_id'],RID)
        self.assertNotIn('native_input_receipt',saved);self.assertTrue(saved['receipt_error'])
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_failed_receipt_write_cannot_be_revived_by_final_save(self):
        original=shared.write_json
        def fail(path,value):
            if Path(path)==home.marker_path(self.request) and value.get('accepted'):raise OSError('disk full')
            return original(path,value)
        with patch.object(shared,'write_json',side_effect=fail),self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        saved=read_json(home.marker_path(self.request));self.assertNotIn('accepted',saved)
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual(self.page.clicks,1)

    def test_lost_acceptance_recovers_only_same_video_then_rechecks_on_normal_resume(self):
        self.mode='media_id_only'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        result=run(self.path,'weibo',self.profile,self.store,resume=True,recover_remote_id=RID,recovery_platform='weibo')
        self.assertEqual(result['state'],'published');self.assertEqual(result['remote_id'],RID)
        self.assertTrue(self.readback_result()['recovery_verified'])
        self.assertEqual(run(self.path,'weibo',self.profile,self.store,resume=True)['state'],'published')
        self.readback['public']['statement']=4
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True)
        self.assertEqual((self.uploads,self.fills,self.page.clicks),(1,1,1))

    def test_candidate_recovery_mutations_cannot_claim_or_repeat_submission(self):
        self.mode='media_id_only'
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store)
        marker=home.marker_path(self.request);original=read_json(marker)
        mutations=[dict(native_input_checkpointed_at=None),dict(native_input_count=0),dict(native_response_count=2),
                   dict(pack_digest='wrong'),dict(source_sha256='f'*64),dict(accepted=dict(remote_id=RID,identity=UID))]
        for mutation in mutations:
            write_json(marker,dict(original,**mutation))
            with self.subTest(mutation=mutation),self.assertRaises(PublishError):
                run(self.path,'weibo',self.profile,self.store,resume=True,recover_remote_id=RID,recovery_platform='weibo')
            self.assertIsNone(self.store.status(self.key)['remote_id'])
        write_json(marker,original)
        for candidate,platform in [('111','weibo'),(RID,'x')]:
            with self.subTest(candidate=candidate,platform=platform),self.assertRaises(PublishError):
                run(self.path,'weibo',self.profile,self.store,resume=True,recover_remote_id=candidate,recovery_platform=platform)
        for changes in (dict(statement=4),dict(title='其他集')):
            previous=copy.deepcopy(self.readback);self.readback['public'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(PublishError):
                run(self.path,'weibo',self.profile,self.store,resume=True,recover_remote_id=RID,recovery_platform='weibo')
            self.readback=previous
        self.readback['edit']['playlists']=['unselected']
        with self.assertRaises(PublishError):run(self.path,'weibo',self.profile,self.store,resume=True,recover_remote_id=RID,recovery_platform='weibo')
        self.assertEqual((self.uploads,self.fills,self.page.clicks),(1,1,1))


if __name__=='__main__':unittest.main()
