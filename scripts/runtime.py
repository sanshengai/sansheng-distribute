"""Optional, pinned browser dependencies in one private environment per distribution install."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

from publish_core import PublishError, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'references/browser-requirements.lock'
BROWSER_PLATFORMS = frozenset(('x', 'weibo', 'douyin', 'wechat_channels', 'xiaohongshu'))
VERSIONS = {'playwright':'1.62.0', 'pyee':'13.0.1', 'greenlet':'3.3.1', 'typing-extensions':'4.15.0'}


def selected(profile):
    modules = profile.get('configured_modules')
    if modules is None:
        modules = list(profile.get('adapters', {}))
    if not isinstance(modules, list) or any(not isinstance(p, str) for p in modules):
        raise PublishError('configured_modules 必须是平台名称列表')
    return sorted(set(modules) & BROWSER_PLATFORMS)


def directory(profile, explicit=None):
    value = explicit or profile.get('runtime_dir')
    return Path(value).expanduser().resolve() if value else Path.home()/'.local/share/sansheng-distribute/runtime'/('browser-'+VERSIONS['playwright'])


def inspect(profile, explicit=None):
    platforms = selected(profile)
    if not platforms:
        return {'ok':True, 'required':False, 'selected_platforms':[], 'installs':[]}
    root = directory(profile, explicit)
    python = root / 'bin/python3'
    base = {'ok':False, 'required':True, 'selected_platforms':platforms, 'directory':str(root)}
    receipt_path = root/'runtime.json'
    if not python.is_file() or not receipt_path.is_file():
        return dict(base, reason='尚未准备所选平台的独立浏览器运行环境')
    receipt = read_json(receipt_path)
    if receipt.get('requirements_sha256') != hashlib.sha256(LOCK.read_bytes()).hexdigest():
        return dict(base, reason='依赖锁已变化；保留原环境，先核对升级')
    try:
        result = subprocess.run([str(python), '-I', '-c',
            'import json;from importlib.metadata import version;from playwright.sync_api import sync_playwright;'
            'print(json.dumps({p:version(p) for p in ("playwright","pyee","greenlet","typing-extensions")}))'],
            capture_output=True, text=True, timeout=20)
        versions = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        versions = {}
    if versions != VERSIONS:
        return dict(base, reason='独立环境缺失或版本与锁不符')
    return dict(base, ok=True, python=str(python), versions=versions, browser_downloaded=False)


def prepare(profile, explicit=None):
    """Install one locked environment. Do not touch config, ledgers, Chrome, or global packages."""
    if not selected(profile):
        return inspect(profile, explicit)
    root = directory(profile, explicit)
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root.parent/(root.name+'.install.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublishError('同一浏览器依赖正在安装；等待原安装完成，不重复启动') from exc
        if root.exists():
            result = inspect(profile, root)
            if result['ok']:
                return dict(result, reused=True)
            raise PublishError('运行环境目录已存在且未通过检查；保留原目录，指定新 --runtime-dir 以审查升级')
        # Build at its final pathname: venv launchers may embed absolute paths.
        root.mkdir(mode=0o700)
        owned_marker = root/'.distribute-install-in-progress'
        owned_marker.touch(mode=0o600)
        try:
            venv.EnvBuilder(with_pip=True).create(root)
            env = dict(os.environ, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
            env.pop('PYTHONPATH', None)
            result = subprocess.run([str(root/'bin/python3'), '-I', '-m', 'pip', 'install', '--isolated',
                '--only-binary=:all:', '--require-hashes', '--index-url', 'https://pypi.org/simple', '-r', str(LOCK)],
                env=env, capture_output=True, text=True, timeout=300)
            if result.returncode:
                # Dependency diagnostics cannot carry private index credentials or environment dumps.
                raise PublishError('固定浏览器依赖安装失败；保留过程环境供检查，未修改系统包或浏览器')
            write_json(root/'runtime.json', {'schema_version':1, 'requirements_sha256':hashlib.sha256(LOCK.read_bytes()).hexdigest(),
                'versions':VERSIONS, 'browser_downloaded':False})
            check = inspect(profile, root)
            if not check['ok']:
                raise PublishError('依赖安装后实际导入或版本检查失败；未启用发布')
            owned_marker.unlink()
            return dict(check, installed=True)
        except subprocess.TimeoutExpired as exc:
            raise PublishError('依赖安装超时；原环境保留，未启用发布') from exc


def require_python(profile):
    value = inspect(profile)
    if not value['ok'] or not value.get('python'):
        raise PublishError('所选平台需要固定浏览器运行环境；先运行 setup.py --prepare-runtime')
    return value['python']


def worker_command(profile, module, args):
    if module not in {'browser_runtime', 'browser_engine', 'browser_prefill'}:
        raise PublishError('不支持的内置浏览器执行模块')
    # Isolated Python still gets the one explicit package path; no producer Skill discovery.
    code = 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module(sys.argv.pop(1),run_name="__main__")'
    return [require_python(profile), '-I', '-c', code, str(ROOT/'scripts'), module, *args]
