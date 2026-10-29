"""Exercise persisted browser handoff and readback; never operate a real platform."""
from datetime import datetime, timedelta, timezone
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from interaction_alipay_capture import ingest
from interaction_drafts import save_review, withdraw
from interaction_inbox import open_db
from interaction_review import ReviewDesk
from publish_core import PublishError
from test_interaction_alipay_capture import capture, row, CHANNEL, OWNER


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        self.raw = capture()
        self.ingest(self.raw)
        self.desk = ReviewDesk(self.root)
        self.pending = next(r for r in self.desk.snapshot()['items'] if r['eligible'])

    def ingest(self, raw):
        return ingest(self.root, raw, account='fixture', channel=CHANNEL, account_name=OWNER)

    def confirm(self, reply='The author edited this reply.'):
        p = next(r for r in self.desk.snapshot()['items'] if r['comment_id'] == self.pending['comment_id'])
        selected = {k: p[k] for k in ('platform', 'account', 'channel_id', 'comment_id', 'source_digest', 'draft_key')}
        return self.desk.confirm([{**selected, 'reply': reply}])

    def queue(self):
        batch = self.confirm()
        self.assertEqual((batch['sendable'], batch['handoffable']), (0, 1))
        self.desk.dispatch(batch['batch_id'])
        return batch['batch_id'], batch['accepted'][0]['draft_key']

    def proof(self, reply='The author edited this reply.'):
        raw = copy.deepcopy(self.raw)
        child = row('d', reply, own=True)
        child['time_text'] = datetime.now(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
        raw['items'][0]['replies'] = [child]
        raw['observed_at'] = datetime.now(timezone.utc).isoformat()
        return raw, child['dom_id']

    def test_confirmation_survives_restart_and_keeps_edited_text_without_queueing(self):
        b = self.confirm('An exact author edit.')
        restored = ReviewDesk(self.root).snapshot()
        self.assertEqual(restored['batches'][0]['items'][0]['reply'], 'An exact author edit.')
        self.assertEqual(restored['batches'][0]['batch_id'], b['batch_id'])
        self.assertEqual(ReviewDesk(self.root).handoffs(), [])

    def test_submit_persists_pending_task_but_does_not_claim_or_send(self):
        batch, key = self.queue()
        tasks = ReviewDesk(self.root).handoffs()
        self.assertEqual((len(tasks), tasks[0]['batch_id'], tasks[0]['draft_key']), (1, batch, key))
        self.desk.dispatch(batch)
        self.assertEqual(len(self.desk.handoffs()), 1)
        self.assertEqual(self.desk.dispatch(batch, reconcile=True)['results'][0]['state'], 'handoff_pending')
        with open_db(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_review_handoffs').fetchone()[0], 0)

    def test_read_only_check_of_unsubmitted_batch_does_not_enqueue(self):
        b = self.confirm()
        self.assertEqual(self.desk.dispatch(b['batch_id'], reconcile=True)['results'][0]['state'], 'not_attempted')
        self.assertEqual(self.desk.handoffs(), [])

    def test_claim_requires_submission_and_cannot_be_repeated_across_batches(self):
        first = self.confirm(); key = first['accepted'][0]['draft_key']
        with self.assertRaises(PublishError): self.desk.claim_handoff(first['batch_id'], key)
        second = self.confirm()
        self.desk.dispatch(first['batch_id']); self.desk.dispatch(second['batch_id'])
        self.desk.claim_handoff(first['batch_id'], key)
        with self.assertRaises(PublishError): self.desk.claim_handoff(second['batch_id'], key)
        self.assertEqual(self.desk.dispatch(second['batch_id'])['results'][0]['state'], 'blocked')
        self.assertEqual(self.desk.handoffs(), [])

    def test_claim_blocks_edit_withdraw_and_requeue_after_draft_projection_loss(self):
        batch, key = self.queue(); self.desk.claim_handoff(batch, key)
        with self.assertRaises(PublishError): withdraw(self.root, key)
        p = self.pending
        with self.assertRaises(PublishError):
            save_review(self.root, 'alipay', 'fixture', CHANNEL, p['comment_id'], 'Replacement',
                        expected_source=p['source_digest'], expected_draft=key)
        with open_db(self.root) as db: db.execute('DELETE FROM interaction_drafts')
        selected = {k: p[k] for k in ('platform', 'account', 'channel_id', 'comment_id', 'source_digest')}
        self.assertEqual(self.desk.confirm([{**selected, 'draft_key': key, 'reply': 'Replacement'}])['confirmed'], 0)
        self.assertEqual(self.desk.dispatch(batch)['results'][0]['state'], 'handoff_attempting')

    def test_changed_draft_invalidates_old_submission(self):
        batch, key = self.queue()
        self.confirm('A newer edit.')
        self.assertEqual(self.desk.handoffs(), [])
        with self.assertRaises(PublishError): self.desk.claim_handoff(batch, key)

    def test_changed_source_invalidates_old_submission(self):
        batch, key = self.queue()
        with open_db(self.root) as db:
            db.execute('UPDATE interaction_items SET body=? WHERE comment_id=?', ('Changed', self.pending['comment_id']))
        self.assertEqual(self.desk.handoffs(), [])
        with self.assertRaises(PublishError): self.desk.claim_handoff(batch, key)

    def test_missing_or_wrong_batch_cannot_claim(self):
        batch, key = self.queue()
        for b, k in [('0' * 64, key), (batch, '1' * 64)]:
            with self.assertRaises(PublishError): self.desk.claim_handoff(b, k)

    def test_verify_requires_claim_and_fresh_reply_in_correct_thread(self):
        batch, key = self.queue()
        with self.assertRaises(PublishError): self.desk.finish_handoff(batch, key, reply_id='commentContent_' + 'd' * 32)
        self.desk.claim_handoff(batch, key)
        with self.assertRaises(PublishError): self.desk.finish_handoff(batch, key, reply_id='commentContent_' + 'd' * 32)
        proof, reply_id = self.proof(); self.ingest(proof)
        result = self.desk.finish_handoff(batch, key, reply_id=reply_id)
        self.assertEqual((result['state'], result['remote_id']), ('handoff_verified', reply_id))
        restored = next(r for r in self.desk.snapshot()['items'] if r['comment_id'] == self.pending['comment_id'])
        self.assertEqual(restored['status_label'], '本次回复已发送并核对')
        self.assertTrue(restored['handled'])
        self.assertFalse(restored['eligible'])
        self.assertEqual(self.desk.finish_handoff(batch, key, reply_id=reply_id)['state'], 'handoff_verified')
        with self.assertRaises(PublishError): self.desk.finish_handoff(batch, key, reply_id='commentContent_' + 'e' * 32)
        self.assertEqual(self.desk.handoffs(), [])

    def test_wrong_reply_text_parent_or_own_identity_cannot_verify(self):
        for field, value in [('body', 'Not the approved reply'), ('parent_id', 'other'), ('is_own', 0)]:
            with self.subTest(field=field):
                batch, key = self.queue(); self.desk.claim_handoff(batch, key)
                proof, reply_id = self.proof(); self.ingest(proof)
                with open_db(self.root) as db:
                    db.execute('UPDATE interaction_items SET '+field+'=? WHERE comment_id=?', (value, reply_id))
                with self.assertRaises(PublishError): self.desk.finish_handoff(batch, key, reply_id=reply_id)
                # Separate root/state per adversarial case.
                self.tearDown_case()

    def tearDown_case(self):
        self.tmp.cleanup()
        self.setUp()

    def test_old_observation_cannot_be_used_as_post_send_proof(self):
        batch, key = self.queue(); self.desk.claim_handoff(batch, key)
        proof, reply_id = self.proof(); self.ingest(proof)
        with open_db(self.root) as db:
            db.execute('UPDATE interaction_web_provenance SET observed_at=?',
                       ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),))
        with self.assertRaises(PublishError): self.desk.finish_handoff(batch, key, reply_id=reply_id)

    def test_unknown_is_frozen_but_can_later_be_verified(self):
        batch, key = self.queue(); self.desk.claim_handoff(batch, key)
        self.assertEqual(self.desk.finish_handoff(batch, key)['state'], 'handoff_unknown')
        with self.assertRaises(PublishError): self.desk.claim_handoff(batch, key)
        proof, reply_id = self.proof(); self.ingest(proof)
        self.assertEqual(self.desk.finish_handoff(batch, key, reply_id=reply_id)['state'], 'handoff_verified')


if __name__ == '__main__':
    unittest.main()
