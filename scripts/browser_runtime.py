"""Shared attached-Chrome connection: profile binding, local endpoint and per-profile lease."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from environment import load_profile
from publish_core import PublishError

ORIGINS = {
    'x': {'https://x.com'}, 'weibo': {'https://weibo.com', 'https://www.weibo.com'},
    'douyin': {'https://creator.douyin.com'}, 'bilibili': {'https://member.bilibili.com'},
    'wechat_channels': {'https://channels.weixin.qq.com'},
    'xiaohongshu': {'https://creator.xiaohongshu.com'}, 'wechat': {'https://mp.weixin.qq.com'},
}


def endpoint(value):
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == 'http' and parts.hostname in {'127.0.0.1', 'localhost'} and parts.port
                 and parts.path in {'', '/'} and not parts.query and not parts.fragment
                 and not parts.username and not parts.password)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise PublishError('仅连接显式配置的本机专用 CDP 端点')
    return value.rstrip('/')


def browser_arguments(session):
    try:
        return session.send('Browser.getBrowserCommandLine')['arguments']
    except Exception:
        processes = session.send('SystemInfo.getProcessInfo')['processInfo']
        ids = [p['id'] for p in processes if p['type'] == 'browser']
        if len(ids) != 1 or type(ids[0]) is not int or ids[0] <= 0:
            raise PublishError('无法确定实际连接的浏览器进程')
        process = subprocess.run(['ps', '-ww', '-p', str(ids[0]), '-o', 'command='],
                                 capture_output=True, text=True, timeout=5)
        if process.returncode:
            raise PublishError('无法只读核对实际浏览器进程')
        values = re.findall(r'(?:^|\s)--user-data-dir=(.*?)(?=\s--[a-zA-Z]|$)', process.stdout.strip())
        return ['--user-data-dir=' + v for v in values]


def assert_profile(session, expected):
    profiles = [a.partition('=')[2] for a in browser_arguments(session) if a.startswith('--user-data-dir=')]
    if len(profiles) != 1 or Path(profiles[0]).resolve() != Path(expected).resolve():
        raise PublishError('实际连接的 Chrome 不是约定的专用 profile')


def browser_config(profile):
    cfg = profile.get('browser_runtime', {})
    # Existing prefill endpoint continues to be accepted without rewriting profiles.
    url = cfg.get('cdp_url') or profile.get('browser_prefill', {}).get('cdp_url')
    browser = profile.get('browser_profile')
    if not isinstance(browser, str) or not Path(browser).is_absolute():
        raise PublishError('需要专用 browser_profile 绝对路径')
    return endpoint(url), str(Path(browser).resolve())


@contextlib.contextmanager
def profile_lease(browser, *, directory=None, timeout=120):
    root = Path(directory) if directory else Path.home()/'.local/state/sansheng-distribute/browser-locks'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(str(Path(browser).resolve()).encode()).hexdigest()
    with (root/(key+'.lock')).open('a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise PublishError('同一专用浏览器正在执行另一任务；保留当前任务，不并行写入') from exc
                time.sleep(.1)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def connect(profile, *, writing=False):
    url, browser = browser_config(profile)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PublishError('浏览器依赖未准备；使用 setup.py --prepare-runtime') from exc
    lease = profile_lease(browser) if writing else contextlib.nullcontext()
    with lease, sync_playwright() as pw:
        connection = pw.chromium.connect_over_cdp(url, timeout=15000, no_defaults=True)
        assert_profile(connection.new_browser_cdp_session(), browser)
        # Stop only our Playwright driver on exit; never close/restart the user's Chrome.
        yield connection


@contextlib.contextmanager
def background_tab(anchor):
    """Create an owned identity tab without activating Chrome or another tab."""
    context = anchor.context
    session = context.new_cdp_session(anchor)
    owned = None
    target = None
    try:
        info = session.send('Target.getTargetInfo')['targetInfo']
        params = dict(url='about:blank', background=True)
        if info.get('browserContextId'):
            params['browserContextId'] = info['browserContextId']
        with context.expect_page(timeout=10000) as created:
            target = session.send('Target.createTarget', params)['targetId']
        candidate = created.value
        check = context.new_cdp_session(candidate)
        try:
            actual = check.send('Target.getTargetInfo')['targetInfo']['targetId']
        finally:
            check.detach()
        if actual != target:
            # An unrelated concurrent popup is not ours to close or navigate.
            raise PublishError('后台身份页身份不符；不操作其他标签页')
        owned = candidate
        yield owned
    finally:
        try:
            if owned is not None:
                owned.close()
            elif target is not None:
                session.send('Target.closeTarget', dict(targetId=target))
        finally:
            session.detach()


def select_page(connection, platform, *, target_id=None, path=None):
    matches = []
    for context in connection.contexts:
        for page in context.pages:
            parts = urlsplit(page.url)
            if f'{parts.scheme}://{parts.netloc}' not in ORIGINS.get(platform, set()):
                continue
            if path is not None and parts.path != path:
                continue
            if target_id:
                session = context.new_cdp_session(page)
                try:
                    if session.send('Target.getTargetInfo')['targetInfo']['targetId'] != target_id:
                        continue
                finally:
                    session.detach()
            matches.append(page)
    if len(matches) != 1:
        raise PublishError('平台页面不是唯一；提供明确 target_id 或精确编辑路径，不猜选标签页')
    return matches[0]


def inspect(profile):
    with connect(profile) as connection:
        counts = {p:0 for p in ORIGINS}
        for context in connection.contexts:
            for page in context.pages:
                parts = urlsplit(page.url)
                origin = f'{parts.scheme}://{parts.netloc}'
                for platform, origins in ORIGINS.items():
                    if origin in origins:
                        counts[platform] += 1
        return dict(ok=True, dedicated_profile_verified=True, platform_page_counts=counts,
                    browser_restarted=False, writes=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect'])
    parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(load_profile(args.profile)), ensure_ascii=False)); return 0
    except Exception as exc:
        print(json.dumps(dict(ok=False, error=str(exc) if isinstance(exc, PublishError) else
            '浏览器连接或检查失败；未操作主浏览器', error_type=type(exc).__name__), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__': raise SystemExit(main())
