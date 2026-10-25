"""Background tab creation must not activate or claim unrelated browser pages."""
import contextlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from browser_runtime import background_tab
from publish_core import PublishError


class Page:
    def __init__(self, context, ident):
        self.context=context;self.ident=ident;self.closed=False
    def close(self):self.closed=True
    def bring_to_front(self):raise AssertionError('no activation')


class Context:
    def __init__(self, *, unrelated=False, error=False, private=True, window=False):
        self.calls=[];self.detaches=0;self.expecting=False;self.error=error
        self.private=private;self.window=window;self.anchor=Page(self,'original')
        self.created=Page(self,'unrelated' if unrelated else 'owned')
    def new_page(self):raise AssertionError('foreground new_page is forbidden')
    @contextlib.contextmanager
    def expect_page(self, **kwargs):
        self.expecting=True
        try:
            yield SimpleNamespace(value=self.created)
            if self.error:raise TimeoutError('event timeout after target creation')
        finally:self.expecting=False
    def new_cdp_session(self,page):
        owner=self
        class Session:
            def send(self,method,params=None):
                owner.calls.append((method,params))
                if method=='Target.getTargetInfo':
                    info={'targetId':page.ident}
                    if owner.private:info['browserContextId']='same-context'
                    return {'targetInfo':info}
                if method=='Target.createTarget':
                    assert owner.expecting
                    if owner.window:
                        assert params['newWindow'] is True and params['focus'] is False and params['background'] is False
                    else:assert params['background'] is True
                    return {'targetId':'owned'}
                if method=='Target.closeTarget':return {'success':True}
                raise AssertionError(method)
            def detach(self):owner.detaches+=1
        return Session()


class BackgroundTests(unittest.TestCase):
    def test_same_context_background_only_and_owned_cleanup(self):
        c=Context()
        with background_tab(c.anchor) as page:
            self.assertIs(page,c.created);self.assertFalse(page.closed)
        self.assertTrue(page.closed);self.assertFalse(c.anchor.closed)
        self.assertIn(('Target.createTarget',{'url':'about:blank','background':True,
            'browserContextId':'same-context'}),c.calls)
        self.assertEqual(c.detaches,2)

    def test_default_context_is_not_invented(self):
        c=Context(private=False)
        with background_tab(c.anchor):pass
        params=next(p for m,p in c.calls if m=='Target.createTarget')
        self.assertNotIn('browserContextId',params)

    def test_navigation_failure_closes_owned_only(self):
        c=Context()
        with self.assertRaisesRegex(RuntimeError,'navigation failed'):
            with background_tab(c.anchor):raise RuntimeError('navigation failed')
        self.assertTrue(c.created.closed);self.assertFalse(c.anchor.closed)
        self.assertEqual(c.detaches,2)

    def test_unrelated_popup_cannot_be_claimed_or_closed(self):
        c=Context(unrelated=True)
        with self.assertRaisesRegex(PublishError,'身份不符'):
            with background_tab(c.anchor):self.fail('must not yield')
        self.assertFalse(c.created.closed);self.assertFalse(c.anchor.closed)
        self.assertIn(('Target.closeTarget',{'targetId':'owned'}),c.calls)
        self.assertEqual(c.detaches,2)

    def test_page_event_timeout_closes_known_target_without_fallback(self):
        c=Context(error=True)
        with self.assertRaises(TimeoutError):
            with background_tab(c.anchor):self.fail('must not yield')
        self.assertFalse(c.created.closed)
        self.assertIn(('Target.closeTarget',{'targetId':'owned'}),c.calls)
        self.assertEqual(c.detaches,1)


if __name__=='__main__':unittest.main()
