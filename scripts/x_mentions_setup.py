#!/usr/bin/env python3
"""Local opt-in setup for the X mentions candidate; never makes network calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from interaction_import import _safe_state_dir
from publish_core import PublishError
from x_mentions_candidate import ENV, _validate_source

FIELDS = {'enabled', 'account', 'user_id', 'state_dir', 'token_env'}
ROOT = Path(__file__).resolve().parent.parent


class PaidAuthorizationRequired(PublishError):
    pass


def _config_path(value):
    p = Path(value).expanduser()
    if not p.is_absolute() or p.is_symlink() or p.resolve().is_relative_to(ROOT):
        raise PublishError('配置文件须是 Skill 仓库外的私有绝对路径，且不能是符号链接')
    return p


def _read_config(path):
    p = _config_path(path)
    if not p.exists():
        return None
    if not p.is_file() or p.stat().st_mode & 0o077:
        raise PublishError('配置文件权限须为 0600')
    with p.open(encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict) or set(data) != FIELDS or type(data.get('enabled')) is not bool:
        raise PublishError('X 提及配置无效')
    _validate_source(data['account'], data['user_id'])
    if not isinstance(data['token_env'], str) or not ENV.fullmatch(data['token_env']):
        raise PublishError('环境变量名无效')
    if not isinstance(data['state_dir'], str) or not Path(data['state_dir']).is_absolute():
        raise PublishError('状态目录须为绝对路径')
    return data


def _save_config(path, data, *, replace=False):
    p = _config_path(path)
    parent = _safe_state_dir(p.parent)
    if p.exists() and not replace:
        raise PublishError('配置文件已存在；请使用 --edit 或继续沿用')
    fd, temp = tempfile.mkstemp(prefix='.x-mentions-', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        if replace:
            os.replace(temp, p)
        else:
            # Link prevents silently overwriting a file created concurrently.
            os.link(temp, p, follow_symlinks=False)
            os.unlink(temp)
        loaded = _read_config(p)
        if loaded != data:
            raise PublishError('配置回读不一致')
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def diagnose(path):
    data = _read_config(path)
    if data is None or not data['enabled']:
        return {'ok': True, 'checks': [], 'local_only': True}
    token = os.environ.get(data['token_env'], '')
    usable = bool(token and '\n' not in token and '\r' not in token)
    return {'ok': usable, 'checks': [{'name': 'credential_environment', 'ok': usable}], 'local_only': True}


def interactive(path, *, edit=False, input_fn=input, output=print, is_tty=None):
    existing = _read_config(path)
    if existing and existing['enabled'] and not edit:
        result = diagnose(path)
        output('已有配置已读取；凭据环境变量' + ('可用。' if result['ok'] else '尚未提供。'))
        return result
    if is_tty is None:
        is_tty = sys.stdin.isatty()
    if not is_tty:
        output('交互式配置需要终端：将询问是否启用、账号、X 用户 ID、私有状态目录和凭据环境变量名。')
        return {'ok': False, 'reason': 'interactive_terminal_required', 'local_only': True}
    choice = input_fn('启用 X 提及只读候选？可能收费，需要 X OAuth 凭据和账号 ID [y/N]：').strip().lower()
    if choice not in {'y', 'yes'}:
        output('已保留原有配置；当前仍启用。' if existing and existing['enabled'] else
               '未启用；没有更改现有配置。')
        return {'ok': True, 'enabled': bool(existing and existing['enabled']), 'changed': False, 'local_only': True}
    account = input_fn('本地账号标识：').strip()
    user_id = input_fn('X 数字用户 ID：').strip()
    state_dir = input_fn('私有互动账本目录（绝对路径）：').strip()
    token_env = input_fn('OAuth access token 的环境变量名（不要输入 token）：').strip()
    _validate_source(account, user_id)
    if not ENV.fullmatch(token_env):
        raise PublishError('凭据环境变量名无效')
    state = _safe_state_dir(state_dir)
    data = {'enabled': True, 'account': account, 'user_id': user_id,
            'state_dir': str(state), 'token_env': token_env}
    _save_config(path, data, replace=bool(existing))
    result = diagnose(path)
    output('配置已保存并回读；凭据环境变量' + ('可用。' if result['ok'] else '尚未提供。'))
    return result


def run(path, accept_paid=False, *, runner=subprocess.run):
    if not accept_paid:
        raise PaidAuthorizationRequired('本次可能收费，未明确确认费用；不调用接口')
    data = _read_config(path)
    if data is None or not data['enabled']:
        raise PublishError('尚未启用 X 提及候选')
    if not diagnose(path)['ok']:
        raise PublishError('OAuth 凭据环境变量不可用')
    cmd = [sys.executable, str(ROOT / 'scripts' / 'x_mentions_candidate.py'),
           '--state-dir', data['state_dir'], '--account', data['account'],
           '--user-id', data['user_id'], '--token-env', data['token_env'], '--accept-paid-api']
    return runner(cmd, check=False).returncode


def main():
    p = argparse.ArgumentParser(description='X 提及候选的本地可选配置；setup/doctor 不联网')
    p.add_argument('--config', required=True, help='Skill 仓库外权限 0600 的配置文件')
    sub = p.add_subparsers(dest='action', required=True)
    setup = sub.add_parser('setup'); setup.add_argument('--edit', action='store_true')
    sub.add_parser('doctor')
    sub.add_parser('disable', help='只禁用本地 X 提及配置，不删除凭据或账本')
    query = sub.add_parser('run'); query.add_argument('--accept-paid-api', action='store_true')
    args = p.parse_args()
    try:
        if args.action == 'setup':
            result = interactive(args.config, edit=args.edit)
            return 0 if result['ok'] else 1
        if args.action == 'doctor':
            result = diagnose(args.config)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['ok'] else 1
        if args.action == 'disable':
            data = _read_config(args.config)
            if data and data['enabled']:
                data['enabled'] = False
                _save_config(args.config, data, replace=True)
            print(json.dumps({'ok': True, 'local_only': True}, ensure_ascii=False))
            return 0
        return run(args.config, args.accept_paid_api)
    except PaidAuthorizationRequired:
        print('本次 X API 查询可能收费；请先确认费用并取得授权。本次未联网。', file=sys.stderr)
        return 1
    except (PublishError, OSError, ValueError, json.JSONDecodeError, EOFError):
        print('配置或启动未完成；请检查私有路径、权限与必要参数。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
