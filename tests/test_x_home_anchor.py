"""x.com/home anchor closed externally -> reopen one in background (2026-11-02 S4E07 readback)."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_runtime as br
from publish_core import PublishError

class P:
    def __init__(self,url,ctx):self.url=url;self.context=ctx
class Ctx:
    def __init__(self):self.pages=[]
class Sess:
    def __init__(self,conn):self.conn=conn
    def send(self,m,params):
        self.conn.sent.append((m,params))
        if self.conn.opens:self.conn.ctx.pages.append(P(params['url'],self.conn.ctx))
    def detach(self):pass
class Conn:
    def __init__(self,urls,opens=True):
        self.ctx=Ctx();self.ctx.pages=[P(u,self.ctx) for u in urls];self.contexts=[self.ctx];self.sent=[];self.opens=opens
    def new_browser_cdp_session(self):return Sess(self)

class AnchorTests(unittest.TestCase):
    def test_existing_anchor_used_nothing_opened(self):
        c=Conn(['https://x.com/home','https://www.xiaohongshu.com/x'])
        self.assertEqual(br.x_home_anchor(c,sleep=lambda s:None).url,'https://x.com/home');self.assertEqual(c.sent,[])
    def test_missing_anchor_reopened_in_background(self):
        c=Conn(['https://creator.xiaohongshu.com/publish','https://x.com/compose/post'])
        self.assertEqual(br.x_home_anchor(c,sleep=lambda s:None).url,'https://x.com/home')
        self.assertEqual(c.sent,[('Target.createTarget',{'url':'https://x.com/home','background':True,'focus':False})])
    def test_reopen_that_never_appears_fails(self):
        c=Conn([],opens=False)
        with self.assertRaises(PublishError):br.x_home_anchor(c,wait_s=1,sleep=lambda s:None)

if __name__=='__main__':unittest.main()
