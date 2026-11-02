#!/usr/bin/env python3
"""Skill 双向同步：只读 status、受控 pull（仅 --ff-only）、受控 push（仅 fast-forward）。

规则见 references/sync.md。不改 git 配置，不 stash/reset/checkout，不 force。
"""
import argparse, json, os, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_NAMES = [r'\.private\.json$', r'(^|/)cookies[^/]*\.json$', r'(^|/)profile\.json$',
                 r'\.sqlite3?(-\w+)?$', r'\.db$', r'(^|/)回执', r'readback[^/]*\.json$']
SECRET_LINES = [
    (r'SESSDATA|bili_jct|DedeUserID', 'cookie'),
    (r'"?(refresh_token|access_token|client_secret|api_key)"?\s*[:=]\s*["\']?[A-Za-z0-9._\-/]{12,}', 'token'),
    ('/' + r'Users/[A-Za-z][\w.-]*/', 'private_path'),  # split so guards do not flag the rule itself
    ('/' + r'home/(?!box\b)[A-Za-z][\w.-]*/', 'private_path'),
    (r'\bPL(?=[\w-]*[a-z])(?=[\w-]*\d)[A-Za-z0-9_-]{10,40}\b', 'playlist_id'),
    (r'\bevent/[A-Za-z0-9_-]{30,}', 'collection_id'),
]
SCAN_ALLOW = {'scripts/skill_sync.py', 'tests/test_skill_sync.py'}  # 规则本身含关键字
PRIVATE_PATTERNS_FILE = Path.home() / '.config' / 'sansheng-distribute' / 'redact-patterns.txt'


class SyncError(Exception):
    pass


def git(repo, *args, check=True):
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    if check and p.returncode:
        raise SyncError(f"git {' '.join(args)} 失败：{p.stderr.strip()[:300]}")
    return p.stdout


def extra_patterns(path=PRIVATE_PATTERNS_FILE):
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    return [(re.escape(x.strip()), 'private_value') for x in lines if x.strip() and not x.startswith('#')]


def scan_text(name, text, patterns):
    hits = []
    if name in SCAN_ALLOW:
        return hits
    for rx in PRIVATE_NAMES:
        if re.search(rx, name):
            hits.append(dict(file=name, kind='private_file'))
            break
    for i, line in enumerate(text.splitlines(), 1):
        for rx, kind in patterns:
            if re.search(rx, line):
                hits.append(dict(file=name, line=i, kind=kind))
    return hits


def scan_diff(repo, base, head='HEAD', patterns=None):
    """只扫将要推送的新增行与新增文件名。"""
    patterns = (patterns if patterns is not None else SECRET_LINES + extra_patterns())
    diff = git(repo, 'diff', '--unified=0', '--no-color', f'{base}..{head}')
    hits, name, ln = [], None, 0
    for line in diff.splitlines():
        if line.startswith('+++ '):
            name = line[6:] if line.startswith('+++ b/') else None
            if name:
                hits += [h for h in scan_text(name, '', patterns)]
        elif line.startswith('@@'):
            m = re.search(r'\+(\d+)', line); ln = int(m.group(1)) if m else 0
        elif line.startswith('+') and name and name not in SCAN_ALLOW:
            for rx, kind in patterns:
                if re.search(rx, line[1:]):
                    hits.append(dict(file=name, line=ln, kind=kind))
            ln += 1
    return hits


def scan_worktree(repo, patterns=None):
    patterns = (patterns if patterns is not None else SECRET_LINES + extra_patterns())
    hits = []
    for rel in dirty_files(repo):
        path = Path(repo) / rel['path']
        if path.is_file():
            try:
                text = path.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                text = ''
            if rel['status'].startswith('M'):
                text = '\n'.join(l[1:] for l in git(repo, 'diff', '--unified=0', 'HEAD', '--', rel['path']).splitlines()
                                 if l.startswith('+') and not l.startswith('+++'))
            hits += scan_text(rel['path'], text, patterns)
    return hits


def dirty_files(repo):
    out = []
    for line in git(repo, 'status', '--porcelain', '--untracked-files=all').splitlines():
        out.append(dict(status=line[:2].strip() or '?', path=line[3:]))
    return out


def ahead_behind(repo, remote, branch):
    ref = f'{remote}/{branch}'
    if git(repo, 'rev-parse', '--verify', '--quiet', ref, check=False).strip() == '':
        raise SyncError(f'找不到 {ref}')
    a, b = git(repo, 'rev-list', '--left-right', '--count', f'HEAD...{ref}').split()
    return int(a), int(b)


def status(repo, remote='origin', branch='main', mirrors=(), fetch=True):
    if fetch:
        git(repo, 'fetch', '--quiet', remote)
    ahead, behind = ahead_behind(repo, remote, branch)
    dirty = dirty_files(repo)
    rep = dict(repo=str(repo), head=git(repo, 'rev-parse', '--short', 'HEAD').strip(),
               remote=f'{remote}/{branch}', ahead=ahead, behind=behind,
               diverged=ahead > 0 and behind > 0, dirty=dirty,
               redaction_hits=scan_worktree(repo) + (scan_diff(repo, f'{remote}/{branch}') if ahead else []),
               mirrors=[])
    for m in mirrors:
        try:
            if fetch:
                git(m, 'fetch', '--quiet', remote)
            ma, mb = ahead_behind(m, remote, branch)
            rep['mirrors'].append(dict(path=str(m), ahead=ma, behind=mb, dirty=len(dirty_files(m))))
        except SyncError as e:
            rep['mirrors'].append(dict(path=str(m), error=str(e)))
    rep['push_ready'] = not dirty and behind == 0 and ahead > 0 and not rep['redaction_hits']
    return rep


def pull(repo, remote='origin', branch='main'):
    git(repo, 'fetch', '--quiet', remote)
    ahead, behind = ahead_behind(repo, remote, branch)
    if ahead and behind:
        raise SyncError('本地与远端已分叉；不自动合并，交作者处理')
    if not behind:
        return dict(ok=True, action='up_to_date')
    git(repo, 'merge', '--ff-only', f'{remote}/{branch}')
    return dict(ok=True, action='fast_forward', pulled=behind)


def push(repo, remote='origin', branch='main', test_cmd=None, dry_run=False):
    git(repo, 'fetch', '--quiet', remote)
    ahead, behind = ahead_behind(repo, remote, branch)
    if behind:
        raise SyncError(f'落后 {remote}/{branch} {behind} 个提交；先 pull --ff-only')
    if not ahead:
        return dict(ok=True, action='nothing_to_push')
    if dirty_files(repo):
        raise SyncError('工作区有未提交改动；先提交或请作者认领')
    hits = scan_diff(repo, f'{remote}/{branch}')
    if hits:
        raise SyncError('脱敏扫描命中：' + json.dumps(hits[:10], ensure_ascii=False))
    if test_cmd:
        p = subprocess.run(test_cmd, shell=True, cwd=repo, capture_output=True, text=True)
        if p.returncode:
            raise SyncError('测试未通过：' + (p.stdout + p.stderr)[-400:])
    if dry_run:
        return dict(ok=True, action='would_push', commits=ahead)
    git(repo, 'push', remote, f'HEAD:refs/heads/{branch}')
    return dict(ok=True, action='pushed', commits=ahead)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Skill 同步（见 references/sync.md）')
    ap.add_argument('command', choices=['status', 'pull', 'push'])
    ap.add_argument('--repo', default=str(ROOT))
    ap.add_argument('--remote', default='origin')
    ap.add_argument('--branch', default='main')
    ap.add_argument('--mirror', action='append', default=[])
    ap.add_argument('--no-fetch', action='store_true')
    ap.add_argument('--test-cmd', default=f'{sys.executable} -m pytest -q')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    try:
        if a.command == 'status':
            r = status(a.repo, a.remote, a.branch, a.mirror, fetch=not a.no_fetch)
        elif a.command == 'pull':
            r = pull(a.repo, a.remote, a.branch)
        else:
            r = push(a.repo, a.remote, a.branch, a.test_cmd, a.dry_run)
    except SyncError as e:
        print(json.dumps(dict(ok=False, error=str(e)), ensure_ascii=False)); return 1
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
