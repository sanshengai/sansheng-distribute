#!/usr/bin/env python3
"""Initialize private configuration and inspect local prerequisites only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
from environment import (EnvironmentError, PLATFORMS, doctor, initialize, resolve_paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description='叁笙分发：私有配置初始化与本地诊断，不登录、不发布')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--init', action='store_true', help='非交互初始化；已有配置拒绝覆盖')
    mode.add_argument('--show-profile', action='store_true', help='只显示配置/账本路径，不输出配置或秘密')
    mode.add_argument('--doctor', action='store_true', help='仅检查已选模块与核心本地依赖')
    parser.add_argument('--profile')
    parser.add_argument('--state-dir')
    parser.add_argument('--platform', action='append', choices=PLATFORMS, default=[])
    parser.add_argument('--account', default='main', help='本地账号别名，不是密码')
    parser.add_argument('--write-scripts')
    parser.add_argument('--wechat-env-file')
    parser.add_argument('--wechat-appid-sha256', help='已确认公众号 AppID 的 SHA-256，不是 AppSecret')
    parser.add_argument('--browser-profile', help='社交模块使用的专用浏览器用户目录；不创建、不登录')
    parser.add_argument('--public-identity', action='append', default=[], metavar='PLATFORM=IDENTITY',
                        help='已确认的公开账号身份，可重复；例如 x=@your_handle')
    args = parser.parse_args(argv)
    try:
        paths = resolve_paths(args.profile, args.state_dir)
        if args.show_profile:
            result = paths
        elif args.doctor:
            result = doctor(paths)
        else:
            if paths['profile_exists'] and not args.init:
                # Repeated interactive setup recognizes current config without asking credentials again.
                result = {'existing': True, 'paths': paths, 'diagnosis': doctor(paths)}
            elif not args.init and not sys.stdin.isatty():
                result = {'ok': False, 'needs_input': ['选择平台（可全不选）', '已选平台的账号别名与私有配置路径'],
                          'hint': '非交互请用 --init；可重复 --platform 选择模块。不会覆盖已有配置。'}
            else:
                platforms = list(args.platform)
                identities = {}
                for item in args.public_identity:
                    platform, separator, identity = item.partition('=')
                    if not separator or platform in identities:
                        raise EnvironmentError('--public-identity 需为不重复的 PLATFORM=IDENTITY')
                    identities[platform] = identity
                if not args.init:
                    descriptions = {
                        'wechat': '公众号草稿；需要公众号 API 凭证、Write 的 wechat_api.py，补媒体需要 Agent 浏览器',
                        'x': '推特图文/视频；需要登录的专用浏览器和 Agent 浏览器工具',
                        'weibo': '微博图文/视频；需要登录的专用浏览器和 Agent 浏览器工具',
                        'xiaohongshu': '小红书预填；需要专用浏览器，最后点击由作者完成',
                        'douyin': '抖音视频；需要登录的创作者后台与 Agent 浏览器工具',
                        'bilibili': 'B 站视频；需要登录的创作中心与 Agent 浏览器工具',
                        'tencent_video': '腾讯视频；需要登录的创作后台与 Agent 浏览器工具',
                    }
                    for platform in PLATFORMS:
                        if platform not in platforms and input(f'{descriptions[platform]}，配置吗？[y/N] ').strip().lower() in ('y', 'yes', '是'):
                            platforms.append(platform)
                    if platforms:
                        args.account = input('本地账号别名 [main]：').strip() or 'main'
                    social_platforms = [p for p in platforms if p != 'wechat']
                    if social_platforms:
                        args.browser_profile = args.browser_profile or input('专用浏览器用户目录（留空则等待配置）：').strip() or None
                        for platform in social_platforms:
                            if platform not in identities:
                                identity = input(f'{platform} 公开账号身份（主页标识或 handle，留空则等待配置）：').strip()
                                if identity:
                                    identities[platform] = identity
                    if 'wechat' in platforms:
                        args.write_scripts = input('Write scripts 路径（留空自动发现）：').strip() or None
                        args.wechat_env_file = input('私有 env 文件路径（留空使用进程环境变量）：').strip() or None
                        appid = input('确认的公众号 AppID（仅存 SHA-256，不输入 AppSecret）：').strip()
                        args.wechat_appid_sha256 = hashlib.sha256(appid.encode()).hexdigest() if appid else None
                result = initialize(paths['profile'], platforms=platforms, account=args.account,
                                    write_scripts=args.write_scripts, env_file=args.wechat_env_file,
                                    appid_sha256=args.wechat_appid_sha256, state_dir=paths['state_dir'],
                                    browser_profile=args.browser_profile, public_identities=identities)
                result['diagnosis'] = doctor(resolve_paths(paths['profile'], paths['state_dir']))
                result['configuration_ready'] = result['diagnosis']['ok']
                if not result['configuration_ready']:
                    result['next_action'] = '配置文件已建立但仍待填写；按诊断补齐同一文件后重新运行 --doctor'
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get('ok') is False or result.get('diagnosis', {}).get('ok') is False:
            return 2
        return 0
    except (EnvironmentError, EOFError) as exc:
        message = str(exc) if isinstance(exc, EnvironmentError) else '输入已结束；没有完成初始化'
        print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
