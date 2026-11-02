"""X hashtag typeahead leftover mask over the send button (S4E01 2026-11-02 root cause)."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import x_publish_browser as xb
from publish_core import PublishError


class Loc:
    def __init__(self,page,kind):self.page,self.kind=page,kind
    def count(self):
        if self.kind=='popup':return 1 if self.page.popup else 0
        return 1
    def evaluate(self,js):
        assert js is xb.STALE_MASK
        return self.page.mask


class Keyboard:
    def __init__(self,page):self.page=page
    def press(self,key):
        assert key=='Escape';self.page.presses+=1
        if self.page.closes>0:
            self.page.closes-=1;self.page.popup=False;self.page.mask=False


class Page:
    def __init__(self,popup=False,mask=False,closes=1):
        self.popup,self.mask,self.closes,self.presses=popup,mask,closes,0
        self.keyboard=Keyboard(self)
    def locator(self,sel):return Loc(self,'popup' if 'listbox' in sel else 'button')
    def wait_for_timeout(self,ms):pass


class MaskTests(unittest.TestCase):
    def test_clean_page_no_escape(self):
        p=Page();xb.dismiss_topic_suggestions(p);self.assertEqual(p.presses,0)

    def test_listbox_closed(self):
        p=Page(popup=True);xb.dismiss_topic_suggestions(p);self.assertEqual(p.presses,1)

    def test_leftover_empty_mask_closed(self):
        # the observed failure: no listbox, only the transparent layer over the button
        p=Page(mask=True);xb.dismiss_topic_suggestions(p);self.assertEqual(p.presses,1)

    def test_persistent_mask_refuses_without_click(self):
        p=Page(mask=True,closes=0)
        with self.assertRaises(PublishError):xb.dismiss_topic_suggestions(p)
        self.assertEqual(p.presses,6)

    def test_mask_that_returns_after_escape_is_closed_again(self):
        # S4E04: Escape closed it, then X re-opened it about a second later
        p=Page(mask=True,closes=3);n={'w':0}
        def wait(ms):
            n['w']+=1
            if n['w'] in (1,3):p.mask=True   # reappears after the 1st and 2nd Escape
        p.wait_for_timeout=wait
        self.assertEqual(xb.dismiss_topic_suggestions(p),3);self.assertFalse(p.mask)

    def test_mask_js_is_narrow(self):
        js=xb.STALE_MASK
        for need in ("#layers","[role=\"dialog\"]","children.length===0","position==='fixed'","node.contains(hit)"):
            self.assertIn(need,js)


class MaskDomTests(unittest.TestCase):
    """Real DOM check of STALE_MASK when a local Chromium is available."""
    def test_dom(self):
        try:
            from playwright.sync_api import sync_playwright
            pw=sync_playwright().start();browser=pw.chromium.launch()
        except Exception as exc:
            self.skipTest(f'no headless chromium: {type(exc).__name__}')
        try:
            page=browser.new_page()
            base='''<div id=layers><div role=dialog aria-modal=true><button data-testid=tweetButton
              style="position:absolute;left:10px;top:10px;width:60px;height:30px">Post</button></div>%s</div>'''
            cases={'':False,
                   '<div style="position:fixed;inset:0"></div>':True,
                   '<div style="position:fixed;inset:0">menu</div>':False,
                   '<div style="position:fixed;inset:0"><span></span></div>':False}
            for extra,want in cases.items():
                page.set_content(base%extra)
                got=page.locator(xb.SEND_BUTTON).evaluate(xb.STALE_MASK)
                self.assertEqual(got,want,extra)
        finally:
            browser.close();pw.stop()


if __name__=='__main__':unittest.main()
