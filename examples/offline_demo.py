#!/usr/bin/env python3
"""Exercise the real batch CLI against a temporary local simulator only.

No accounts, network requests, browser, private configuration or third-party
packages are used. The simulator loses one creation response, then confirms
that same simulated draft on resume. All temporary files are removed on exit.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


SKILL_ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = r'''
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
request = json.loads(Path(sys.argv[-1]).read_text(encoding='utf-8'))
target = request['target']
action = sys.argv[1]
with (root / 'calls.jsonl').open('a', encoding='utf-8') as stream:
    stream.write(json.dumps({'target': target['id'], 'action': action}) + '\n')
draft = root / (target['id'] + '.simulated-draft.json')
if action == 'execute':
    if draft.exists():
        raise SystemExit('Duplicate creation rejected by offline simulator')
    draft.write_text(json.dumps({'text': target['text']}), encoding='utf-8')
    if target['id'] == 'delayed-response':
        # Simulate a lost response after a local draft has already been created.
        raise SystemExit(7)
elif action == 'verify':
    if not draft.is_file() or json.loads(draft.read_text())['text'] != target['text']:
        raise SystemExit('Simulated draft missing or changed')
else:
    raise SystemExit('Unknown simulator operation')
print(json.dumps({
    'manifest_digest': request['manifest_digest'], 'target_id': target['id'],
    'platform': target['platform'], 'account': target['account'],
    'remote_id': 'simulated-' + target['id'],
    'state': 'pending' if action == 'execute' else 'draft_ready',
    'content_verified': action == 'verify'
}))
'''


def main():
    with tempfile.TemporaryDirectory(prefix='sansheng-distribute-offline-') as temporary:
        root = Path(temporary)
        (root / 'source.txt').write_text('这是离线演示定稿；不会发送到真实平台。', encoding='utf-8')
        simulator = root / 'simulator.py'
        simulator.write_text(SIMULATOR, encoding='utf-8')
        spec = {
            'schema_version': 1, 'job_id': 'offline-demo', 'producer': 'offline-demo',
            'content_status': 'final', 'source_root': str(root),
            'documents': [{'id': 'source', 'path': 'source.txt', 'format': 'text'}],
            'assets': [], 'media_slots': [],
            'targets': [
                {'id': 'delayed-response', 'platform': 'x', 'account': 'simulated-account',
                 'endpoint': 'draft', 'content_type': 'text', 'document_id': 'source',
                 'text': '这是离线演示定稿；不会发送到真实平台。'},
                {'id': 'normal-response', 'platform': 'weibo', 'account': 'simulated-account',
                 'endpoint': 'draft', 'content_type': 'text', 'document_id': 'source',
                 'text': '这是离线演示定稿；不会发送到真实平台。'},
            ],
        }
        profile = {
            'accounts': {'simulated-account': {'platforms': ['x', 'weibo']}},
            'adapters': {
                platform: {
                    'enabled_capabilities': ['text:draft'],
                    'execute': [sys.executable, str(simulator), 'execute'],
                    'verify': [sys.executable, str(simulator), 'verify'],
                } for platform in ('x', 'weibo')
            },
        }
        for name, value in (('spec.json', spec), ('profile.json', profile)):
            (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('SANSHENG_DISTRIBUTE_', 'SANDY_PUBLISH_', 'WECHAT_'))}
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        commands = []

        def cli(action, *arguments):
            command = [sys.executable, str(SKILL_ROOT / 'scripts/publish.py'),
                       '--profile', str(root / 'profile.json'), '--state-dir', str(root / 'state'),
                       action, *map(str, arguments)]
            result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
            if result.returncode:
                raise RuntimeError(f'Offline CLI step failed: {action}; exit={result.returncode}')
            envelope = json.loads(result.stdout)
            if not envelope.get('ok'):
                raise RuntimeError(f'Offline CLI step did not pass: {action}')
            commands.append(action)
            return envelope['result']

        manifest = root / 'manifest.json'
        cli('seal', root / 'spec.json', '--output', manifest)
        prepared = cli('batch-prepare', manifest)
        campaign_id = prepared['campaign_id']
        cli('batch-authorize', campaign_id, '--statement',
            '离线演示：允许本地模拟器为两个合成目标创建并查询模拟草稿；禁止真实平台操作。')
        first = cli('batch-run', campaign_id)
        resumed = cli('batch-resume', campaign_id)
        final = cli('batch-status', campaign_id)
        calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
        creations = Counter(call['target'] for call in calls if call['action'] == 'execute')
        expected_first = {'delayed-response': 'submission_unknown', 'normal-response': 'draft_ready'}
        if {row['target_id']: row['state'] for row in first['targets']} != expected_first:
            raise RuntimeError('Offline demonstration did not exercise the expected partial-failure path')
        if not final['all_endpoints_reached'] or any(creations[target] != 1 for target in expected_first):
            raise RuntimeError('Offline recovery failed or attempted duplicate creation')

        def public_report(report):
            # Whitelist actual CLI fields. Do not print temporary paths or runtime configuration.
            return {
                'targets': [{key: row.get(key) for key in ('target_id', 'platform', 'endpoint', 'state', 'next_action')}
                            for row in report['targets']],
                'counts': report['counts'],
                'all_endpoints_reached': report['all_endpoints_reached'],
                'outcomes': report.get('outcomes', {}),
            }

        output = {
            'mode': 'offline_simulation',
            'notice': '真实 CLI + 本地模拟器；非真实平台发布证据。',
            'commands': commands,
            'first_run': public_report(first),
            'readonly_resume': public_report(resumed),
            'simulated_creation_calls': dict(creations),
            'adapter_calls': calls,
            'duplicate_creation': False,
            'temporary_files_removed_on_exit': True,
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
