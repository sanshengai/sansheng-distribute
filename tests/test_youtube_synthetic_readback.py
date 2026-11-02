"""YouTube containsSyntheticMedia 读回：false 读回为 null/缺失时，仅在上传请求确实声明 false 时视为「否」。离线。"""
import json
import unittest
import sys, pathlib
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import test_youtube_adapter as base
from publish_core import PublishError, digest, validate
from youtube_adapter import checkpoint_path, synthetic_readback_ok


class SyntheticReadbackTest(unittest.TestCase):
    setUp=base.YoutubeAdapterTest.setUp
    run_adapter=base.YoutubeAdapterTest.run_adapter
    def set_altered(self, value):
        t=self.manifest['targets'][0]; t['platform_metadata']['altered_content']=value
        validate(self.manifest,sealing=True); self.mpath.write_text(json.dumps(self.manifest))
        self.request.update(manifest_digest=digest(self.manifest),target=t); self.rpath.write_text(json.dumps(self.request))
    def platform_returns(self, mode):
        orig=self.fake.call
        def call(method,url,**kw):
            code,h,b=orig(method,url,**kw)
            if method=='GET' and '/videos?' in url:
                d=json.loads(b); st=d['items'][0]['status']
                if mode=='null': st['containsSyntheticMedia']=None
                elif mode=='missing': st.pop('containsSyntheticMedia',None)
                else: st['containsSyntheticMedia']=mode
                b=json.dumps(d).encode()
            return code,h,b
        self.fake.call=call
    def test_false_with_null_or_missing_readback_passes(self):
        for mode in ('null','missing'):
            with self.subTest(mode=mode):
                self.setUp(); self.set_altered(False); self.run_adapter('execute')
                self.assertIs(self.fake.resource['status']['containsSyntheticMedia'],False)
                self.platform_returns(mode); self.assertEqual(self.run_adapter('verify')['state'],'published')
    def test_false_with_true_readback_fails(self):
        self.set_altered(False); self.run_adapter('execute'); self.platform_returns(True)
        with self.assertRaises(PublishError): self.run_adapter('verify')
    def test_true_with_null_readback_fails(self):
        self.set_altered(True); self.run_adapter('execute'); self.platform_returns('null')
        with self.assertRaises(PublishError): self.run_adapter('verify')
    def test_true_with_true_readback_passes(self):
        self.set_altered(True); self.run_adapter('execute'); self.platform_returns(True)
        self.assertEqual(self.run_adapter('verify')['state'],'published')
    def test_false_not_in_sent_request_fails(self):
        # 原请求未声明 false（按无 altered_content 的清单上传），之后清单改为 false：读回 null 不能当作已声明
        t=self.manifest['targets'][0]; t['platform_metadata'].pop('altered_content')
        validate(self.manifest,sealing=True); self.mpath.write_text(json.dumps(self.manifest))
        self.request.update(manifest_digest=digest(self.manifest),target=t); self.rpath.write_text(json.dumps(self.request))
        self.run_adapter('execute'); self.assertNotIn('containsSyntheticMedia',self.fake.resource['status'])
        self.set_altered(False); self.platform_returns('null')
        with self.assertRaises(PublishError): self.run_adapter('verify')
        # 篡改检查点中的请求摘要同样拒绝
        self.setUp(); self.set_altered(False); self.run_adapter('execute')
        cp=checkpoint_path(self.rpath); d=json.loads(cp.read_text()); d['binding']['resource_digest']='0'*64
        cp.write_text(json.dumps(d)); self.platform_returns('null')
        with self.assertRaises(PublishError): self.run_adapter('verify')
    def test_helper_truth_table(self):
        self.assertTrue(synthetic_readback_ok(False,None,True))
        self.assertTrue(synthetic_readback_ok(False,False,True))
        self.assertFalse(synthetic_readback_ok(False,True,True))
        self.assertFalse(synthetic_readback_ok(True,None,True))
        self.assertTrue(synthetic_readback_ok(True,True,True))
        self.assertFalse(synthetic_readback_ok(False,None,False))
        self.assertFalse(synthetic_readback_ok(True,True,False))
        self.assertFalse(synthetic_readback_ok(None,None,True))

if __name__=='__main__': unittest.main()
