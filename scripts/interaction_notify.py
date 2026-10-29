#!/usr/bin/env python3
"""Optional macOS alert for new inbox items. Never fetches or sends comments."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from interaction_inbox import _lock, _private_dir, open_db
from publish_core import PublishError, now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS interaction_notice_sources (
 platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
 baseline_at TEXT NOT NULL, PRIMARY KEY(platform, account, channel_id)
);
CREATE TABLE IF NOT EXISTS interaction_notice_items (
 platform TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
 comment_id TEXT NOT NULL, state TEXT NOT NULL, batch_id TEXT,
 created_at TEXT NOT NULL, PRIMARY KEY(platform, account, channel_id, comment_id)
);
CREATE TABLE IF NOT EXISTS interaction_notice_discovery_sources (
 kind TEXT NOT NULL, account TEXT NOT NULL, channel_id TEXT NOT NULL,
 baseline_at TEXT NOT NULL, PRIMARY KEY(kind, account, channel_id)
);
'''


def _datetime(value):
    if not value:
        return None
    try:
        value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except ValueError:
        return None


def _request_notification():
    # Fixed literal: do not put external text or identifiers into AppleScript.
    script = 'display notification "发现新的互动，请查看收件箱" with title "互动收件箱"'
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True,
                                check=False, timeout=10)
        return 'requested' if result.returncode == 0 else 'failed'
    except subprocess.TimeoutExpired:
        return 'unknown'
    except OSError:
        return 'failed'


def notify_monitor_stopped():
    """One privacy-safe health warning; request is not a delivery receipt."""
    script = 'display notification "评论检查已停止，请查看互动收件箱状态" with title "互动收件箱"'
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True,
                                check=False, timeout=10)
        return 'requested' if result.returncode == 0 else 'failed'
    except subprocess.TimeoutExpired:
        return 'unknown'
    except OSError:
        return 'failed'


def _weibo_discoveries(db, stamp, batch_id, notify, ready):
    """Alert on discovery without promoting a comment to a replyable thread."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'weibo_official_sources', 'weibo_official_items'} <= tables:
        return 0, 0, 0
    baselines = ignored = candidates = 0
    sources = db.execute("SELECT account,uid FROM weibo_official_sources WHERE state='partial'").fetchall()
    for source in sources:
        key = ('weibo', source['account'], source['uid'])
        discovery_key = ('weibo-official', source['account'], source['uid'])
        baseline = db.execute('''SELECT 1 FROM interaction_notice_discovery_sources
            WHERE kind=? AND account=? AND channel_id=?''', discovery_key).fetchone()
        # One shared comment notice key deduplicates later canonical imports. A
        # canonical item owns its handling/status even if its reader is failed.
        items = db.execute('''SELECT d.comment_id,d.baseline,d.is_own
            FROM weibo_official_items d WHERE d.account=? AND d.uid=?
            AND NOT EXISTS (SELECT 1 FROM interaction_notice_items n
                WHERE n.platform='weibo' AND n.account=d.account
                AND n.channel_id=d.uid AND n.comment_id=d.comment_id)
            AND NOT EXISTS (SELECT 1 FROM interaction_items i
                WHERE i.platform='weibo' AND i.account=d.account
                AND i.channel_id=d.uid AND i.comment_id=d.comment_id)''', key[1:]).fetchall()
        if baseline is None:
            db.execute('INSERT INTO interaction_notice_discovery_sources VALUES(?,?,?,?)',
                       (*discovery_key, stamp))
        for item in items:
            item_key = (*key, item['comment_id'])
            if baseline is None:
                state = 'baseline'
                baselines += 1
            elif item['baseline'] or item['is_own']:
                state = 'ignored'
                ignored += 1
            else:
                candidates += 1
                ready.append(item_key)
                if not notify:
                    continue
                state = 'attempted'
            db.execute('INSERT INTO interaction_notice_items VALUES(?,?,?,?,?,?,?)',
                       (*item_key, state, batch_id if state == 'attempted' else None, stamp))
    return baselines, ignored, candidates


def check(root, notify=False, *, sender=_request_notification):
    """Record baseline and optionally attempt one aggregated, privacy-safe alert."""
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise PublishError('状态目录须为本机绝对路径')
    if root.resolve().is_relative_to(Path(__file__).resolve().parent.parent):
        raise PublishError('状态目录不能位于 Skill 仓库内')
    root = _private_dir(root)
    if not (root / 'interaction-inbox.sqlite3').is_file():
        raise PublishError('互动收件箱尚不存在，请先完成一次同步')
    stamp = now()
    ready = []
    baselines = older = ignored = 0
    batch_id = uuid.uuid4().hex
    with _lock(root), closing(open_db(root)) as db:
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        sources = db.execute("SELECT platform,account,channel_id FROM interaction_sources WHERE state IN ('complete','partial')").fetchall()
        has_manual = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='interaction_manual_provenance'").fetchone()
        for source in sources:
            key = tuple(source)
            baseline = db.execute('SELECT baseline_at FROM interaction_notice_sources WHERE platform=? AND account=? AND channel_id=?',key).fetchone()
            items = db.execute('''SELECT i.comment_id,i.published_at,i.is_own,i.local_status
                                  FROM interaction_items i WHERE i.platform=? AND i.account=? AND i.channel_id=?
                                  AND NOT EXISTS (SELECT 1 FROM interaction_notice_items n
                                      WHERE n.platform=i.platform AND n.account=i.account
                                      AND n.channel_id=i.channel_id AND n.comment_id=i.comment_id)''',key).fetchall()
            if baseline is None:
                db.execute('INSERT INTO interaction_notice_sources VALUES(?,?,?,?)',(*key,stamp))
                for item in items:
                    db.execute('INSERT OR IGNORE INTO interaction_notice_items VALUES(?,?,?,?,?,?,?)',
                               (*key,item['comment_id'],'baseline',None,stamp))
                    baselines += 1
                continue
            baseline_dt = _datetime(baseline['baseline_at'])
            for item in items:
                item_key = (*key,item['comment_id'])
                manual = has_manual and db.execute('''SELECT 1 FROM interaction_manual_provenance
                     WHERE platform=? AND account=? AND channel_id=? AND comment_id=?''',item_key).fetchone()
                published = _datetime(item['published_at'])
                if item['is_own'] or item['local_status'] != 'new' or manual:
                    state = 'ignored'
                    ignored += 1
                elif notify:
                    # Commit the attempt before calling the OS, to prevent a restart
                    # from sending the same item twice after an ambiguous result.
                    state = 'attempted'
                    ready.append(item_key)
                    if published and baseline_dt and published <= baseline_dt:
                        older += 1
                else:
                    # Do not consume candidates while alerts are disabled.
                    ready.append(item_key)
                    if published and baseline_dt and published <= baseline_dt:
                        older += 1
                    continue
                db.execute('INSERT INTO interaction_notice_items VALUES(?,?,?,?,?,?,?)',
                           (*item_key,state,batch_id if state == 'attempted' else None,stamp))
        discovery_baselines, discovery_ignored, context_candidates = _weibo_discoveries(
            db, stamp, batch_id, notify, ready)
        baselines += discovery_baselines
        ignored += discovery_ignored
        db.commit()
    request = 'no_candidates' if notify else 'disabled'
    if ready and notify:
        try:
            request = sender()
            if request not in {'requested','failed','unknown'}:
                request = 'unknown'
        except Exception:
            request = 'unknown'
        with _lock(root), closing(open_db(root)) as db:
            db.execute('UPDATE interaction_notice_items SET state=? WHERE batch_id=? AND state=?',
                       (request,batch_id,'attempted'))
            db.commit()
    return {'notification':request, 'candidates':len(ready), 'baseline_items':baselines,
            'historical_items':older, 'ignored_items':ignored,
            'context_candidates':context_candidates,
            'note':'只记录通知请求，不证明系统展示或用户已看到'}


def main():
    parser = argparse.ArgumentParser(description='可选的本机评论提示，不联网或发送评论')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--notify-macos', action='store_true', help='主动开启一次 macOS 通知请求')
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.state_dir, args.notify_macos), ensure_ascii=False))
    except (PublishError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc, PublishError) else '本地提醒检查失败'},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
