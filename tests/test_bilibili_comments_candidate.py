import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bilibili_comments_candidate as b
from interaction_drafts import prepare, listing
from interaction_inbox import open_db
from interaction_context import prepare as build, load_content
from interaction_dashboard import render
from interaction_observations import record
from publish_core import PublishError

AID, UID, BV = '123', '777', 'BV1AA411A7gJ'


def cfg():
    return {'enabled': True, 'account': 'example', 'uid': UID, 'credentials': '/private/bilibili.json',
            'not_before': '2020-01-01T00:00:00+00:00', 'works': [{'aid': AID, 'bvid': BV}]}


def comment(rid, *, root=0, parent=0, author=999):
    return {'rpid': rid, 'rpid_str': str(rid), 'oid': 123, 'oid_str': AID, 'type': 1,
            'mid': author, 'mid_str': str(author), 'root': root, 'root_str': str(root),
            'parent': parent, 'parent_str': str(parent), 'ctime': 1701241993,
            'state': 0, 'invisible': False, 'member': {'mid': str(author), 'uname': 'viewer'},
            'content': {'message': '关于视频里的英语，有个问题'}}


class Fake:
    def __init__(self, roots=None, children=None):
        self.roots = roots or []
        self.children = children or {}
        self.calls = []
        self.owner = UID
        self.identity = UID
        self.mutate = None

    def get(self, kind, **params):
        self.calls.append((kind, params))
        if kind == 'identity': return {'isLogin': True, 'mid': int(self.identity)}
        if kind == 'work': return {'aid': 123, 'bvid': BV, 'owner': {'mid': self.owner}}
        rows = self.roots if kind == 'threads' else self.children.get(params['root'], [])
        pn = params['pn']
        result = {'page': {'num': pn, 'size': 20, 'count': len(rows)},
                  'replies': copy.deepcopy(rows[(pn-1)*20:pn*20]) or None}
        if kind == 'replies':
            result['root'] = copy.deepcopy(next(r for r in self.roots if str(r['rpid']) == params['root']))
        if self.mutate: self.mutate(kind, params, result)
        return result


class BilibiliCommentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'private'

    def items(self):
        with open_db(self.root) as db:
            return [dict(r) for r in db.execute('SELECT * FROM interaction_items ORDER BY comment_id')]

    def test_real_consumer_reads_roots_children_and_deduplicates(self):
        f = Fake([comment(1)], {'1': [comment(2, root=1, parent=1, author=777),
                                      comment(3, root=1, parent=2)]})
        r = b.sync(self.root, cfg(), f)
        self.assertEqual((r['state'], r['added'], r['writes'], r['send_enabled']), ('complete', 3, 0, False))
        items = self.items()
        self.assertEqual([(x['comment_id'],x['parent_id'],x['is_own']) for x in items],
                         [('1',None,0),('2','1',1),('3','2',0)])
        self.assertEqual(b.sync(self.root,cfg(),f)['added'],0)
        self.assertTrue(all(k in ('identity','work','threads','replies') for k,p in f.calls))

    def test_empty_success_is_scoped_to_selected_work(self):
        r=b.sync(self.root,cfg(),Fake())
        self.assertEqual((r['state'],r['works'],r['scope']),('complete',1,'configured_works'))
        self.assertEqual(self.items(),[])
        with open_db(self.root) as db:
            s=db.execute('SELECT * FROM interaction_sources').fetchone()
            self.assertIn('1 个作品',s['error'])
            self.assertIsNotNone(s['last_success_at'])

    def test_disabled_empty_or_duplicate_config_fails_before_any_request(self):
        for change in ('disabled','empty','duplicate','future','bad_id'):
            c=cfg();f=Fake()
            if change=='disabled':c['enabled']=False
            if change=='empty':c['works']=[]
            if change=='duplicate':c['works']*=2
            if change=='future':c['not_before']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
            if change=='bad_id':c['works'][0]['aid']=True
            with self.subTest(change=change),self.assertRaises(PublishError):b.sync(self.root,c,f)
            self.assertEqual(f.calls,[])

    def test_wrong_identity_or_work_owner_rejects_before_comment_query(self):
        for key in ('identity','owner'):
            f=Fake([comment(1)]);setattr(f,key,'888')
            with self.subTest(key=key),self.assertRaises(PublishError):b.sync(self.root,cfg(),f)
            self.assertFalse(any(k in ('threads','replies') for k,p in f.calls))
            self.assertEqual(self.items(),[])

    def test_baseline_keeps_context_without_creating_reply_candidate(self):
        c=cfg();c['not_before']=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()
        f=Fake([comment(1)])
        self.assertEqual(b.sync(self.root,c,f)['baseline'],1)
        self.assertEqual(self.items()[0]['local_status'],'handled')
        with self.assertRaises(PublishError):prepare(self.root,'bilibili','example',UID,'1','谢谢')
        self.assertEqual(b.sync(self.root,c,f)['baseline'],0)
        with open_db(self.root) as db:self.assertEqual(db.execute('SELECT count(*) FROM interaction_bilibili_baselines').fetchone()[0],1)

    def test_new_child_on_old_root_becomes_new_candidate(self):
        c=cfg();c['not_before']=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()
        child=comment(2,root=1,parent=1);child['ctime']=int(datetime.now(timezone.utc).timestamp())-1
        r=b.sync(self.root,c,Fake([comment(1)],{'1':[child]}))
        self.assertEqual(r['baseline'],1)
        self.assertEqual([i['local_status'] for i in self.items()],['handled','new'])
        self.assertEqual(prepare(self.root,'bilibili','example',UID,'2','可以这样理解')['state'],'draft')

    def test_child_payload_cannot_cross_root(self):
        f=Fake([comment(1)],{'1':[comment(2,root=9,parent=1)]})
        with self.assertRaises(PublishError):b.sync(self.root,cfg(),f)
        self.assertEqual([x['comment_id'] for x in self.items()],['1'])

    def test_context_and_dashboard_disclose_read_scope_and_baseline(self):
        c=cfg();c['not_before']=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()
        child=comment(2,root=1,parent=1);child['ctime']=int(datetime.now(timezone.utc).timestamp())-1
        b.sync(self.root,c,Fake([comment(1)],{'1':[child]}))
        path=Path(self.temp.name)/'work.json'
        path.write_text(json.dumps({'platform':'bilibili','account':'example','channel_id':UID,
                                   'video_id':AID,'title':'英语视频','summary':'测试作品摘要'}));path.chmod(0o600)
        packet=build(self.root,'bilibili','example',UID,'2',load_content(path))
        self.assertIn('unofficial_platform_read',packet['warnings'])
        self.assertTrue(packet['provenance']['monitor_scope']['work_in_scope'])
        with open_db(self.root) as db:db.execute('UPDATE interaction_bilibili_scope SET video_ids=\'[]\'')
        self.assertIn('work_outside_current_monitor_scope',build(self.root,'bilibili','example',UID,'2',load_content(path))['warnings'])
        page=render(self.root/'interaction-inbox.sqlite3',include_text=True)
        self.assertIn('历史基线 1 条',page)
        self.assertIn('不计已回复',page)
        record(self.root,{'platform':'bilibili','account':'example','observed_at':'2026-01-01T00:00:00+00:00',
                          'state':'partial','scope':'旧网页观察','reason':'当时只检查了一页',
                          'next_action':'核对更多作品','evidence':'private-record','replies':[]})
        page=render(self.root/'interaction-inbox.sqlite3',include_text=True)
        self.assertEqual(page.count('<td>B 站</td>'),1)
        self.assertIn('查看同账号网页观察',page)
        self.assertIn('当时只检查了一页',page)

    def test_head_is_refreshed_and_history_cursor_advances(self):
        f=Fake([comment(i) for i in range(1,42)])
        self.assertEqual(b.sync(self.root,cfg(),f,max_pages=2,max_parents=1)['state'],'partial')
        self.assertEqual(len(self.items()),40)
        f.calls.clear()
        r=b.sync(self.root,cfg(),f,max_pages=2,max_parents=1)
        self.assertEqual([p['pn'] for k,p in f.calls if k=='threads'],[1,3])
        self.assertEqual(len(self.items()),41)
        self.assertEqual(r['state'],'partial')

    def test_parent_rotation_and_child_cursor_advance(self):
        f=Fake([comment(1),comment(2)],{'1':[comment(i,root=1,parent=1) for i in range(10,51)]})
        b.sync(self.root,cfg(),f,max_parents=1,max_reply_pages=2)
        f.calls.clear();b.sync(self.root,cfg(),f,max_parents=1,max_reply_pages=2)
        self.assertEqual([p['root'] for k,p in f.calls if k=='replies'],['2'])
        f.calls.clear();b.sync(self.root,cfg(),f,max_parents=1,max_reply_pages=2)
        self.assertEqual([p['pn'] for k,p in f.calls if k=='replies'],[1,3])
        self.assertEqual(len(self.items()),43)

    def test_duplicate_page_short_page_and_wrong_child_root_rejected(self):
        for case in ('duplicate','short','wrong_root'):
            root=self.root/case
            f=Fake([comment(i) for i in range(1,22)],{'1':[comment(90,root=1,parent=1)]})
            def mutate(kind,params,raw):
                if case=='duplicate' and kind=='threads' and params['pn']==2:raw['replies']=[comment(1)]
                if case=='short' and kind=='threads' and params['pn']==1:raw['replies']=raw['replies'][:2]
                if case=='wrong_root' and kind=='replies':raw['root']=comment(999)
            f.mutate=mutate
            with self.subTest(case=case),self.assertRaises(PublishError):b.sync(root,cfg(),f)
            with open_db(root) as db:self.assertEqual(db.execute('SELECT state FROM interaction_sources').fetchone()[0],'error')

    def test_malformed_identity_body_time_and_visibility_rejected(self):
        for case in ('oid','author','id_conflict','body','time','hidden','bool_type'):
            r=comment(1)
            if case=='oid':r['oid']=555;r['oid_str']='555'
            if case=='author':r['member']['mid']='555'
            if case=='id_conflict':r['rpid_str']='2'
            if case=='body':r['content']['message']=''
            if case=='time':r['ctime']=True
            if case=='hidden':r['invisible']=True
            if case=='bool_type':r['type']=True
            with self.subTest(case=case),self.assertRaises(PublishError):b.sync(self.root/case,cfg(),Fake([r]))

    def test_preserves_manual_handled_and_invalidates_changed_draft(self):
        f=Fake([comment(1),comment(2)])
        b.sync(self.root,cfg(),f)
        prepare(self.root,'bilibili','example',UID,'1','谢谢你的问题')
        with open_db(self.root) as db:db.execute("UPDATE interaction_items SET local_status='handled' WHERE comment_id='2'")
        f.roots[0]['content']['message']='修改过的原评'
        b.sync(self.root,cfg(),f)
        self.assertEqual(listing(self.root)[0]['state'],'needs_review')
        self.assertEqual(self.items()[1]['local_status'],'handled')

    def test_identity_rebinding_rejected_and_prior_data_retained(self):
        f=Fake([comment(1)]);b.sync(self.root,cfg(),f)
        f.roots[0]['mid']=888;f.roots[0]['mid_str']='888';f.roots[0]['member']['mid']='888'
        with self.assertRaises(PublishError):b.sync(self.root,cfg(),f)
        self.assertEqual(self.items()[0]['author_channel'],'999')

    def test_account_lock_rejects_second_sync_before_network(self):
        root=b._safe_state_dir(self.root);f=Fake()
        with b._account_lock(root,'example',UID),self.assertRaises(PublishError):b.sync(root,cfg(),f)
        self.assertEqual(f.calls,[])
        c=cfg();c['account']='another-alias'
        with b._account_lock(root,'example',UID),self.assertRaises(PublishError):b.sync(root,c,f)
        self.assertEqual(f.calls,[])

    def test_private_input_and_public_state_are_rejected(self):
        p=Path(self.temp.name)/'config.json';p.write_text(json.dumps(cfg()));p.chmod(0o644)
        with self.assertRaises(PublishError):b._private_json(p)
        p.chmod(0o600);self.assertEqual(b._private_json(p),cfg())
        link=p.with_name('link.json');link.symlink_to(p)
        with self.assertRaises(PublishError):b._private_json(link)
        repo=Path(__file__).resolve().parents[1]/'not-created-comment-state'
        with self.assertRaises(PublishError):b.sync(repo,cfg(),Fake())
        self.assertFalse(repo.exists())

    def test_http_transport_is_get_only_rejects_redirect_and_hides_body_errors(self):
        p=Path(self.temp.name)/'credential.json'
        p.write_text(json.dumps({'cookie_info':{'cookies':[{'name':'SESSDATA','value':'synthetic-secret'}]}}));p.chmod(0o600)
        seen=[]
        class Opener:
            def open(self,req,timeout):
                seen.append(req)
                return io.BytesIO(b'{"code":0,"data":{"ok":true}}')
        with patch.object(b.urllib.request,'build_opener',return_value=Opener()) as build:
            c=b.Client(p);self.assertIsInstance(build.call_args.args[0],b.NoRedirect)
            self.assertEqual(c.get('threads',oid=AID,pn=1,ps=20),{'ok':True})
        self.assertEqual(seen[0].get_method(),'GET');self.assertIsNone(seen[0].data)
        self.assertEqual(b.urllib.parse.urlsplit(seen[0].full_url).netloc,'api.bilibili.com')
        with self.assertRaises(PublishError):b.NoRedirect().redirect_request(seen[0],None,302,'',{},'https://other.invalid')
        with patch.object(c.opener,'open',return_value=io.BytesIO(b'{"code":-352,"message":"synthetic-secret"}')):
            with self.assertRaises(PublishError) as e:c.get('threads')
            self.assertNotIn('synthetic-secret',str(e.exception))
            self.assertIn('code=-352',str(e.exception));self.assertIn('threads',str(e.exception))
        failure=b.urllib.error.HTTPError('https://api.bilibili.com/?secret=synthetic-secret',412,'synthetic-secret',{},None)
        with patch.object(c.opener,'open',side_effect=failure):
            with self.assertRaises(PublishError) as e:c.get('identity')
            self.assertIn('HTTP 412',str(e.exception));self.assertIn('identity',str(e.exception))
            self.assertNotIn('synthetic-secret',str(e.exception))


if __name__=='__main__':unittest.main()
