"""Offline Alipay video handoff contract; no browser or platform is contacted."""
from pathlib import Path
import copy
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from agent_adapter import check_reply
from platform_pack import build_pack
from publish_core import PublishError, digest, file_hash, read_json, validate, write_json
from social_adapter import dispatch, preflight_social, work_path


class AlipayHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'movie.mp4').write_bytes(b'video fixture')
        (self.root / 'cover.png').write_bytes(b'cover fixture')
        self.manifest = {'schema_version': 1, 'job_id': 'synthetic', 'producer': 'test',
                         'content_status': 'final', 'source_root': str(self.root),
                         'documents': [], 'media_slots': [],
                         'assets': [{'id': 'movie', 'path': 'movie.mp4', 'kind': 'video'},
                                    {'id': 'cover', 'path': 'cover.png', 'kind': 'image'}],
                         'targets': [{'id': 'clip', 'platform': 'alipay', 'account': 'owner',
                                      'endpoint': 'publish', 'content_type': 'video',
                                      'title': 'Sample', 'text': 'Description',
                                      'asset_ids': ['movie', 'cover'], 'cover_asset_id': 'cover',
                                      'platform_metadata': {'collection_name': 'Example collection',
                                                            'ai_generated': True}}]}
        validate(self.manifest, sealing=True)
        self.source = self.root / 'manifest.json'
        write_json(self.source, self.manifest)
        self.request = self.root / 'request.json'
        self.req = {'manifest_path': str(self.source), 'manifest_digest': digest(self.manifest),
                    'target': self.manifest['targets'][0], 'job': {}}
        write_json(self.request, self.req)
        self.profile = {'browser_profile': str((self.root / 'dedicated').resolve()),
                        'accounts': {'owner': {'platforms': ['alipay'],
                                               'public_identities': {'alipay': 'life-account-fixture'}}}}
        self.pack = build_pack(self.manifest, 'clip', self.profile)
        self.observation = {'manifest_digest': digest(self.manifest), 'target_id': 'clip',
                            'platform': 'alipay', 'account': 'owner', 'state': 'pending',
                            'observed_account_identity': 'life-account-fixture',
                            'remote_id': 'synthetic-remote-identity'}
        self.evidence = self.root / 'evidence.txt'
        self.evidence.write_text('synthetic only')

    def agent_reply(self, request_path, action, **unused):
        observation = dict(self.observation)
        if action == 'verify':
            observation.update(state='published', content_verified=True,
                               verified_pack_digest=digest(self.pack),
                               verified_asset_ids=['movie', 'cover'])
        task = {'nonce': 'test-' + action, 'action': action, 'request': self.req}
        envelope = {'nonce': task['nonce'], 'observed_phase': task['nonce'],
                    'observation': observation,
                    'evidence': [{'path': str(self.evidence), 'sha256': file_hash(self.evidence)}]}
        return check_reply(task, envelope)

    def test_work_order_retains_full_alipay_package_for_agent(self):
        profile_path = self.root / 'profile.json'
        write_json(profile_path, self.profile)
        argv = ['python3', 'social_adapter.py', 'execute', '--profile', str(profile_path)]
        self.assertTrue(preflight_social(self.manifest, self.manifest['targets'][0], self.profile, argv)['local_only'])
        with patch('social_adapter.agent_adapter.dispatch', side_effect=self.agent_reply):
            self.assertEqual(dispatch(self.request, 'execute', self.profile)['state'], 'pending')
            work = read_json(work_path(self.request))
            self.assertEqual(work['expected_account_identity'], 'life-account-fixture')
            self.assertEqual(work['pack']['cover_asset_id'], 'cover')
            self.assertEqual(work['pack']['platform_metadata'], {'collection_name': 'Example collection', 'ai_generated': True})
            self.assertEqual(dispatch(self.request, 'verify', self.profile)['state'], 'published')
        # The synthetic verify result exercises the contract only; it is not real platform evidence.

    def test_wrong_account_and_missing_cover_rejected(self):
        with patch('social_adapter.agent_adapter.dispatch', side_effect=self.agent_reply):
            dispatch(self.request, 'execute', self.profile)
        for change in ({'observed_account_identity': 'other'}, {'verified_asset_ids': ['movie']},
                       {'verified_pack_digest': 'incorrect'}, {'remote_id': None}):
            observation = {**self.observation, 'state': 'published', 'content_verified': True,
                           'verified_pack_digest': digest(self.pack),
                           'verified_asset_ids': ['movie', 'cover'], **change}
            with self.subTest(change=change), patch('social_adapter.agent_adapter.dispatch', return_value=observation):
                with self.assertRaises(PublishError):
                    dispatch(self.request, 'verify', self.profile)

    def test_execute_may_not_claim_published_and_verify_cannot_switch_identity(self):
        premature = {**self.observation, 'state': 'published'}
        with patch('social_adapter.agent_adapter.dispatch', return_value=premature):
            with self.assertRaisesRegex(PublishError, '执行不能替代独立读回'):
                dispatch(self.request, 'execute', self.profile)
        with patch('social_adapter.agent_adapter.dispatch', side_effect=self.agent_reply):
            dispatch(self.request, 'execute', self.profile)
        changed = copy.deepcopy(self.profile)
        changed['accounts']['owner']['public_identities']['alipay'] = 'other-life-account'
        with patch('social_adapter.agent_adapter.dispatch') as agent:
            with self.assertRaisesRegex(PublishError, '禁止切换账号'):
                dispatch(self.request, 'verify', changed)
            agent.assert_not_called()


if __name__ == '__main__':
    unittest.main()
