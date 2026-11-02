"""视频号无远端 ID 的未知提交：只能用明确候选 recover-wechat_channels 绑定。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError, Store, digest, run, write_json
from platform_pack import build_pack
from social_adapter import dispatch, work_path
from test_platform_pack import fixture

RID = 'export/FixtureAAtestCandidate01'


class ChannelsUnknownRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.m = fixture(self.root, 'wechat_channels', 'video')
        self.m['targets'][0].update(endpoint='publish', title='标题', description='简介', cover_asset_id='image')
        self.path = self.root / 'manifest.json'; write_json(self.path, self.m)
        self.store = Store(self.root / 'state'); self.addCleanup(self.store.close)
        self.key = self.store.prepare(self.m, 't')['key']
        self.store.authorize(self.m, 't', '测试夹具授权')
        self.store.begin(self.m, 't')  # executor started, lost before any ID was saved
        (self.store.directory / 'snapshots').mkdir(exist_ok=True)
        write_json(self.store.directory / 'snapshots' / f'{self.key}.json', self.m)

    def obs(self, **extra):
        value = dict(manifest_digest=digest(self.m), target_id='t', platform='wechat_channels', account='owner',
                     state='published', remote_id=RID, remote_url='https://weixin.qq.com/sph/Atest',
                     content_verified=True, recovery_verified=True)
        value.update(extra); return value

    def go(self, observation, **kwargs):
        with patch('publish_core.adapter_config', return_value={'verify': ['x']}), \
                patch('publish_core.invoke', return_value=observation) as call:
            return run(self.path, 't', {}, self.store, resume=True, **kwargs), call

    def test_plain_resume_cannot_adopt_a_listed_post(self):
        with self.assertRaisesRegex(PublishError, 'recover-wechat_channels'):
            self.go(self.obs())
        job = self.store.status(self.key)
        self.assertEqual((job['state'], job['remote_id']), ('submission_unknown', None))

    def test_explicit_candidate_binds_and_releases_lock(self):
        result, _ = self.go(self.obs(), recover_remote_id=RID, recovery_platform='wechat_channels')
        self.assertEqual((result['state'], result['remote_id']), ('published', RID))
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM account_locks').fetchone())

    def test_candidate_mismatch_or_unverified_keeps_unknown(self):
        for change in ({'remote_id': 'export/FixtureAAotherCandidate'}, {'recovery_verified': False}):
            with self.subTest(change=change), self.assertRaises(PublishError):
                self.go(self.obs(**change), recover_remote_id=RID, recovery_platform='wechat_channels')
        with self.assertRaises(PublishError):
            self.go(self.obs(), recover_remote_id=RID, recovery_platform='x')
        self.assertEqual(self.store.status(self.key)['state'], 'submission_unknown')


class ChannelsAdapterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.m = fixture(self.root, 'wechat_channels', 'video'); self.m['targets'][0]['endpoint'] = 'publish'
        mp = self.root / 'manifest.json'; write_json(mp, self.m)
        self.profile = {'accounts': {'owner': {'platforms': ['wechat_channels'], 'public_identities': {'wechat_channels': 'sphTest'}}},
                        'browser_profile': str((self.root / 'dedicated').resolve())}
        self.pack = build_pack(self.m, 't', self.profile)
        self.request = self.root / 'request.json'
        write_json(self.request, dict(manifest_path=str(mp), manifest_digest=digest(self.m), target=self.m['targets'][0],
                                      job={}, recovery_remote_id=RID))
        write_json(work_path(self.request), dict(pack=self.pack, pack_digest=digest(self.pack),
                                                 expected_account_identity='sphTest', browser_profile=self.profile['browser_profile']))

    def obs(self, **extra):
        value = dict(manifest_digest=digest(self.m), target_id='t', platform='wechat_channels', account='owner',
                     observed_account_identity='sphTest', state='published', remote_id=RID, content_verified=True,
                     verified_pack_digest=digest(self.pack), verified_asset_ids=['video', 'image', 'subtitle'],
                     recovery_verified=True, source_md5_match=True)
        value.update(extra); return value

    def test_recovery_needs_same_id_and_source_md5(self):
        with patch('social_adapter.agent_adapter.dispatch', return_value=self.obs()):
            self.assertEqual(dispatch(self.request, 'verify', self.profile)['remote_id'], RID)
        for change in ({'remote_id': 'export/FixtureAAotherCandidate'}, {'source_md5_match': False}):
            with self.subTest(change=change), patch('social_adapter.agent_adapter.dispatch', return_value=self.obs(**change)), \
                    self.assertRaises(PublishError):
                dispatch(self.request, 'verify', self.profile)


if __name__ == '__main__':
    unittest.main()
