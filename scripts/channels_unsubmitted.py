"""视频号：结果未知、无远端 ID、浏览器接力从未回应的任务，凭作者书面确认记为未提交。

适用：执行接力已关闭（status=closed, action=execute），接力目录只有 task.json 与 lease.lock
（没有任何回应/回执），账本事件只有空的 submission_unknown，任务无远端 ID；
另附一次只读作品列表核对（matches=[]）。任一条件不满足即拒绝，保持未知。
用法：confirm（写绑定确认文件）→ publish.py resume（记未提交，账本回 prepared）→ run。
"""
import argparse, json, os, sqlite3, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_core import PublishError, digest, read_json

FIELDS = {'schema_version', 'job_key', 'identity', 'author_statement', 'confirmed_at', 'relay_digest', 'listing_check'}
LISTING = {'observed_at', 'account_identity', 'query', 'matches', 'evidence_path'}
RELAY_ALLOWED = {'task.json', 'lease.lock'}


def confirmation_path(requests, key):
    return Path(requests) / f'{key}.channels-unsubmitted.json'


def relay_state(requests, key):
    """Return digest of the closed, never-answered execute relay task; refuse anything else."""
    root = Path(requests) / f'{key}.agent'
    if not root.is_dir():
        raise PublishError('无浏览器接力记录；不适用视频号未提交确认')
    extra = sorted(p.name for p in root.iterdir() if p.name not in RELAY_ALLOWED)
    if extra:
        raise PublishError('接力目录存在回应/回执痕迹；不能认定未提交：' + ', '.join(extra))
    task = read_json(root / 'task.json')
    if task.get('status') != 'closed' or task.get('action') != 'execute':
        raise PublishError('接力不是已关闭且未回应的执行阶段；不能认定未提交')
    traces = sorted(p.name for p in Path(requests).glob(key + '*')
                    if p.name not in {f'{key}.json', f'{key}.social.json', f'{key}.agent', f'{key}.channels-unsubmitted.json'})
    if traces:
        raise PublishError('请求目录存在其他痕迹；不能认定未提交：' + ', '.join(traces))
    return digest(task)


def ledger_clean(state_dir, key):
    con = sqlite3.connect(f'file:{Path(state_dir) / "publish.sqlite3"}?mode=ro', uri=True)
    try:
        job = con.execute('SELECT state, remote_id FROM jobs WHERE key=?', (key,)).fetchone()
        events = con.execute('SELECT state, data FROM events WHERE job_key=? ORDER BY id', (key,)).fetchall()
    finally:
        con.close()
    return (job is not None and job[0] == 'submission_unknown' and not job[1] and bool(events)
            and all(s == 'submission_unknown' and d in ('{}', '') for s, d in events))


def _aware(v):
    t = datetime.fromisoformat(v)
    if t.tzinfo is None: raise ValueError
    return t


def check(value, *, key, identity, requests):
    listing = value.get('listing_check') if isinstance(value, dict) else None
    try:
        _aware(value['confirmed_at']); _aware(listing['observed_at']); ok_time = True
    except (KeyError, TypeError, ValueError):
        ok_time = False
    if (not isinstance(value, dict) or set(value) != FIELDS or value.get('schema_version') != 1
            or value['job_key'] != key or value['identity'] != identity
            or not isinstance(value['author_statement'], str) or len(value['author_statement'].strip()) < 4
            or not isinstance(listing, dict) or set(listing) != LISTING
            or listing['account_identity'] != identity or listing['matches'] != []
            or not isinstance(listing['query'], str) or not listing['query'].strip()
            or not isinstance(listing['evidence_path'], str) or not Path(listing['evidence_path']).is_file()
            or not ok_time):
        raise PublishError('视频号未提交确认不完整或不符；保持未知')
    if value['relay_digest'] != relay_state(requests, key):
        raise PublishError('接力记录在确认后发生变化；保持未知')
    return digest(value)


def recover(request, requests, identity):
    """Called on resume for a wechat_channels job in submission_unknown without remote ID."""
    job = request['job']
    if request['target']['platform'] != 'wechat_channels' or job.get('remote_id') or job.get('state') != 'submission_unknown':
        return None
    path = confirmation_path(requests, job['key'])
    if not path.exists():
        return None
    proof = check(read_json(path), key=job['key'], identity=identity, requests=requests)
    t = request['target']
    return dict(manifest_digest=request['manifest_digest'], target_id=t['id'], platform=t['platform'], account=t['account'],
                state='not_submitted', absence_verified=True, absence_scope='author_confirmed_channels_relay_unanswered',
                confirmation_digest=proof, evidence_path=str(path), observed_account_identity=identity)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state-dir', required=True); ap.add_argument('--key', required=True)
    ap.add_argument('--identity', required=True, help='视频号 ID（平台显示）')
    ap.add_argument('--statement', required=True, help='作者原话与确认时间')
    ap.add_argument('--confirmed-at', required=True, help='带时区 ISO 时间')
    ap.add_argument('--listing', required=True, help='只读作品列表核对 JSON：observed_at/account_identity/query/matches/evidence_path')
    a = ap.parse_args(argv)
    try:
        requests = Path(a.state_dir) / 'requests'
        request = read_json(requests / f'{a.key}.json')
        if request['target']['platform'] != 'wechat_channels':
            raise PublishError('仅限视频号任务')
        if request['job'].get('key') != a.key:
            raise PublishError('请求与任务 key 不符')
        if not ledger_clean(a.state_dir, a.key):
            raise PublishError('账本不是无观察的结果未知状态；不能认定未提交')
        value = dict(schema_version=1, job_key=a.key, identity=a.identity, author_statement=a.statement,
                     confirmed_at=a.confirmed_at, relay_digest=relay_state(requests, a.key),
                     listing_check=json.loads(Path(a.listing).read_text()))
        path = confirmation_path(requests, a.key)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(value, f, ensure_ascii=False, indent=1)
        try:
            check(value, key=a.key, identity=a.identity, requests=requests)
        except PublishError:
            path.unlink(); raise
    except (PublishError, OSError, KeyError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)); return 2
    print(json.dumps({'ok': True, 'confirmation': str(path)}, ensure_ascii=False)); return 0


if __name__ == '__main__':
    sys.exit(main())
