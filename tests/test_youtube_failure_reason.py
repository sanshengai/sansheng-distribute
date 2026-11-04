import json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import safe_adapter_failure as f
E0='YouTube 适配器未完成；保持原任务并先核验'
def body(reason):return json.dumps({'ok':False,'error':E0,'reason':reason},ensure_ascii=False)
class T(unittest.TestCase):
    def test_fixed_reason_passes(self):
        self.assertEqual(f(body('YouTube 原上传会话无法确认')),'；YouTube 原因：YouTube 原上传会话无法确认')
        self.assertEqual(f(body('HTTPError')),'；YouTube 原因：HTTPError')
    def test_urls_tokens_long_or_multiline_dropped(self):
        for r in ('see https://x/upload?upload_id=1','bad token abc','x'*81,'a\nb','Bearer zzz','',None,3):
            with self.subTest(r=r):self.assertEqual(f(body(r)),'')
    def test_other_shapes_dropped(self):
        self.assertEqual(f(json.dumps({'ok':False,'error':'其它','reason':'HTTPError'})),'')
        self.assertEqual(f('not json'),'')
if __name__=='__main__':unittest.main()
