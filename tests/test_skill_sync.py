import json, subprocess, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import skill_sync as ss


def run(cwd, *a):
    subprocess.run(['git', '-C', str(cwd), *a], check=True, capture_output=True, text=True)


def commit(repo, name, text, msg='c'):
    (repo / name).write_text(text, encoding='utf-8')
    run(repo, 'add', name); run(repo, '-c', 'user.name=t', '-c', 'user.email=t@e', 'commit', '-qm', msg)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, 'extra_patterns', lambda *a: [])
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(origin)], check=True)
    a = tmp_path / 'a'; subprocess.run(['git', 'clone', '-q', str(origin), str(a)], check=True, capture_output=True)
    run(a, 'checkout', '-q', '-b', 'main')
    commit(a, 'README.md', 'hi\n'); run(a, 'push', '-q', 'origin', 'main')
    b = tmp_path / 'b'; subprocess.run(['git', 'clone', '-q', '-b', 'main', str(origin), str(b)], check=True, capture_output=True)
    return origin, a, b


def test_clean_status(repos):
    _, a, b = repos
    r = ss.status(a, mirrors=[b])
    assert r['ahead'] == 0 and r['behind'] == 0 and r['dirty'] == [] and not r['redaction_hits']
    assert r['mirrors'][0]['behind'] == 0 and not r['push_ready']


def test_dirty_blocks_push(repos):
    _, a, _ = repos
    commit(a, 'x.md', 'one\n'); (a / 'x.md').write_text('two\n')
    r = ss.status(a)
    assert r['dirty'] and not r['push_ready']
    with pytest.raises(ss.SyncError, match='未提交'):
        ss.push(a, test_cmd=None)


def test_behind_pull_ff_and_mirror_behind(repos):
    _, a, b = repos
    commit(a, 'new.md', 'n\n'); run(a, 'push', '-q', 'origin', 'main')
    assert ss.status(a, mirrors=[b])['mirrors'][0]['behind'] == 1
    with pytest.raises(ss.SyncError, match='落后'):
        commit(b, 'other.md', 'o\n'); ss.push(b, test_cmd=None)


def test_mirror_pull_fast_forward(repos):
    _, a, b = repos
    commit(a, 'new.md', 'n\n'); run(a, 'push', '-q', 'origin', 'main')
    assert ss.pull(b)['action'] == 'fast_forward'
    assert ss.status(b)['behind'] == 0


def test_diverged_pull_refuses(repos):
    _, a, b = repos
    commit(a, 'new.md', 'n\n'); run(a, 'push', '-q', 'origin', 'main')
    commit(b, 'mine.md', 'm\n')
    assert ss.status(b)['diverged']
    with pytest.raises(ss.SyncError, match='分叉'):
        ss.pull(b)


def test_redaction_hits_block_push(repos):
    _, a, _ = repos
    commit(a, 'leak.md', 'path /' + 'Users/someone/work/x\n')
    commit(a, 'run.private.json', '{}\n')
    r = ss.status(a)
    kinds = {h['kind'] for h in r['redaction_hits']}
    assert {'private_path', 'private_file'} <= kinds and not r['push_ready']
    with pytest.raises(ss.SyncError, match='脱敏'):
        ss.push(a, test_cmd=None)


def test_clean_push_fast_forward_and_failing_tests(repos):
    origin, a, _ = repos
    commit(a, 'doc.md', 'fine\n')
    with pytest.raises(ss.SyncError, match='测试未通过'):
        ss.push(a, test_cmd='false')
    assert ss.push(a, test_cmd='true', dry_run=True)['action'] == 'would_push'
    assert ss.push(a, test_cmd='true')['action'] == 'pushed'
    assert ss.status(a)['ahead'] == 0


def test_private_values_file(tmp_path):
    f = tmp_path / 'p.txt'; f.write_text('# c\n123456789\n')
    pats = ss.SECRET_LINES + ss.extra_patterns(f)
    assert ss.scan_text('a.md', 'uid 123456789', pats)[0]['kind'] == 'private_value'


def test_playlist_pattern_ignores_constants():
    pats = ss.SECRET_LINES
    assert ss.scan_text('a.py', 'PLACE_CARET_END = 1\nPLAYER_READY', pats) == []
    assert ss.scan_text('a.md', 'list PLTdYGxyH9TXAbc1', pats)[0]['kind'] == 'playlist_id'
