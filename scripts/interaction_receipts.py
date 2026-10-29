"""Project verified YouTube reply receipts into the private inbox; never send."""
from interaction_inbox import open_db, _lock
from publish_core import PublishError, digest, now


def record_reply(root, account, intent, receipt, *, source_comment_id=None):
    if (not account or intent.get('action') != 'reply' or not intent.get('parent_id')
            or receipt.get('state') != 'verified'
            or receipt.get('intent_digest') != digest(intent)
            or not receipt.get('remote_id')
            or receipt.get('thread_id') != intent.get('parent_thread_id')):
        raise PublishError('必须使用匹配的已核验回复回执')
    with _lock(root), open_db(root) as db:
        source_id = source_comment_id or intent['parent_id']
        row = db.execute('''SELECT video_id,thread_id,parent_id,comment_id FROM interaction_items
            WHERE platform='youtube' AND account=? AND channel_id=? AND comment_id=?''',
            (account, intent['channel_id'], source_id)).fetchone()
        if row is None:
            return {'recorded': False, 'reason': 'parent_not_in_inbox'}
        if (row['video_id'] != intent['video_id'] or row['thread_id'] != intent['parent_thread_id']
                or (row['parent_id'] or row['comment_id']) != intent['parent_id']):
            raise PublishError('收件箱中的作品或评论串与回执不匹配')
        db.execute('''CREATE TABLE IF NOT EXISTS interaction_reply_receipts (
            platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
            parent_id TEXT NOT NULL, remote_id TEXT NOT NULL, intent_digest TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            PRIMARY KEY(platform,account,channel_id,remote_id))''')
        db.execute('''INSERT OR IGNORE INTO interaction_reply_receipts VALUES
            ('youtube',?,?,?,?,?,?)''', (account, intent['channel_id'], source_id,
            receipt['remote_id'], receipt['intent_digest'], now()))
        return {'recorded': True}
