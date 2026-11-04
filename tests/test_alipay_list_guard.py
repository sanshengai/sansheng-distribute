import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import alipay_list_guard as g
URL='https://c.alipay.com/page/content-creation/posts?appId=1'
def resp(rows,total=None):return {'stat':'ok','result':{'total':len(rows) if total is None else total,'publishContents':rows}}
def row(i,t):return {'contentId':i,'title':t,'state':'已发布'}
class Evaluate(unittest.TestCase):
    def test_login_redirect_empty_is_not_clear(self):
        r=g.evaluate('https://auth.alipay.com/login/index.htm',None,[],'S5E02')
        self.assertEqual(r['status'],g.UNVERIFIABLE);self.assertEqual(g.EXIT[r['status']],5)
    def test_no_account_id_or_no_response_is_unverifiable(self):
        self.assertEqual(g.evaluate(URL,False,[resp([])],'S5E02')['status'],g.UNVERIFIABLE)
        self.assertEqual(g.evaluate(URL,True,[],'S5E02')['status'],g.UNVERIFIABLE)
    def test_bad_response_or_missing_total(self):
        self.assertEqual(g.evaluate(URL,True,[{'stat':'fail'}],'S5E02')['status'],g.UNVERIFIABLE)
        self.assertEqual(g.evaluate(URL,True,[{'stat':'ok','result':{'publishContents':[]}}],'S5E02')['status'],g.UNVERIFIABLE)
    def test_incomplete_pagination_is_unverifiable(self):
        r=g.evaluate(URL,True,[resp([row('a','「初馨」S4E08｜x')],total=32)],'S1E08')
        self.assertEqual(r['status'],g.UNVERIFIABLE);self.assertIn('分页',r['reasons'][0])
    def test_lookalike_host_rejected(self):
        self.assertEqual(g.evaluate('https://c.alipay.com.evil.cn/page/content-creation/posts',True,[resp([])],'S5E02')['status'],g.UNVERIFIABLE)
    def test_clear_and_duplicate(self):
        rs=[resp([row('a','「初馨」S5E01｜x')])]
        self.assertEqual(g.evaluate(URL,True,rs,'S5E02')['status'],g.CLEAR)
        self.assertEqual(g.evaluate(URL,True,rs,'S5E01')['status'],g.DUPLICATE)
        self.assertEqual(g.evaluate(URL,True,[resp([])],'S5E01')['status'],g.CLEAR)
    def test_superseded_only_passes_only_for_declared_ids(self):
        rs=[resp([row('old','「初馨」S1E08｜x'),row('z','「初馨」S1E07｜y')])]
        self.assertEqual(g.evaluate(URL,True,rs,'S1E08',['old'])['status'],g.SUPERSEDED)
        rs2=[resp([row('old','「初馨」S1E08｜x'),row('other','「初馨」S1E08｜x')])]
        self.assertEqual(g.evaluate(URL,True,rs2,'S1E08',['old'])['status'],g.DUPLICATE)
        self.assertEqual(g.evaluate(URL,True,rs,'S1E08',['nope'])['status'],g.UNVERIFIABLE)
    def test_readback_expects_exactly_one_new(self):
        rs=[resp([row('old','「初馨」S1E08｜x'),row('new','「初馨」S1E08｜x')])]
        r=g.evaluate(URL,True,rs,'S1E08',['old'],expected_new=1)
        self.assertEqual(r['status'],g.CLEAR);self.assertEqual(r['new_hits'][0]['contentId'],'new')
        self.assertEqual(g.evaluate(URL,True,[resp([row('old','「初馨」S1E08｜x')])],'S1E08',['old'],expected_new=1)['status'],g.DUPLICATE)
    def test_pages_merge_by_content_id(self):
        rs=[resp([row('a','「初馨」S1E01｜x')],total=2),resp([row('a','「初馨」S1E01｜x'),row('b','「初馨」S1E02｜y')],total=2)]
        self.assertEqual(g.evaluate(URL,True,rs,'S1E03')['status'],g.CLEAR)
class Resend(unittest.TestCase):
    def setUp(self):
        self.d=Path(tempfile.mkdtemp());self.v=self.d/'v.mp4';self.c=self.d/'c.png';self.v.write_bytes(b'v1');self.c.write_bytes(b'c');self.l=self.d/'l.jsonl'
    def mk(self,**k):
        a=dict(ledger=self.l,ep='S1E08',video=self.v,cover=self.c,title='t',description='d',supersedes=['old'],revision='去掉结尾引导')
        a.update(k);return g.create_resend(**a)
    def test_requires_revision_and_supersedes(self):
        with self.assertRaises(ValueError):self.mk(revision=' ')
        with self.assertRaises(ValueError):self.mk(supersedes=[])
    def test_records_sha_and_rejects_same_task_twice(self):
        r=self.mk();self.assertEqual(r['video_sha256'],g.sha256(self.v));self.assertEqual(r['supersedes'],['old'])
        self.assertEqual(oct(self.l.stat().st_mode&0o777),'0o600')
        with self.assertRaises(ValueError):self.mk()
        self.v.write_bytes(b'v2')
        with self.assertRaises(ValueError):self.mk()  # 旧作品已被声明替代
        self.assertEqual(g.load_task(self.l,'S1E08')['task_id'],r['task_id'])
    def test_cli_exit_codes(self):
        o=self.d/'o.json';o.write_text(json.dumps({'url':'https://auth.alipay.com/x','account_id_seen':False,'responses':[]}))
        self.assertEqual(g.main(['evaluate',str(o),'--ep','S5E02']),5)
if __name__=='__main__':unittest.main()


class SeriesCloseoutTests(unittest.TestCase):
    URL='https://c.alipay.com/page/content-creation/posts?appId=1'
    def row(self,ep,cid):return {'contentId':cid,'title':f'「初馨」{ep}｜x'}
    def test_each_episode_exactly_one_passes(self):
        rows=[self.row(f'S5E0{i}',f'c{i}') for i in range(1,9)]
        r=g.evaluate_series(self.URL,True,[resp(rows)],[f'S5E0{i}' for i in range(1,9)])
        self.assertEqual(r['status'],g.CLEAR);self.assertEqual(g.EXIT[r['status']],0)
    def test_just_published_last_episode_is_not_duplicate(self):
        rows=[self.row('S5E08','new')]
        self.assertEqual(g.evaluate(self.URL,True,[resp(rows)],'S5E08')['status'],g.DUPLICATE)  # 发前查重语义
        self.assertEqual(g.evaluate_series(self.URL,True,[resp(rows)],['S5E08'])['status'],g.CLEAR)
    def test_missing_or_extra_fails(self):
        rows=[self.row('S5E01','a'),self.row('S5E01','b')]
        r=g.evaluate_series(self.URL,True,[resp(rows)],['S5E01','S5E02'])
        self.assertEqual(r['status'],g.DUPLICATE);self.assertEqual(len(r['reasons']),2)
    def test_unreadable_list_is_unverifiable(self):
        r=g.evaluate_series('https://auth.alipay.com/login/index.htm',None,[],['S5E01'])
        self.assertEqual(r['status'],g.UNVERIFIABLE)
