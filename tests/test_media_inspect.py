import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError
import media_inspect


class MediaInspectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'a; touch injected.mp4'
        self.path.write_bytes(b'fixture video')
        self.manifest = {
            'schema_version': 1, 'job_id': 'demo', 'producer': 'test', 'content_status': 'final',
            'source_root': self.temp.name, 'documents': [], 'media_slots': [],
            'assets': [{'id': 'video', 'path': self.path.name, 'kind': 'video',
                        'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest()}],
            'targets': [{'id': 'one', 'platform': 'x', 'account': 'owner', 'endpoint': 'prefill',
                         'content_type': 'video', 'title': 'Demo', 'asset_ids': ['video']}],
        }
        self.data = {'streams': [{'codec_type': 'video', 'width': 1920, 'height': 1080}],
                     'format': {'duration': '2.5', 'size': str(self.path.stat().st_size), 'format_name': 'mov,mp4'}}

    def invoke(self, data=None):
        result = subprocess.CompletedProcess([], 0, json.dumps(self.data if data is None else data), '')
        with patch.object(media_inspect.subprocess, 'run', return_value=result) as run:
            value = media_inspect.inspect(self.manifest, 'one')
        return value, run

    def test_facts_and_non_network_process(self):
        value, run = self.invoke()
        self.assertEqual((value['encoded_width'], value['encoded_height']), (1920, 1080))
        self.assertEqual((value['display_width'], value['display_height']), (1920, 1080))
        self.assertEqual(value['duration_seconds'], 2.5)
        self.assertEqual(value['size_bytes'], self.path.stat().st_size)
        self.assertTrue(value['local_only'])
        self.assertFalse(value['platform_rules_verified'])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file,pipe')
        self.assertEqual(command[-1], str(self.path.resolve()))
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)

    def test_rotation_and_vertical(self):
        self.data['streams'][0]['side_data_list'] = [{'rotation': -90}]
        value, _ = self.invoke()
        self.assertEqual(value['rotation_degrees'], 270)
        self.assertEqual((value['display_width'], value['display_height']), (1080, 1920))
        self.data['streams'][0] = {'codec_type': 'video', 'width': 720, 'height': 1280}
        value, _ = self.invoke()
        self.assertEqual((value['display_width'], value['display_height']), (720, 1280))

    def test_bad_metadata_and_ambiguous_rotation_rejected(self):
        cases = [
            {'streams': [], 'format': self.data['format']},
            {'streams': self.data['streams'] * 2, 'format': self.data['format']},
            {'streams': [{'codec_type': 'video', 'width': True, 'height': 10}], 'format': self.data['format']},
            {'streams': self.data['streams'], 'format': dict(self.data['format'], duration='NaN')},
            {'streams': self.data['streams'], 'format': dict(self.data['format'], size='1')},
            {'streams': [dict(self.data['streams'][0], tags={'rotate': '45'})], 'format': self.data['format']},
            {'streams': [dict(self.data['streams'][0], tags={'rotate': '90'},
                              side_data_list=[{'rotation': 180}])], 'format': self.data['format']},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(PublishError):
                self.invoke(data)

    def test_source_change_before_or_during_probe_rejected(self):
        self.path.write_bytes(b'changed')
        with patch.object(media_inspect.subprocess, 'run') as run, self.assertRaises(PublishError):
            media_inspect.inspect(self.manifest, 'one')
        run.assert_not_called()
        self.path.write_bytes(b'fixture video')
        def mutate(*args, **kwargs):
            self.path.write_bytes(b'changed video')  # same length; size alone must not pass
            return subprocess.CompletedProcess([], 0, json.dumps(self.data), '')
        with patch.object(media_inspect.subprocess, 'run', side_effect=mutate), self.assertRaises(PublishError):
            media_inspect.inspect(self.manifest, 'one')

    def test_missing_ffprobe_timeout_and_invalid_file(self):
        errors = [FileNotFoundError(), subprocess.TimeoutExpired('ffprobe', 30)]
        for error in errors:
            with self.subTest(error=error), patch.object(media_inspect.subprocess, 'run', side_effect=error), self.assertRaises(PublishError):
                media_inspect.inspect(self.manifest, 'one')
        for result in (subprocess.CompletedProcess([], 1, '', 'private-path'),
                       subprocess.CompletedProcess([], 0, 'not-json', '')):
            with patch.object(media_inspect.subprocess, 'run', return_value=result), self.assertRaises(PublishError) as err:
                media_inspect.inspect(self.manifest, 'one')
            self.assertNotIn('private-path', str(err.exception))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe unavailable')
    def test_real_ffprobe_roundtrip(self):
        p = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=64x96:d=0.2',
                            '-c:v', 'mpeg4', '-y', str(self.path)], capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr.decode(errors='replace'))
        self.manifest['assets'][0]['sha256'] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        value = media_inspect.inspect(self.manifest, 'one')
        self.assertEqual((value['display_width'], value['display_height']), (64, 96))
        self.assertGreater(value['duration_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
