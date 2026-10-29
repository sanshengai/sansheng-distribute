import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from interaction_drafts import prepare,listing,withdraw
from interaction_import import import_manual
from interaction_inbox import open_db
from interaction_dashboard import render
from publish_core import PublishError

class DraftTests(unittest.TestCase):
    def test_queue_binds_source_and_never_marks_handled(self):
        with tempfile.TemporaryDirectory() as f:
            root=Path(f)
            import_manual(root,dict(platform='bilibili',account='test',channel_id='owner',items=[dict(comment_id='c1',video_id='v1',body='question',is_own=False)]))
            one=prepare(root,'bilibili','test','owner','c1','answer')
            self.assertFalse(one['sent']);self.assertFalse(one['authorized'])
            self.assertEqual(one,prepare(root,'bilibili','test','owner','c1','answer'))
            self.assertIn('本地回复草稿（1 条；非发送队列）',render(root/'interaction-inbox.sqlite3'))
            self.assertNotIn('<p>answer</p>',render(root/'interaction-inbox.sqlite3'))
            self.assertIn('<p>answer</p>',render(root/'interaction-inbox.sqlite3',True))
            self.assertNotIn('reply',listing(root)[0])
            self.assertEqual(listing(root,True)[0]['reply'],'answer')
            with self.assertRaises(PublishError): prepare(root,'bilibili','test','owner','c1','different')
            with open_db(root) as db:
                self.assertEqual(db.execute('SELECT local_status FROM interaction_items').fetchone()[0],'new')
                db.execute("UPDATE interaction_items SET body='edited'")
            self.assertEqual(listing(root)[0]['state'],'needs_review')
            withdraw(root,one['draft_key'])
            self.assertEqual(listing(root),[])

    def test_missing_empty_own_and_handled_rejected(self):
        with tempfile.TemporaryDirectory() as f:
            root=Path(f)
            with self.assertRaises(PublishError): prepare(root,'bilibili','test','owner','fake','answer')
            import_manual(root,dict(platform='bilibili',account='test',channel_id='owner',items=[dict(comment_id='c1',video_id='v1',body='question',is_own=True)]))
            with self.assertRaises(PublishError): prepare(root,'bilibili','test','owner','c1','answer')
            with open_db(root) as db: db.execute("UPDATE interaction_items SET is_own=0,local_status='handled'")
            with self.assertRaises(PublishError): prepare(root,'bilibili','test','owner','c1','answer')
            with self.assertRaises(PublishError): prepare(root,'bilibili','test','owner','c1','')

    def test_verified_reply_blocks_existing_draft(self):
        with tempfile.TemporaryDirectory() as f:
            root=Path(f)
            import_manual(root,dict(platform='youtube',account='test',channel_id='owner',items=[dict(comment_id='c1',video_id='v1',body='question',is_own=False)]))
            prepare(root,'youtube','test','owner','c1','answer')
            with open_db(root) as db:
                db.execute('CREATE TABLE interaction_reply_receipts(platform,account,channel_id,parent_id)')
                db.execute("INSERT INTO interaction_reply_receipts VALUES('youtube','test','owner','c1')")
            self.assertEqual(listing(root)[0]['state'],'blocked')
            with self.assertRaises(PublishError):prepare(root,'youtube','test','owner','c1','answer')

if __name__=='__main__':unittest.main()
