"""记录作者书面确认：X 任务在写入任何尝试记录之前就已停止，未提交。

只读核对账本事件（不写数据库）与请求目录，写一份绑定任务的确认文件；
之后仍须 `resume` 只读核对（记为未提交并释放账号锁）再 `run`。
任一尝试记录、上传或发送痕迹存在即拒绝。
"""
import argparse, json, os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_core import PublishError, read_json
import no_attempt_recovery as na


def ledger_events(state_dir, key):
    db = Path(state_dir) / 'publish.sqlite3'
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        job = con.execute('SELECT state, remote_id FROM jobs WHERE key=?', (key,)).fetchone()
        events = con.execute('SELECT state, data FROM events WHERE job_key=? ORDER BY id', (key,)).fetchall()
    finally:
        con.close()
    return job, events


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state-dir', required=True); ap.add_argument('--key', required=True)
    ap.add_argument('--identity', required=True, help='配置中的 X 公开身份，如 @handle')
    ap.add_argument('--statement', required=True, help='作者原话与确认时间')
    ap.add_argument('--confirmed-at', required=True, help='带时区 ISO 时间')
    ap.add_argument('--timeline', required=True, help='只读时间线核对 JSON：observed_at/account_identity/query/matches/evidence_path')
    a = ap.parse_args(argv)
    try:
        requests = Path(a.state_dir) / 'requests'
        request = read_json(requests / f'{a.key}.json')
        if request['target']['platform'] not in na.PLATFORMS:
            raise PublishError('无尝试记录恢复仅限 X')
        if request['job'].get('key') != a.key:
            raise PublishError('请求与任务 key 不符')
        job, events = ledger_events(a.state_dir, a.key)
        if not job or job[0] != 'submission_unknown' or job[1]:
            raise PublishError('只接受无远端 ID 的结果未知任务')
        if not na.ledger_clean(events):
            raise PublishError('账本事件含观察记录；不能认定未提交')
        value = dict(schema_version=1, job_key=a.key, identity=a.identity, author_statement=a.statement,
                     confirmed_at=a.confirmed_at,
                     ledger_check={'events_without_observation': True, 'remote_id': None},
                     timeline_check=json.loads(Path(a.timeline).read_text()))
        path = na.confirmation_path(requests, a.key)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(value, f, ensure_ascii=False, indent=1)
        try:
            na.check(value, key=a.key, identity=a.identity, requests=requests)
        except PublishError:
            path.unlink(); raise
    except (PublishError, OSError, KeyError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)); return 2
    print(json.dumps({'ok': True, 'confirmation': str(path)}, ensure_ascii=False)); return 0


if __name__ == '__main__':
    sys.exit(main())
