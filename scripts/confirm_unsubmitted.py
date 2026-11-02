"""记录作者书面确认：某个首次上传前停止、原窗口已丢失的尝试未提交。

只写一份绑定原尝试记录的确认文件，不改账本、不连接平台。之后仍须 `resume`
只读核对（记为未提交）再 `run`。确认须引用作者原话与时间，并附一次只读作品列表核对。
"""
import argparse, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_core import PublishError, digest, read_json
import browser_preinput as pre


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state-dir', required=True); ap.add_argument('--key', required=True)
    ap.add_argument('--statement', required=True, help='作者原话与确认时间')
    ap.add_argument('--confirmed-at', required=True, help='带时区 ISO 时间')
    ap.add_argument('--listing', required=True, help='只读作品列表核对 JSON：observed_at/account_identity/query/matches/evidence_path')
    ap.add_argument('--release', action='store_true', help='抖音已上传未填写：作者选择释放并重新上传，而非原窗口续填')
    a = ap.parse_args(argv)
    try:
        requests = Path(a.state_dir) / 'requests'
        marker = requests / f'{a.key}.browser-attempt.json'
        request = read_json(requests / f'{a.key}.json'); saved = read_json(marker)
        platform = request['target']['platform']
        import douyin_unsent_release as unsent
        import douyin_uploaded_resume as uploaded
        resumable = platform == 'douyin' and uploaded.applies(saved)
        if resumable:
            saved = uploaded.original(saved)
        if not ((platform in pre.AUTHOR_CONFIRMABLE and pre.eligible(saved, platform)) or unsent.eligible(saved, platform) or resumable):
            raise PublishError('该尝试不是可确认的首次上传前状态')
        if request['job'].get('key') != a.key or request['job'].get('state') not in {'submission_unknown', 'prepared'} or request['job'].get('remote_id'):
            # The request snapshot is taken at run start; resume re-checks the live ledger state.
            raise PublishError('只接受无远端 ID 的结果未知任务')
        listing = json.loads(Path(a.listing).read_text())
        value = dict(schema_version=1, job_key=a.key, attempt_digest=digest(saved), identity=saved.get('identity'),
                     author_statement=a.statement, confirmed_at=a.confirmed_at, listing_check=listing)
        if a.release:
            if not resumable:
                raise PublishError('--release 只用于抖音已上传未填写的尝试')
            rp = uploaded.release_path(marker)
            fd = os.open(rp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(dict(schema_version=1, job_key=a.key, attempt_digest=digest(uploaded.original(saved)),
                               author_statement=a.statement, confirmed_at=a.confirmed_at), f, ensure_ascii=False, indent=1)
        path = pre.confirmation_path(marker)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(value, f, ensure_ascii=False, indent=1)
        try:
            pre.author_confirmation(marker, saved, request, dict(identity=saved.get('identity')))
        except PublishError:
            path.unlink(); raise
    except (PublishError, OSError, KeyError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)); return 2
    print(json.dumps({'ok': True, 'confirmation': str(path)}, ensure_ascii=False)); return 0


if __name__ == '__main__':
    sys.exit(main())
