import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from browser_runtime import render_background_page
from publish_core import PublishError

class Page:
    def __init__(self,states):self.states=iter(states);self.context=self;self.calls=[];self.detached=False
    def evaluate(self,script):return next(self.states)
    def new_cdp_session(self,page):return self
    def send(self,method,args):
        if method!='Page.captureScreenshot':raise AssertionError('must not activate a tab or browser')
        self.calls.append((method,args));return {'data':'discarded'}
    def detach(self):self.detached=True

class BackgroundRenderTests(unittest.TestCase):
    def test_already_rendered_page_needs_no_capture(self):
        p=Page([dict(inner=828,outer=949,hidden=True)]);render_background_page(p);self.assertEqual(p.calls,[])
    def test_zero_outer_background_viewport_is_realized_without_focus(self):
        p=Page([dict(inner=828,outer=0,hidden=True),dict(inner=828,outer=828,hidden=True)])
        self.assertIsNone(render_background_page(p));self.assertEqual(len(p.calls),1);self.assertTrue(p.detached)
        self.assertEqual(p.calls[0][1]['captureBeyondViewport'],False)
        # Native X acceptance: PNG realized the fresh surface; the attempted
        # low-quality JPEG did not reliably produce a visible editor.
        self.assertEqual(p.calls[0][1]['format'],'png')
    def test_still_unrendered_stops_after_one_native_capture(self):
        p=Page([dict(inner=828,outer=0,hidden=True)]*2)
        with self.assertRaises(PublishError):render_background_page(p)
        self.assertEqual(len(p.calls),1);self.assertTrue(p.detached)
    def test_empty_or_foreground_invalid_viewport_is_not_repaired_by_focusing(self):
        for state in (dict(inner=0,outer=0,hidden=True),dict(inner=828,outer=0,hidden=False)):
            p=Page([state])
            with self.assertRaises(PublishError):render_background_page(p)
            self.assertEqual(p.calls,[])

if __name__=='__main__':unittest.main()
