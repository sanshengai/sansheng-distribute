import json,sqlite3,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_preinput as pre
import confirm_no_attempt
import no_attempt_recovery as na
from publish_core import PublishError,write_json

BOUND=dict(identity='@acct',pack_digest='sealed',cdp_url='http://127.0.0.1:9333')

class NoAttemptTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=root=Path(self.tmp.name);self.key='k'*8
  self.req=root/'requests';self.req.mkdir()
  self.marker=self.req/f'{self.key}.browser-attempt.json'
  self.request=dict(target=dict(platform='x'),manifest_path='m',job=dict(key=self.key,state='submission_unknown',remote_id=None))
  write_json(self.req/f'{self.key}.json',self.request)
  write_json(self.req/f'{self.key}.browser-error.json',dict(ok=False,action='execute'))
  db=sqlite3.connect(root/'publish.sqlite3')
  db.execute('CREATE TABLE jobs(key TEXT,state TEXT,remote_id TEXT)');db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,job_key TEXT,state TEXT,data TEXT)')
  db.execute("INSERT INTO jobs VALUES(?,?,NULL)",(self.key,'submission_unknown'));db.execute("INSERT INTO events(job_key,state,data) VALUES(?,?,?)",(self.key,'submission_unknown','{}'))
  db.commit();db.close()
  ev=root/'tl.json';ev.write_text('{}');self.tl=root/'timeline.json'
  self.tl.write_text(json.dumps(dict(observed_at='2026-10-31T15:40:00+08:00',account_identity='@acct',query='S3E01',matches=[],evidence_path=str(ev))))
  self.pack=dict(platform='x',account='a',manifest_digest='sealed',target_id='t')
 def tearDown(self):self.tmp.cleanup()
 def confirm(self,identity='@acct'):
  return confirm_no_attempt.main(['--state-dir',str(self.root),'--key',self.key,'--identity',identity,
   '--statement','作者原话：授权改 skill 加只读恢复办法（2026-10-31 15:37 台北）','--confirmed-at','2026-10-31T15:37:00+08:00','--timeline',str(self.tl)])
 def path(self):return na.confirmation_path(self.req,self.key)
 def test_without_confirmation_stays_unknown(self):
  self.assertIsNone(pre.recover('verify',self.request,self.pack,BOUND,self.marker,{}))
 def test_confirmed_verify_reports_not_submitted_and_execute_proceeds(self):
  self.assertEqual(self.confirm(),0)
  r=pre.recover('verify',self.request,self.pack,BOUND,self.marker,{})
  self.assertEqual(r['state'],'not_submitted');self.assertTrue(r['absence_verified'])
  self.assertEqual(r['absence_scope'],'author_confirmed_no_attempt_record')
  self.request['job']['state']='prepared'
  self.assertIsNone(pre.recover('execute',self.request,self.pack,BOUND,self.marker,{}))
 def test_any_request_trace_refuses(self):
  for name in ('browser-attempt.json.preinput-abc','browser-assets','native-raw.json','x-upload.json'):
   with self.subTest(name=name):
    t=self.req/f'{self.key}.{name}';t.write_text('{}')
    self.assertEqual(self.confirm(),2);self.assertFalse(self.path().exists());t.unlink()
 def test_trace_after_confirmation_refuses_verify(self):
  self.assertEqual(self.confirm(),0);(self.req/f'{self.key}.native-raw.json').write_text('{}')
  with self.assertRaises(PublishError):pre.recover('verify',self.request,self.pack,BOUND,self.marker,{})
 def test_ledger_observation_or_remote_id_refuses(self):
  db=sqlite3.connect(self.root/'publish.sqlite3');db.execute("INSERT INTO events(job_key,state,data) VALUES(?,?,?)",(self.key,'pending','{"remote_id":"1"}'));db.commit();db.close()
  self.assertEqual(self.confirm(),2);self.assertFalse(self.path().exists())
 def test_remote_id_job_refuses(self):
  db=sqlite3.connect(self.root/'publish.sqlite3');db.execute("UPDATE jobs SET remote_id='9'");db.commit();db.close()
  self.assertEqual(self.confirm(),2)
 def test_timeline_match_or_wrong_identity_refuses(self):
  d=json.loads(self.tl.read_text());d['matches']=['S3E01'];self.tl.write_text(json.dumps(d))
  self.assertEqual(self.confirm(),2);self.assertFalse(self.path().exists())
  d['matches']=[];self.tl.write_text(json.dumps(d));self.assertEqual(self.confirm('@other'),2)
 def test_non_x_platform_refuses(self):
  self.request['target']['platform']='weibo';write_json(self.req/f'{self.key}.json',self.request)
  self.assertEqual(self.confirm(),2)
  self.assertIsNone(na.recover('verify',self.request,dict(self.pack,platform='weibo'),BOUND,self.marker))
 def test_existing_marker_never_uses_this_path(self):
  self.assertEqual(self.confirm(),0);write_json(self.marker,dict(state='attempted'))
  self.assertIsNone(na.recover('verify',self.request,self.pack,BOUND,self.marker))
 def test_wrong_identity_at_verify_refuses(self):
  self.assertEqual(self.confirm(),0)
  with self.assertRaises(PublishError):na.recover('verify',self.request,self.pack,dict(BOUND,identity='@other'),self.marker)
if __name__=='__main__':unittest.main()
