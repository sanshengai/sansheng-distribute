#!/usr/bin/env python3
"""Local reply drafts bound to observed inbox content. No network or authorization."""
import argparse
import json
import sys
from pathlib import Path
from interaction_import import _safe_state_dir, _string, PLATFORMS
from interaction_inbox import _lock, open_db
from publish_core import PublishError, digest, now

SCHEMA = '''CREATE TABLE IF NOT EXISTS interaction_drafts (
 draft_key TEXT PRIMARY KEY, platform TEXT NOT NULL, account TEXT NOT NULL,
 channel_id TEXT NOT NULL, comment_id TEXT NOT NULL, source_digest TEXT NOT NULL,
 reply TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(platform,account,channel_id,comment_id));'''


def source(db, platform, account, channel, comment):
    row = db.execute('''SELECT * FROM interaction_items
        WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''',
        (platform,account,channel,comment)).fetchone()
    if row is None:
        raise PublishError('原评论不在收件箱；网页观察摘要不是平台评论ID')
    if row['is_own'] or row['local_status'] != 'new':
        raise PublishError('本人评论或已处理评论不加入网友回复草稿队列')
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='interaction_reply_receipts'").fetchone():
        if db.execute('''SELECT 1 FROM interaction_reply_receipts WHERE platform=? AND account=?
                      AND channel_id=? AND parent_id=?''',(platform,account,channel,comment)).fetchone():
            raise PublishError('已有核验回复回执，不重复准备')
    identity = {k: row[k] for k in ('platform','account','channel_id','comment_id','video_id','thread_id','parent_id','author_channel','body')}
    return dict(row), digest(identity)


def prepare(root, platform, account, channel, comment, reply):
    if platform not in PLATFORMS:
        raise PublishError('不支持的平台')
    reply = _string(reply, 'reply', 10000)
    root = _safe_state_dir(root)
    with _lock(root), open_db(root) as db:
        row, snapshot = source(db,platform,account,channel,comment)
        db.execute(SCHEMA)
        key = digest({'source':snapshot,'reply':reply})
        old = db.execute('''SELECT draft_key FROM interaction_drafts WHERE platform=? AND account=?
                         AND channel_id=? AND comment_id=?''',(platform,account,channel,comment)).fetchone()
        if old and old[0] != key:
            raise PublishError('已有不同草稿；先核对并显式撤销旧草稿，不能隐式覆盖')
        db.execute('INSERT OR IGNORE INTO interaction_drafts VALUES (?,?,?,?,?,?,?,?)',
                   (key,platform,account,channel,comment,snapshot,reply,now()))
    return {'draft_key':key, 'state':'draft', 'authorized':False, 'sent':False}


def listing(root, include_text=False):
    root = _safe_state_dir(root)
    with _lock(root), open_db(root) as db:
        db.execute(SCHEMA)
        result=[]
        for draft in db.execute('SELECT * FROM interaction_drafts ORDER BY created_at').fetchall():
            out={k:draft[k] for k in ('draft_key','platform','account','channel_id','comment_id','created_at')}
            try:
                row, fingerprint=source(db,draft['platform'],draft['account'],draft['channel_id'],draft['comment_id'])
                out['state']='draft' if fingerprint==draft['source_digest'] else 'needs_review'
                out['reason']='发送前仍须查看平台当前上下文' if out['state']=='draft' else '缓存原评论或作品归属已变化'
            except PublishError as exc:
                out.update(state='blocked',reason=str(exc))
                row=None
            if include_text:
                out['reply']=draft['reply']
                out['comment']=row['body'] if row else None
            result.append(out)
    return result


def withdraw(root, key):
    root = _safe_state_dir(root)
    with _lock(root), open_db(root) as db:
        db.execute(SCHEMA)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='interaction_dispatches'").fetchone():
            bound = db.execute('SELECT * FROM interaction_dispatches WHERE draft_key=?', (key,)).fetchone()
            if bound:
                from youtube_comments_candidate import _checked_plan, _receipt_key
                intent = _checked_plan(root, bound['plan_key'], bound['account'], bound['channel_id'])
                if (root / (_receipt_key(intent) + '.json')).exists():
                    raise PublishError('草稿已有发送尝试，保留原计划与恢复点，不能撤销后重发')
        count=db.execute('DELETE FROM interaction_drafts WHERE draft_key=?',(key,)).rowcount
    return {'withdrawn': count, 'platform_action':False}


def main():
    p=argparse.ArgumentParser(description='本地回复草稿队列；不授权、不联网、不发送')
    p.add_argument('--state-dir',required=True)
    sub=p.add_subparsers(dest='action',required=True)
    add=sub.add_parser('prepare')
    for name in ('platform','account','channel','comment','text-file'): add.add_argument('--'+name,required=True)
    sub.add_parser('list').add_argument('--include-text',action='store_true')
    sub.add_parser('withdraw').add_argument('--key',required=True)
    a=p.parse_args()
    try:
        if a.action=='prepare':
            path=Path(a.text_file)
            if path.stat().st_mode & 0o077: raise PublishError('回复草稿文件权限须为0600')
            out=prepare(a.state_dir,a.platform,a.account,a.channel,a.comment,path.read_text())
        elif a.action=='list': out=listing(a.state_dir,a.include_text)
        else: out=withdraw(a.state_dir,a.key)
        print(json.dumps(out,ensure_ascii=False))
    except (PublishError,OSError,ValueError) as exc:
        print(str(exc),file=sys.stderr);return 2
    return 0

if __name__=='__main__': sys.exit(main())
