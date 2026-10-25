import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import browser_runtime as runtime
from publish_core import PublishError
from test_background_tab import Context

PROTOCOL={'domains':[{'domain':'Target','commands':[{'name':'createTarget','parameters':[{'name':'focus','type':'boolean'}]}]}]}


class BackgroundWindowTests(unittest.TestCase):
    def protocol(self,value):return patch.object(runtime,'urlopen',return_value=io.BytesIO(json.dumps(value).encode()))
    def test_supported_actual_protocol_creates_unfocused_owned_window(self):
        c=Context(window=True)
        with self.protocol(PROTOCOL) as fetch:
            with runtime.background_window(c.anchor,cdp_url='http://127.0.0.1:9333') as page:
                self.assertIs(page,c.created)
        fetch.assert_called_once_with('http://127.0.0.1:9333/json/protocol',timeout=5)
        self.assertTrue(c.created.closed);self.assertFalse(c.anchor.closed)
        params=next(p for m,p in c.calls if m=='Target.createTarget')
        self.assertEqual(params,dict(url='about:blank',newWindow=True,background=False,focus=False,browserContextId='same-context'))
    def test_missing_empty_or_wrong_protocol_creates_nothing(self):
        for value in ({},{'domains':[]},{'domains':[{'domain':'Target','commands':[]}]},
                      {'domains':[{'domain':'Target','commands':[{'name':'createTarget','parameters':[]}]}]}):
            c=Context(window=True)
            with self.protocol(value),self.assertRaises(PublishError):
                with runtime.background_window(c.anchor,cdp_url='http://127.0.0.1:9333'):self.fail('must not create')
            self.assertEqual(c.calls,[])
    def test_unreachable_protocol_never_falls_back_to_focused_window(self):
        c=Context(window=True)
        with patch.object(runtime,'urlopen',side_effect=TimeoutError()),self.assertRaises(PublishError):
            with runtime.background_window(c.anchor,cdp_url='http://127.0.0.1:9333'):self.fail('must not create')
        self.assertEqual(c.calls,[])
    def test_invalid_endpoint_is_rejected_before_fetch_or_create(self):
        c=Context(window=True)
        with patch.object(runtime,'urlopen') as fetch,self.assertRaises(PublishError):
            with runtime.background_window(c.anchor,cdp_url='https://remote.example'):self.fail('must not create')
        fetch.assert_not_called();self.assertEqual(c.calls,[])
    def test_unrelated_popup_remains_open_and_only_owned_target_is_closed(self):
        c=Context(window=True,unrelated=True)
        with self.protocol(PROTOCOL),self.assertRaises(PublishError):
            with runtime.background_window(c.anchor,cdp_url='http://127.0.0.1:9333'):self.fail('must not claim')
        self.assertFalse(c.created.closed);self.assertFalse(c.anchor.closed)
        self.assertIn(('Target.closeTarget',{'targetId':'owned'}),c.calls)
    def test_navigation_failure_closes_only_owned_window(self):
        c=Context(window=True)
        with self.protocol(PROTOCOL),self.assertRaises(RuntimeError):
            with runtime.background_window(c.anchor,cdp_url='http://127.0.0.1:9333'):raise RuntimeError('navigation failed')
        self.assertTrue(c.created.closed);self.assertFalse(c.anchor.closed)


if __name__=='__main__':unittest.main()
