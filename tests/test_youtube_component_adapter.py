import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from publish_core import PublishError, digest, file_hash
from youtube_adapter import APIClient, dispatch, extract_component_spec, make_plan, preflight_youtube

CHANNEL = 'UC' + 'x'*22
VIDEO = 'a'*11
PLAYLIST = 'p'*20


class FakeClient:
    items = []
    scopes = {'https://www.googleapis.com/auth/youtube'}
    calls = []
    def __init__(self, token): pass
    def granted_scopes(self): return self.scopes
    def thumbnail(self, url, video):
        if url != f'https://i.ytimg.com/vi/{video}/hqdefault.jpg': raise PublishError('wrong image')
        return b'\xff\xd8\xfffixture'
    def call(self, method, url, *, headers=None, data=None):
        self.calls.append((method,url))
        if '/channels?' in url: body={'items':[{'id':CHANNEL}]}
        elif '/videos?' in url: body={'items':[{'id':VIDEO,'snippet':{'channelId':CHANNEL,'thumbnails':{'high':{'url':f'https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg'}}}}]}
        elif '/playlists?' in url: body={'items':[{'id':PLAYLIST,'snippet':{'channelId':CHANNEL}}]}
        elif '/playlistItems?' in url and method=='GET': body={'items':self.items}
        elif '/playlistItems?' in url and method=='POST':
            sn=json.loads(data)['snippet']; body={'id':'item','snippet':sn}
        elif '/thumbnails/set?' in url and method=='POST': body={'items':[{'default':{}}]}
        else: raise AssertionError(url)
        return 200, {}, json.dumps(body).encode()


class ComponentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.cover=self.root/'cover.png'
        self.cover.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        self.target={'id':'target','platform':'youtube','account':'owner'}
        self.manifest={'targets':[self.target]}; self.manifest_path=self.root/'manifest.json'
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.profile={'accounts':{'owner':{'platforms':['youtube'],'public_identities':{'youtube':CHANNEL},'youtube_token_env':'TEST_YOUTUBE_TOKEN'}}}
        self.profile_path=self.root/'profile.json'; self.profile_path.write_text(json.dumps(self.profile))
        self.env=patch.dict(os.environ,{'TEST_YOUTUBE_TOKEN':'fixture'}); self.env.start(); self.addCleanup(self.env.stop)
        FakeClient.items=[]; FakeClient.calls=[]; FakeClient.scopes={'https://www.googleapis.com/auth/youtube'}

    def request(self,part):
        binding={'original_job_key':'e'*64,'video_id':VIDEO,'channel':CHANNEL,
                 'cover':{'id':'cover','absolute_path':str(self.cover),'sha256':file_hash(self.cover)},
                 'playlist_id':PLAYLIST,'playlist_position':0}
        key=digest(binding)
        component={**binding,'component_key':key,'component_digest':key,'part':part}
        request={'manifest_path':str(self.manifest_path),'manifest_digest':digest(self.manifest),
                 'target':self.target,'job':{'key':'e'*64,'remote_id':VIDEO},'component_job':component}
        path=self.root/'request.json'; path.write_text(json.dumps(request)); return path, request

    def item(self, video=VIDEO, pos=0):
        return {'id':'item','snippet':{'playlistId':PLAYLIST,'resourceId':{'videoId':video},'position':pos}}

    def test_playlist_submit_then_read_back_same_item(self):
        path,_=self.request('playlist')
        self.assertEqual(dispatch(path,'component',self.profile,FakeClient)['state'],'accepted')
        self.assertEqual(sum(method=='POST' for method,_ in FakeClient.calls),1)
        FakeClient.items=[self.item()]; FakeClient.calls=[]
        self.assertEqual(dispatch(path,'verify_component',self.profile,FakeClient)['state'],'verified')
        self.assertFalse(any(method=='POST' for method,_ in FakeClient.calls))

    def test_existing_item_never_inserted_again(self):
        path,_=self.request('playlist'); FakeClient.items=[self.item()]
        self.assertEqual(dispatch(path,'component',self.profile,FakeClient)['state'],'accepted')
        self.assertFalse(any(method=='POST' for method,_ in FakeClient.calls))

    def test_missing_scope_stops_before_write(self):
        path,_=self.request('playlist'); FakeClient.scopes=set()
        result=dispatch(path,'component',self.profile,FakeClient)
        self.assertEqual((result['state'],result['no_write']),('needs_authorization',True))
        self.assertFalse(any(method=='POST' for method,_ in FakeClient.calls))

    def test_duplicate_or_wrong_position_never_writes(self):
        path,_=self.request('playlist')
        for items in ([self.item(),self.item()],[self.item(pos=1)],[self.item(video='b'*11)]):
            FakeClient.items=items; FakeClient.calls=[]
            with self.assertRaises(PublishError): dispatch(path,'component',self.profile,FakeClient)
            self.assertFalse(any(method=='POST' for method,_ in FakeClient.calls))

    def test_cover_write_and_readback_requires_visual_review(self):
        path,_=self.request('cover')
        self.assertEqual(dispatch(path,'component',self.profile,FakeClient)['state'],'accepted')
        FakeClient.calls=[]; result=dispatch(path,'verify_component',self.profile,FakeClient)
        self.assertEqual(result['state'],'needs_visual_review')
        self.assertEqual(result['cover_sha256'],file_hash(self.cover))
        self.assertEqual(result['thumbnail_sha256'],file_hash(result['thumbnail_path']))
        self.assertEqual(Path(result['thumbnail_path']).stat().st_mode & 0o777,0o600)
        self.assertFalse(any(method=='POST' for method,_ in FakeClient.calls))

    def test_changed_cover_or_binding_rejected(self):
        path,request=self.request('cover'); self.cover.write_bytes(b'changed')
        with self.assertRaises(PublishError): dispatch(path,'component',self.profile,FakeClient)
        self.assertFalse(any(method=='POST' for method,_ in FakeClient.calls))
        request['component_job']['video_id']='b'*11; path.write_text(json.dumps(request))
        with self.assertRaises(PublishError): dispatch(path,'verify_component',self.profile,FakeClient)

    def test_component_preflight_does_not_require_video_and_restricted_read_is_allowed(self):
        self.profile['accounts']['owner']['restricted']=True
        self.profile_path.write_text(json.dumps(self.profile))
        cmd=['python3',str(ROOT/'scripts/youtube_adapter.py'),'verify_component','--profile',str(self.profile_path)]
        self.assertTrue(preflight_youtube(self.manifest,self.target,self.profile,cmd,'verify_component')['local_only'])
        cmd[2]='component'
        with self.assertRaises(PublishError): preflight_youtube(self.manifest,self.target,self.profile,cmd,'component')

    def test_thumbnail_rejects_wrong_video_and_untrusted_host_before_network(self):
        client=APIClient('fixture')
        for url in (f'https://evil.invalid/vi/{VIDEO}/hqdefault.jpg',
                    f'http://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg',
                    'https://i.ytimg.com/vi/'+'b'*11+'/hqdefault.jpg',
                    f'https://i.ytimg.com/vi/{VIDEO}/../../secret',
                    f'https://i.ytimg.com/vi/{VIDEO}/anything.jpg'):
            with self.subTest(url=url), self.assertRaises(PublishError): client.thumbnail(url,VIDEO)

    def test_bad_thumbnail_bytes_are_not_review_evidence(self):
        path,_=self.request('cover')
        with patch.object(FakeClient,'thumbnail',return_value=b'not an image'):
            self.assertEqual(dispatch(path,'verify_component',self.profile,FakeClient)['state'],'unknown')

    def test_job_and_manifest_binding_cannot_be_swapped(self):
        path,request=self.request('playlist')
        request['job']['remote_id']='b'*11; path.write_text(json.dumps(request))
        with self.assertRaises(PublishError): dispatch(path,'component',self.profile,FakeClient)
        self.assertFalse(any(method=='POST' for method,_ in FakeClient.calls))

    def test_extract_bundle_is_read_only_and_regular_upload_remains_fail_closed(self):
        cover={'id':'cover','kind':'image','absolute_path':str(self.cover),'sha256':file_hash(self.cover)}
        pack={'platform':'youtube','content_type':'video','endpoint':'publish',
              'platform_metadata':{'visibility':'public','made_for_kids':False,'playlist_id':PLAYLIST,'playlist_position':0},
              'fields':{'title':'Title','text':'','description':'','links':[],'tags':[]},
              'assets':[{'id':'video','kind':'video'},cover],'cover_asset_id':'cover','subtitle_asset_ids':[]}
        self.assertEqual(extract_component_spec(pack),{'cover':{'id':'cover','absolute_path':str(self.cover),
                         'sha256':file_hash(self.cover)},'playlist_id':PLAYLIST,'playlist_position':0})
        with self.assertRaises(PublishError): make_plan(pack)
        pack['assets'].append({'id':'extra','kind':'image'})
        with self.assertRaises(PublishError): extract_component_spec(pack)

    def test_extract_bundle_rejects_missing_cover_and_invalid_position(self):
        pack={'platform_metadata':{'playlist_id':PLAYLIST,'playlist_position':True},
              'assets':[{'id':'video','kind':'video'}],'cover_asset_id':None,'subtitle_asset_ids':[]}
        with self.assertRaises(PublishError): extract_component_spec(pack)
        pack['platform_metadata']['playlist_position']=0; pack['cover_asset_id']='missing'
        with self.assertRaises(PublishError): extract_component_spec(pack)

    def test_bundle_video_is_only_pending_after_video_readback(self):
        sys.path.insert(0,str(ROOT/'tests'))
        import test_youtube_adapter as video_fixtures
        original=video_fixtures.YoutubeAdapterTest(methodName='runTest'); original.setUp()
        self.addCleanup(original.doCleanups)
        from publish_core import validate
        (original.root/'cover.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        original.manifest['assets'].append({'id':'c','path':'cover.png','kind':'image'})
        target=original.manifest['targets'][0]; target['asset_ids'].append('c');target['cover_asset_id']='c'
        target['platform_metadata'].update(playlist_id=PLAYLIST,playlist_position=0)
        validate(original.manifest,sealing=True)
        original.mpath.write_text(json.dumps(original.manifest))
        original.request.update(manifest_digest=digest(original.manifest),target=target)
        original.rpath.write_text(json.dumps(original.request))
        with self.assertRaises(PublishError): original.run_adapter('execute')
        self.assertFalse(any(c[0]=='POST' for c in original.fake.calls))
        original.request['youtube_bundle']=True; original.rpath.write_text(json.dumps(original.request))
        self.assertEqual(original.run_adapter('execute')['state'],'pending')
        verified=original.run_adapter('verify')
        self.assertEqual(verified['state'],'pending')
        self.assertTrue(verified['video_only_verified'])
        self.assertEqual(verified['stage'],'youtube_components_pending')
        self.assertNotIn('content_verified',verified)
        self.assertEqual(verified['verified_asset_ids'],['v'])
        self.assertEqual(sum(c[0]=='POST' for c in original.fake.calls),1)

    def test_bundle_recovery_preflight_is_offline_after_source_deleted_and_rejects_spoofing(self):
        sys.path.insert(0,str(ROOT/'tests'))
        import test_youtube_adapter as video_fixtures
        original=video_fixtures.YoutubeAdapterTest(methodName='runTest'); original.setUp()
        self.addCleanup(original.doCleanups)
        from publish_core import validate
        cover=original.root/'cover.png'; cover.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        original.manifest['assets'].append({'id':'c','path':'cover.png','kind':'image'})
        target=original.manifest['targets'][0]; target['asset_ids'].append('c'); target['cover_asset_id']='c'
        target['platform_metadata'].update(playlist_id=PLAYLIST,playlist_position=0)
        validate(original.manifest,sealing=True)
        original.profile['accounts']['owner']['restricted']=True
        profile=original.root/'profile.json'; profile.write_text(json.dumps(original.profile))
        cmd=[sys.executable,str(ROOT/'scripts/youtube_adapter.py'),'verify','--profile',str(profile)]
        (original.root/'movie.mp4').unlink(); cover.unlink()
        with patch('youtube_adapter.refresh_access_token') as refresh:
            result=preflight_youtube(original.manifest,target,original.profile,cmd,'verify',bundle=True,recovery=True)
            self.assertTrue(result['local_only']); refresh.assert_not_called()
        bad=cmd.copy(); bad[1]=str(original.root/'youtube_adapter.py')
        with self.assertRaises(PublishError):
            preflight_youtube(original.manifest,target,original.profile,bad,'verify',bundle=True,recovery=True)
        other=json.loads(json.dumps(original.profile)); other['accounts']['owner']['public_identities']['youtube']='UC'+'b'*22
        profile.write_text(json.dumps(other))
        with self.assertRaises(PublishError):
            preflight_youtube(original.manifest,target,original.profile,cmd,'verify',bundle=True,recovery=True)
        with self.assertRaises(PublishError):
            preflight_youtube(original.manifest,target,original.profile,cmd,'execute',bundle=True,recovery=True)


if __name__=='__main__': unittest.main()
