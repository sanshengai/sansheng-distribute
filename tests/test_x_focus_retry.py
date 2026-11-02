"""Owned X window must prove focus emulation; re-establish when it did not apply (S4E02 2026-11-02)."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import x_compose_ready as xc
from publish_core import PublishError


class Session:
    def __init__(self,page):self.page=page;self.detached=False
    def send(self,method,params=None):
        if method=='Emulation.setFocusEmulationEnabled':
            if self.page.fail_new:self.page.fail_new-=1;raise RuntimeError('target not ready')
            self.page.applied=self.page.apply_after<=self.page.sessions
    def detach(self):self.detached=True;self.page.applied=False


class Context:
    def __init__(self,page):self.page=page
    def new_cdp_session(self,page):
        self.page.sessions+=1;s=Session(self.page);self.page.made.append(s);return s


class Page:
    def __init__(self,apply_after=1,fail_new=0):
        self.apply_after,self.fail_new,self.sessions,self.applied,self.made,self.waits=apply_after,fail_new,0,False,[],0
        self.context=Context(self)
    def evaluate(self,js):
        return {'vis':'visible','focus':True} if self.applied else {'vis':'hidden','focus':False}
    def wait_for_timeout(self,ms):self.waits+=1


class FocusRetryTests(unittest.TestCase):
    def tearDown(self):xc._HELD.clear()

    def test_first_session_ok(self):
        p=Page();log=[]
        self.assertEqual(xc.ensure_focus(p,force=True,record=log.append)['vis'],'visible')
        self.assertEqual(p.sessions,1);self.assertEqual(p.waits,0)

    def test_stale_session_released_and_recreated(self):
        # the observed failure: the held session never took effect
        p=Page(apply_after=3);logs=[]
        xc.ensure_focus(p,force=True,record=lambda l:logs.append(list(l)),wait_ms=0)
        self.assertEqual(p.sessions,3);self.assertTrue(p.made[0].detached and p.made[1].detached)
        self.assertEqual([a['vis'] for a in logs[-1]],['hidden','hidden','visible'])

    def test_session_creation_error_recorded_then_recovers(self):
        p=Page(fail_new=1);logs=[]
        xc.ensure_focus(p,force=True,record=lambda l:logs.append(list(l)),wait_ms=0)
        self.assertEqual(logs[-1][0]['error'],'RuntimeError');self.assertEqual(len(logs[-1]),2)

    def test_bounded_and_clear_error(self):
        p=Page(apply_after=99)
        with self.assertRaises(PublishError) as cm:xc.ensure_focus(p,force=True,attempts=3,wait_ms=0)
        self.assertIn('重试 3 次',str(cm.exception));self.assertEqual(p.sessions,3)

    def test_disabled_check_is_noop(self):
        self.assertIsNone(xc.ensure_focus(Page(apply_after=99)))


if __name__=='__main__':unittest.main()
