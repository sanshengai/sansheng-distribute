import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import publish


class MediaInspectCliTests(unittest.TestCase):
    def test_read_only_command_passes_manifest_and_target_without_store(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'manifest.json'
            path.write_text('{"example": true}', encoding='utf-8')
            out = io.StringIO()
            with patch.object(sys, 'argv', ['publish.py', 'media-inspect', str(path), '--target', 'sample']), \
                    patch('media_inspect.inspect', return_value={'local_only': True}) as inspect, \
                    patch.object(publish, 'Store') as store, redirect_stdout(out):
                result = publish.main()
            self.assertEqual(result, 0)
            inspect.assert_called_once_with({'example': True}, 'sample')
            store.assert_not_called()
            self.assertTrue(json.loads(out.getvalue())['result']['local_only'])


if __name__ == '__main__':
    unittest.main()
