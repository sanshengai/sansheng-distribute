"""Portable private configuration discovery. No login, network or ledger mutation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ('wechat', 'x', 'weibo', 'xiaohongshu', 'douyin', 'bilibili', 'tencent_video', 'wechat_channels')


class EnvironmentError(ValueError):
    pass


def _path(value, base=None):
    value = str(value).replace('${SKILL_ROOT}', str(SKILL_ROOT))
    path = Path(value).expanduser()
    return (path if path.is_absolute() else (base or Path.cwd()) / path).resolve()


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise EnvironmentError('配置文件不可读取或不是有效 JSON') from exc
    if not isinstance(value, dict):
        raise EnvironmentError('配置必须是 JSON 对象')
    for field in ('accounts', 'adapters'):
        entries = value.get(field, {})
        if not isinstance(entries, dict) or not all(isinstance(v, dict) for v in entries.values()):
            raise EnvironmentError(f'{field} 必须是配置对象映射')
    modules = value.get('configured_modules', [])
    if not isinstance(modules, list) or not all(isinstance(v, str) and v in PLATFORMS for v in modules):
        raise EnvironmentError('configured_modules 必须是受支持的平台名称列表')
    trials = value.get('acceptance_runs', [])
    if not isinstance(trials, list) or not all(isinstance(v, dict) for v in trials):
        raise EnvironmentError('acceptance_runs 必须是对象列表')
    public_trials = value.get('first_publication_runs', [])
    if not isinstance(public_trials, list) or not all(isinstance(v, dict) for v in public_trials):
        raise EnvironmentError('first_publication_runs 必须是对象列表')
    if 'state_dir' in value and (not isinstance(value['state_dir'], str) or not value['state_dir'].strip()):
        raise EnvironmentError('state_dir 必须是非空路径字符串')
    return value


def resolve_paths(profile=None, state_dir=None, *, environ=None, home=None):
    """Explicit inputs win. Never silently choose between two default ledgers."""
    env = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    old_profile = home / '.config/sandy-publish/profile.json'
    new_profile = home / '.config/sansheng-distribute/profile.json'
    old_state = home / '.local/state/sandy-publish'
    new_state = home / '.local/state/sansheng-distribute'
    selected = profile or env.get('SANSHENG_DISTRIBUTE_PROFILE') or env.get('SANDY_PUBLISH_PROFILE')
    source = 'explicit' if profile else 'environment' if selected else 'default'
    if not selected:
        if old_profile.exists() and new_profile.exists() and old_profile.resolve() != new_profile.resolve():
            raise EnvironmentError('新旧默认配置同时存在；请用 --profile 明确选择，不能自动切换账号或账本')
        selected = old_profile if old_profile.exists() else new_profile
    selected = _path(selected)
    value = _read(selected) if selected.exists() else {}
    state = state_dir or env.get('SANSHENG_DISTRIBUTE_STATE') or env.get('SANDY_PUBLISH_STATE')
    state_source = 'explicit' if state_dir else 'environment' if state else 'default'
    if state:
        state = _path(state)
    elif value.get('state_dir'):
        state = _path(value['state_dir'], selected.parent)
        state_source = 'profile'
    else:
        if old_state.exists() and new_state.exists() and old_state.resolve() != new_state.resolve():
            raise EnvironmentError('新旧状态目录同时存在；请用 --state-dir 明确原账本，不能自动新建或迁移账本')
        state = (old_state if old_state.exists() or (selected.is_file() and not new_state.exists()) else new_state).resolve()
    return {'profile': str(selected), 'state_dir': str(state), 'profile_source': source,
            'state_source': state_source, 'profile_exists': selected.is_file()}


def load_profile(path):
    """Resolve config-relative paths without changing the on-disk configuration."""
    path = _path(path)
    value = copy.deepcopy(_read(path))
    for account in value.get('accounts', {}).values():
        if account.get('wechat_env_file'):
            account['wechat_env_file'] = str(_path(account['wechat_env_file'], path.parent))
    if value.get('state_dir'):
        value['state_dir'] = str(_path(value['state_dir'], path.parent))
    if value.get('browser_profile'):
        if not isinstance(value['browser_profile'], str):
            raise EnvironmentError('browser_profile 必须是路径字符串')
        value['browser_profile'] = str(_path(value['browser_profile'], path.parent))
    for adapter in value.get('adapters', {}).values():
        for action in ('execute', 'verify', 'continue'):
            argv = adapter.get(action)
            if not isinstance(argv, list):
                continue
            for i, item in enumerate(argv):
                if not isinstance(item, str):
                    raise EnvironmentError('执行器参数必须是字符串数组')
                previous = argv[i - 1] if i else None
                is_path = (previous in ('--profile', '--write-scripts', '--browser-profile')
                           or '${SKILL_ROOT}' in item
                           or (i == 0 and '/' in item)
                           or (i == 1 and item.endswith('.py')))
                if is_path:
                    argv[i] = str(_path(item, path.parent))
    return value


def discover_write_scripts(explicit=None, *, profile=None, home=None):
    home = Path.home() if home is None else Path(home)
    candidates = []
    if explicit:
        candidate = _path(explicit)
        return candidate if (candidate / 'wechat_api.py').is_file() else None
    if profile:
        for adapter in profile.get('adapters', {}).values():
            for action in ('execute', 'verify'):
                argv = adapter.get(action, [])
                if '--write-scripts' in argv and argv.index('--write-scripts') + 1 < len(argv):
                    candidates.append(Path(argv[argv.index('--write-scripts') + 1]))
    candidates.append(SKILL_ROOT.parent / 'sansheng-write/scripts')
    for directory in ('.agents/skills', '.claude/skills', '.codex/skills', '.gemini/config/skills'):
        candidates.append(home / directory / 'sansheng-write/scripts')
    for candidate in candidates:
        if (candidate / 'wechat_api.py').is_file():
            return candidate.resolve()
    return None


def _selected(profile):
    selected = set(profile.get('configured_modules', []))
    for platform, adapter in profile.get('adapters', {}).items():
        if adapter.get('enabled') is True or adapter.get('enabled_capabilities'):
            selected.add(platform)
    for trial in profile.get('acceptance_runs', []) + profile.get('first_publication_runs', []):
        if isinstance(trial, dict) and trial.get('platform'):
            selected.add(trial['platform'])
    return sorted(selected)


def _credential_status(account, env):
    values = {}
    if account.get('wechat_env_file'):
        try:
            lines = Path(account['wechat_env_file']).read_text(encoding='utf-8-sig').splitlines()
        except OSError:
            return '凭证文件不可读取'
        for line in lines:
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"\'')
    app = values.get('WECHAT_APP_ID') or env.get('WECHAT_APP_ID', '')
    secret = values.get('WECHAT_APP_SECRET') or env.get('WECHAT_APP_SECRET', '')
    if not app or not secret:
        return '凭证未配置（仅支持环境变量或私有 env 文件）'
    if hashlib.sha256(app.encode()).hexdigest() != account.get('wechat_appid_sha256'):
        return 'AppID 与账号指纹不匹配'
    return None


def doctor(paths, *, environ=None):
    env = os.environ if environ is None else environ
    checks = [{'name': 'python', 'ok': sys.version_info >= (3, 10)},
              {'name': 'platform', 'ok': sys.platform == 'darwin' or sys.platform.startswith('linux')}]
    if not paths['profile_exists']:
        checks.append({'name': 'configuration', 'ok': False, 'message': '尚未初始化；运行 setup.py --init'})
        return {'ok': False, 'checks': checks, 'local_only': True}
    profile = load_profile(paths['profile'])
    checks.append({'name': 'configuration', 'ok': True})
    for platform in _selected(profile):
        adapter = profile.get('adapters', {}).get(platform, {})
        errors = []
        for action in ('execute', 'verify'):
            argv = adapter.get(action)
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
                errors.append(f'{action} 命令未配置')
                continue
            if not shutil.which(argv[0]):
                errors.append(f'{action} 执行程序不存在')
            if len(argv) > 1 and argv[1].endswith('.py') and not Path(argv[1]).is_file():
                errors.append(f'{action} 脚本不存在')
            if platform == 'wechat':
                if '--write-scripts' not in argv or argv.index('--write-scripts') + 1 >= len(argv):
                    errors.append(f'{action} 缺少 --write-scripts；指定安装的 sansheng-write/scripts')
                elif not (Path(argv[argv.index('--write-scripts') + 1]) / 'wechat_api.py').is_file():
                    errors.append(f'{action} 缺少 wechat_api.py；安装 sansheng-write 或修正 --write-scripts')
        accounts = [a for a in profile.get('accounts', {}).values() if platform in a.get('platforms', [])]
        if not accounts:
            errors.append('没有配置所属账号')
        if platform == 'wechat':
            for account in accounts:
                problem = _credential_status(account, env)
                if problem:
                    errors.append(problem)
        else:
            browser = profile.get('browser_profile')
            if not isinstance(browser, str) or not browser.strip() or not Path(browser).is_absolute():
                errors.append('未配置专用 browser_profile；不能使用作者日常浏览器代替')
            for account in accounts:
                identities = account.get('public_identities', {})
                identity = identities.get(platform) if isinstance(identities, dict) else None
                if not isinstance(identity, str) or not identity.strip():
                    errors.append('未配置账号 public_identities，用于实际页面身份核对')
        checks.append({'name': platform, 'ok': not errors, 'messages': sorted(set(errors))})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks, 'local_only': True}


def initialize(path, *, platforms=(), account='main', write_scripts=None, env_file=None,
               appid_sha256=None, state_dir=None, browser_profile=None, public_identities=None):
    """Create a private inactive profile exclusively. Never overwrite or authorize."""
    path = _path(path)
    if path.exists():
        raise EnvironmentError('配置已存在；保留原文件和账本，拒绝覆盖。修改前请显式编辑选中配置')
    if not account or not account.strip():
        raise EnvironmentError('账号别名不能为空')
    if set(platforms) - set(PLATFORMS):
        raise EnvironmentError('存在不支持的平台名称')
    public_identities = public_identities or {}
    if not isinstance(public_identities, dict) or any(
        k not in platforms or k == 'wechat' or not isinstance(v, str) or not v.strip()
        for k, v in public_identities.items()
    ):
        raise EnvironmentError('公开身份必须对应本次选择的社交平台且非空')
    profile = {'schema_version': 1, 'configured_modules': list(dict.fromkeys(platforms)),
               'acceptance_runs': [], 'accounts': {}, 'adapters': {}}
    if state_dir:
        profile['state_dir'] = str(_path(state_dir, path.parent))
    if browser_profile and any(p != 'wechat' for p in platforms):
        profile['browser_profile'] = str(_path(browser_profile, path.parent))
    for platform in dict.fromkeys(platforms):
        cfg = {'platforms': [platform], 'restricted': False}
        if platform in public_identities:
            cfg['public_identities'] = {platform: public_identities[platform]}
        adapter = {'enabled': False, 'enabled_capabilities': [], 'timeout_seconds': 2100}
        script = '${SKILL_ROOT}/scripts/agent_adapter.py'
        for action in ('execute', 'verify'):
            adapter[action] = [sys.executable, '${SKILL_ROOT}/scripts/social_adapter.py', action,
                               '--profile', str(path)]
        if platform == 'wechat':
            if env_file:
                cfg['wechat_env_file'] = str(_path(env_file, path.parent))
            if appid_sha256:
                if len(appid_sha256) != 64 or any(c not in '0123456789abcdef' for c in appid_sha256):
                    raise EnvironmentError('AppID 指纹必须是 64 位 SHA-256 小写十六进制值')
                cfg['wechat_appid_sha256'] = appid_sha256
            module = discover_write_scripts(write_scripts)
            if write_scripts and module is None:
                raise EnvironmentError('--write-scripts 目录内缺少 wechat_api.py')
            # Missing optional module remains unconfigured, never substitute another user's path.
            if module:
                for action in ('execute', 'verify'):
                    adapter[action] = [sys.executable, '${SKILL_ROOT}/scripts/wechat_adapter.py', action,
                                       '--profile', str(path), '--write-scripts', str(module)]
                adapter['continue'] = [sys.executable, str(script), 'execute']
            else:
                adapter.pop('execute'); adapter.pop('verify')
        profile['accounts'][f'{platform}-{account}'] = cfg
        profile['adapters'][platform] = adapter
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise EnvironmentError('配置已存在，拒绝覆盖') from exc
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(profile, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    if _read(path) != profile:
        raise EnvironmentError('写入后回读不一致')
    return {'created': True, 'profile': str(path), 'capabilities_enabled': False}
