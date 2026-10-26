"""Actual child-process failures keep actionable codes without exposing secrets."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import PublishError, invoke, safe_adapter_failure


class AdapterDiagnosticsTests(unittest.TestCase):
    def test_real_failed_child_exposes_code_but_no_adapter_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root/'adapter.py'
            child.write_text('import json,sys\nprint(json.dumps(dict(error_type="WechatError",'
                'errcode=40007, detail="PRIVATE-TOKEN https://private.invalid/?secret=PRIVATE")),file=sys.stderr)\n'
                'sys.exit(2)\n')
            with (root/'lock').open('w') as lock:
                with self.assertRaises(PublishError) as caught:
                    invoke({'verify':[sys.executable,str(child)]}, 'verify', root/'request.json', lock_fd=lock.fileno())
            text = str(caught.exception)
            self.assertIn('errcode=40007', text)
            self.assertIn('不自动新建', text)
            self.assertNotIn('PRIVATE', text)
            self.assertNotIn('private.invalid', text)

    def test_empty_unstructured_unknown_or_forged_codes_are_not_echoed(self):
        for value in ('', 'PRIVATE', '{}', '[]', 'x'*16385,
                      json.dumps(dict(error_type='PRIVATE',errcode=40007)),
                      json.dumps(dict(error_type='WechatError',errcode='40007')),
                      json.dumps(dict(error_type='WechatError',errcode=123456))):
            with self.subTest(value=value[:40]): self.assertEqual(safe_adapter_failure(value), '')


if __name__ == '__main__': unittest.main()
