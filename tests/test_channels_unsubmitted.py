import json,sqlite3,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import channels_unsubmitted as cu
from publish_core import PublishError,write_json

ID='sphTEST'
class ChannelsUnsubmittedTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=root=Path(self.tmp.name);self.key='c'*8
  self.req=root/'requests';self.req.mkdir()
  self.request=dict(manifest_digest='md',target=dict(id='S1E01-wechat_channels',platform='wechat_channels',account='main'),job=dict(key=self.key,state='submission_unknown',remote_id=None))
  write_json(self.req/f'{self.key}.json',self.request);write_json(self.req/f'{self.key}.social.json',{})
  ag=self.req/f'{self.key}.agent';ag.mkdir();(ag/'lease.lock').write_text('')
  write_json(ag/'task.json',dict(schema_version=1,status='closed',action='execute',nonce='n'))
  db=sqlite3.connect(root/'publish.sqlite3')
  db.execute('CREATE TABLE jobs(key TEXT,state TEXT,remote_id TEXT)');db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,job_key TEXT,state TEXT,data TEXT)')
  db.execute("INSERT INTO jobs VALUES(?,?,NULL)",(self.key,'submission_unknown'));db.execute("INSERT INTO events(job_key,state,data) VALUES(?,?,?)",(self.key,'submission_unknown','{}'))
  db.commit();db.close()
  ev=root/'list-evidence.json';ev.write_text('{}');self.ls=root/'listing.json'
  self.ls.write_text(json.dumps(dict(observed_at='2026-11-04T10:45:00+08:00',account_identity=ID,query='S1E01',matches=[],evidence_path=str(ev))))
 def tearDown(self):self.tmp.cleanup()
 def confirm(self,identity=ID):
  return cu.main(['--state-dir',str(self.root),'--key',self.key,'--identity',identity,'--statement','作者原话：没有手动发过（测试）','--confirmed-at','2026-11-04T10:42:00+08:00','--listing',str(self.ls)])
 def path(self):return cu.confirmation_path(self.req,self.key)
 def test_without_confirmation_stays_unknown(self):
  self.assertIsNone(cu.recover(self.request,self.req,ID))
 def test_confirmed_reports_not_submitted(self):
  self.assertEqual(self.confirm(),0)
  r=cu.recover(self.request,self.req,ID)
  self.assertEqual(r['state'],'not_submitted');self.assertTrue(r['absence_verified'])
  self.assertEqual(r['target_id'],'S1E01-wechat_channels')
 def test_wrong_identity_refuses(self):
  self.assertEqual(self.confirm(),0)
  with self.assertRaises(PublishError):cu.recover(self.request,self.req,'other')
 def test_relay_reply_or_receipt_refuses(self):
  for name in ('reply.json','n.receipt.json'):
   with self.subTest(name=name):
    t=self.req/f'{self.key}.agent'/name;t.write_text('{}')
    self.assertEqual(self.confirm(),2);self.assertFalse(self.path().exists());t.unlink()
 def test_relay_change_after_confirmation_refuses(self):
  self.assertEqual(self.confirm(),0);(self.req/f'{self.key}.agent'/'n.receipt.json').write_text('{}')
  with self.assertRaises(PublishError):cu.recover(self.request,self.req,ID)
 def test_responded_or_verify_task_refuses(self):
  for st,act in (('responded','execute'),('closed','verify'),('active','execute')):
   with self.subTest(st=st,act=act):
    write_json(self.req/f'{self.key}.agent'/'task.json',dict(schema_version=1,status=st,action=act,nonce='n'))
    self.assertEqual(self.confirm(),2)
 def test_ledger_observation_or_remote_id_refuses(self):
  db=sqlite3.connect(self.root/'publish.sqlite3');db.execute("INSERT INTO events(job_key,state,data) VALUES(?,?,?)",(self.key,'pending','{"remote_id":"1"}'));db.commit();db.close()
  self.assertEqual(self.confirm(),2)
 def test_remote_id_job_refuses(self):
  db=sqlite3.connect(self.root/'publish.sqlite3');db.execute("UPDATE jobs SET remote_id='9'");db.commit();db.close()
  self.assertEqual(self.confirm(),2)
 def test_listing_match_refuses(self):
  d=json.loads(self.ls.read_text());d['matches']=['x'];self.ls.write_text(json.dumps(d))
  self.assertEqual(self.confirm(),2);self.assertFalse(self.path().exists())
 def test_other_platform_refuses(self):
  self.request['target']['platform']='douyin';write_json(self.req/f'{self.key}.json',self.request)
  self.assertEqual(self.confirm(),2)
 def test_extra_request_trace_refuses(self):
  (self.req/f'{self.key}.browser-attempt.json').write_text('{}')
  self.assertEqual(self.confirm(),2)
if __name__=='__main__':unittest.main()
