"""weibo.com/ anchor closed externally -> reopen one in background (2026-11-03 S5 pre-check)."""
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

class WeiboAnchorTests(unittest.TestCase):
    def test_existing_anchor_used_nothing_opened(self):
        c=Conn(['https://weibo.com/','https://muse.ai/'])
        self.assertEqual(br.weibo_home_anchor(c,sleep=lambda s:None).url,'https://weibo.com/');self.assertEqual(c.sent,[])
    def test_www_origin_counts_as_existing(self):
        c=Conn(['https://www.weibo.com/'])
        self.assertEqual(br.weibo_home_anchor(c,sleep=lambda s:None).url,'https://www.weibo.com/');self.assertEqual(c.sent,[])
    def test_upload_page_is_not_an_anchor_and_missing_home_reopened(self):
        c=Conn(['https://weibo.com/upload/channel','https://muse.ai/'])
        self.assertEqual(br.weibo_home_anchor(c,sleep=lambda s:None).url,'https://weibo.com/')
        self.assertEqual(c.sent,[('Target.createTarget',{'url':'https://weibo.com/','background':True,'focus':False})])
    def test_two_home_tabs_take_first_without_opening(self):
        c=Conn(['https://weibo.com/','https://weibo.com/'])
        br.weibo_home_anchor(c,sleep=lambda s:None);self.assertEqual(c.sent,[])
    def test_reopen_that_never_appears_fails(self):
        c=Conn([],opens=False)
        with self.assertRaises(PublishError):br.weibo_home_anchor(c,wait_s=1,sleep=lambda s:None)
    def test_mismatched_url_refused(self):
        c=Conn([])
        with self.assertRaises(PublishError):br.home_anchor(c,'weibo',url='https://x.com/home',path='/')
        self.assertEqual(c.sent,[])

if __name__=='__main__':unittest.main()

class WeiboVideoAnchorFallback(unittest.TestCase):
    def test_execute_anchor_reopens_when_select_fails(self):
        import weibo_video_browser as wvb
        c=Conn(['https://muse.ai/'])
        orig=br.time.sleep
        anchor=wvb._anchor(c)
        self.assertEqual(anchor.url,'https://weibo.com/')
        self.assertEqual(c.sent,[('Target.createTarget',{'url':'https://weibo.com/','background':True,'focus':False})])
    def test_execute_anchor_unique_existing_untouched(self):
        import weibo_video_browser as wvb
        c=Conn(['https://weibo.com/'])
        self.assertEqual(wvb._anchor(c).url,'https://weibo.com/');self.assertEqual(c.sent,[])

class PumpTests(unittest.TestCase):
    def test_real_sleep_pumps_through_existing_page(self):
        calls=[]
        class PP(P):
            def wait_for_timeout(self,ms):calls.append(ms)
        c=Conn([]);c.ctx.pages=[PP('https://muse.ai/',c.ctx)]
        br._pump(c,br.time.sleep,0.5);self.assertEqual(calls,[500])
    def test_injected_sleep_used_as_is(self):
        got=[];c=Conn([]);br._pump(c,got.append,0.5);self.assertEqual(got,[0.5])
