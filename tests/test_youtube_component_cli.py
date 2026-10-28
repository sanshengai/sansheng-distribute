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


class ComponentCliTests(unittest.TestCase):
    def run_cli(self, argv, store, profile):
        out = io.StringIO()
        with patch.object(sys, 'argv', ['publish.py', *argv]), patch.object(publish, 'Store', return_value=store), \
                patch.object(publish, 'load_profile', return_value=profile), redirect_stdout(out):
            code = publish.main()
        self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())['result']

    def test_prepare_reads_spec_and_calls_store(self):
        from unittest.mock import MagicMock
        store = MagicMock()
        store.prepare_youtube_component.return_value = {'key': 'component'}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'spec.json'
            path.write_text('{"cover": null, "playlist_id": "example", "playlist_position": null}', encoding='utf-8')
            profile = {'example': True}
            result = self.run_cli(['--state-dir', temp, 'youtube-component-prepare', 'parent', '--spec', str(path)], store, profile)
        self.assertEqual(result['key'], 'component')
        store.prepare_youtube_component.assert_called_once_with('parent',
            {'cover': None, 'playlist_id': 'example', 'playlist_position': None}, profile)

    def test_verification_never_calls_writing_route(self):
        from unittest.mock import MagicMock
        store = MagicMock()
        with tempfile.TemporaryDirectory() as temp, \
                patch('publish_core.run_youtube_component', return_value={'state': 'unknown'}) as run:
            profile = {'example': True}
            self.run_cli(['--state-dir', temp, 'youtube-component-verify', 'key', '--part', 'cover'], store, profile)
        run.assert_called_once_with(store, 'key', 'cover', profile, verify_only=True)

    def test_bundle_continues_the_same_upload_only_on_explicit_command(self):
        from unittest.mock import MagicMock
        store = MagicMock()
        with tempfile.TemporaryDirectory() as temp, \
                patch('publish_core.run_youtube_bundle', return_value={'overall_state': 'pending'}) as run:
            profile = {'example': True}
            self.run_cli(['--state-dir', temp, 'youtube-bundle-continue', 'key'], store, profile)
        run.assert_called_once_with(store, 'key', profile, continue_upload=True)


if __name__ == '__main__':
    unittest.main()
