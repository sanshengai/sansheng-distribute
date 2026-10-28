"""Original-authorized YouTube continuation cannot create new submissions."""
import unittest
import test_youtube_continue_core as fixture
from publish_core import PublishError, run

class OriginalGrantContinuationTest(fixture.YoutubeContinuationCoreTest):
    def opt_in_without_opening_publish(self):
        self.profile['accounts']['owner']['youtube_allow_authorized_continuation'] = True
        self.profile['adapters']['youtube']['enabled_capabilities'] = []
        self.profile_file.write_text(__import__('json').dumps(self.profile))

    def test_expired_trial_allows_only_existing_authorized_session(self):
        self.interrupted(); self.opt_in_without_opening_publish()
        result=self.cont()
        self.assertEqual(result['state'],'published')
        self.assertEqual(sum(x[0]=='POST' for x in self.fake.calls),1)

    def test_removed_grant_blocks_opted_in_continuation(self):
        self.interrupted(); self.opt_in_without_opening_publish()
        self.store.db.execute('DELETE FROM grants'); self.store.db.commit()
        with self.assertRaises(PublishError): self.cont()
        self.assertEqual(self.fake.offset,4)

    def test_other_account_lock_blocks_opted_in_continuation(self):
        self.interrupted(); self.opt_in_without_opening_publish()
        self.store.db.execute('UPDATE account_locks SET job_key=? WHERE job_key=?',('other-job',self.key));self.store.db.commit()
        with self.assertRaises(PublishError): self.cont()
        self.assertEqual(self.fake.offset,4)

    def test_no_opt_in_rejects_disabled_capability(self):
        self.interrupted()
        self.profile['adapters']['youtube']['enabled_capabilities']=[]
        with self.assertRaises(PublishError): self.cont()
        self.assertEqual(self.fake.offset,4)

    def test_opt_in_does_not_enable_new_upload(self):
        self.opt_in_without_opening_publish()
        with self.assertRaises(PublishError): run(self.path,'t',self.profile,self.store)
        self.assertEqual(self.fake.offset,0)
        self.assertFalse(self.fake.calls)

    def test_restricted_account_never_continues_write(self):
        self.interrupted(); self.opt_in_without_opening_publish()
        self.profile['accounts']['owner']['restricted'] = True
        with self.assertRaises(PublishError): self.cont()
        self.assertEqual(self.fake.offset,4)
