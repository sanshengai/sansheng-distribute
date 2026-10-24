import copy
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_core import PublishError, adapter_config, digest


class FirstPublicationTests(unittest.TestCase):
    def setUp(self):
        self.target = dict(id='episode-one', platform='douyin', account='editor', content_type='video', endpoint='publish')
        self.manifest = dict(targets=[self.target], assets=[])
        self.entry = dict(manifest_digest=digest(self.manifest), **{k:self.target[k] for k in ('platform','account','content_type','endpoint')}, target_id=self.target['id'], acknowledge_publication=True,
                          expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        self.profile = dict(accounts={'editor':dict(platforms=['douyin'])}, adapters={'douyin':dict(enabled_capabilities=[],execute=['adapter'],verify=['verifier'])}, first_publication_runs=[self.entry])

    def check(self, profile=None, target=None, manifest=None):
        return adapter_config(profile or self.profile, target or self.target, manifest=manifest or self.manifest)

    def test_exact_scope_works_without_global_enable(self):
        self.assertEqual(self.check()['enabled_capabilities'], [])

    def test_empty_and_missing_scope_refused(self):
        for entries in ([], [{}]):
            p=copy.deepcopy(self.profile);p['first_publication_runs']=entries
            with self.subTest(entries=entries), self.assertRaises(PublishError): self.check(profile=p)

    def test_each_binding_and_acknowledgment_required(self):
        for key, value in dict(manifest_digest='0'*64,target_id='other',platform='bilibili',account='other',content_type='text',endpoint='draft',acknowledge_publication=False).items():
            p=copy.deepcopy(self.profile);p['first_publication_runs'][0][key]=value
            with self.subTest(key=key), self.assertRaises(PublishError): self.check(profile=p)

    def test_expired_naive_far_future_and_bad_time_refused(self):
        for value in ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), (datetime.now(timezone.utc)+timedelta(days=2)).isoformat(),datetime.now().isoformat(),'tomorrow',None):
            p=copy.deepcopy(self.profile);p['first_publication_runs'][0]['expires_at']=value
            with self.subTest(value=value), self.assertRaises(PublishError): self.check(profile=p)

    def test_changed_content_and_unknown_scope_fields_refused(self):
        m=copy.deepcopy(self.manifest);m['title']='changed'
        with self.assertRaises(PublishError): self.check(manifest=m)
        p=copy.deepcopy(self.profile);p['first_publication_runs'][0]['all_accounts']=True
        with self.assertRaises(PublishError): self.check(profile=p)

    def test_restricted_account_and_other_endpoints_refused(self):
        p=copy.deepcopy(self.profile);p['accounts']['editor']['restricted']=True
        with self.assertRaises(PublishError): self.check(profile=p)
        for platform,endpoint in [('wechat','broadcast'),('xiaohongshu','publish')]:
            t=dict(self.target,platform=platform,endpoint=endpoint);m=dict(targets=[t],assets=[])
            p=copy.deepcopy(self.profile);p['accounts']['editor']['platforms']=[platform];p['adapters'][platform]=p['adapters']['douyin']
            p['first_publication_runs'][0].update(platform=platform,endpoint=endpoint,manifest_digest=digest(m))
            with self.subTest(platform=platform),self.assertRaises(PublishError):self.check(profile=p,target=t,manifest=m)


if __name__ == '__main__':
    unittest.main()
