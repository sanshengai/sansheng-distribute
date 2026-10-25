#!/usr/bin/env python3
"""Pinned optional biliup installer/probe/login and lossless upload planning.

This module plans commands only. Real trials must retain the dispatch ledger,
account lock and independent readback; a CLI exit code is not publication proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from environment import load_profile
from platform_pack import build_pack
from publish_core import PublishError, read_json

VERSION = '1.3.4'
ASSET = 'biliupR-v1.3.4-aarch64-macos.tar.xz'
SHA256 = '1bfdb13a092600907e6111c0ed08c30af4abe5c8ed0b041e6d98b94efe376d46'
URL = f'https://github.com/biliup/biliup/releases/download/v{VERSION}/{ASSET}'


def install(directory):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise PublishError('当前固定包仅适用于 Apple Silicon macOS')
    root = Path(directory).expanduser().resolve()
    if root.exists():
        raise PublishError('安装目录已存在；拒绝覆盖，请先 probe 已有版本')
    # Verify before extracting; extract only the single executable, never paths.
    with tempfile.TemporaryDirectory(prefix='sansheng-biliup-') as temporary:
        archive = Path(temporary) / ASSET
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open('wb') as out:
            while block := response.read(1024 * 1024):
                out.write(block)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise PublishError('下载包 SHA-256 与固定上游摘要不符')
        with tarfile.open(archive, 'r:xz') as tar:
            members = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == 'biliup']
            if len(members) != 1 or members[0].size > 150 * 1024 * 1024:
                raise PublishError('上游压缩包结构不符')
            executable = tar.extractfile(members[0]).read()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        binary = root / 'biliup'
        binary.write_bytes(executable); binary.chmod(0o700)
        receipt = {'version': VERSION, 'source': URL, 'archive_sha256': SHA256,
                   'binary_sha256': hashlib.sha256(executable).hexdigest()}
        (root / 'install.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return probe(root)


def probe(directory):
    root = Path(directory).expanduser().resolve()
    binary = root / 'biliup'
    receipt = read_json(root / 'install.json')
    if (receipt.get('version') != VERSION or receipt.get('source') != URL
            or receipt.get('archive_sha256') != SHA256
            or hashlib.sha256(binary.read_bytes()).hexdigest() != receipt.get('binary_sha256')):
        raise PublishError('CLI 安装回执或二进制摘要不符；不运行')
    result = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=15)
    if result.returncode != 0 or result.stdout.strip() != f'biliup-cli {VERSION}':
        raise PublishError('CLI 版本实测不符')
    return {'binary': str(binary), 'version': VERSION, 'integrity_verified': True,
            'upload_enabled': False, 'login_required': True}


def login(directory, credentials):
    binary = probe(directory)['binary']
    path = Path(credentials).expanduser().resolve()
    if path.exists():
        raise PublishError('登录凭证文件已存在；拒绝覆盖，请先核对现有登录')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The QR code is for the user. Credentials stay in this explicit private path.
    old_mask = os.umask(0o077)
    try:
        code = subprocess.run([binary, '-u', str(path), 'login']).returncode
    finally:
        os.umask(old_mask)
    if code or not path.is_file():
        raise PublishError('CLI 登录未完成；没有执行上传')
    path.chmod(0o600)
    return {'credentials_saved': True, 'upload_executed': False,
            'next': '核对实际账号 UID 与私有配置，再完成字段覆盖试验'}


def identity(credentials, expected_uid):
    """Read-only account check; never print cookies, tokens, or response body."""
    path = Path(credentials).expanduser().resolve()
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise PublishError('需要权限为 0600 的私有凭证文件')
    if not isinstance(expected_uid, str) or not expected_uid.isdigit():
        raise PublishError('需要已核实的预期 B 站 UID')
    value = read_json(path)
    cookies = value.get('cookie_info', {}).get('cookies', [])
    if not isinstance(cookies, list) or not cookies:
        raise PublishError('CLI 凭证缺少网页 Cookie；不自动转换其他登录文件')
    pairs = []
    for item in cookies:
        name, content = item.get('name'), item.get('value')
        if (not isinstance(name, str) or not isinstance(content, str)
                or any(c in name + content for c in '\r\n;') or not name):
            raise PublishError('凭证字段格式不符')
        pairs.append(name + '=' + content)
    request = urllib.request.Request('https://api.bilibili.com/x/web-interface/nav',
        headers={'Cookie': '; '.join(pairs), 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    data = result.get('data', {})
    if result.get('code') != 0 or data.get('isLogin') is not True:
        raise PublishError('CLI 登录已失效；未执行任何上传')
    if str(data.get('mid')) != expected_uid:
        raise PublishError('CLI 实际登录 UID 与预期账号不符；停止')
    return {'authenticated': True, 'expected_uid_matches': True, 'upload_executed': False}


def plan(pack, directory, credentials, tid):
    """Build argv only; reject fields the current CLI route cannot preserve."""
    if pack.get('platform') != 'bilibili' or pack.get('content_type') != 'video':
        raise PublishError('只接受 B 站视频发布包')
    if pack.get('endpoint') != 'publish':
        raise PublishError('CLI 投稿不能代替草稿或预填终点')
    if type(tid) is not int or tid <= 0:
        raise PublishError('分区 tid 必须来自实际分区查询')
    metadata = pack.get('platform_metadata', {})
    if set(metadata) - {'copyright', 'ai_generated'} or pack.get('subtitle_asset_ids'):
        raise PublishError('当前 CLI 桥未覆盖平台扩展字段或字幕；保留浏览器路线，不静默丢字段')
    if metadata.get('copyright') != 1:
        raise PublishError('试验计划须显式确认 platform_metadata.copyright=1 的自制权利；不自动认定原创')
    if 'ai_generated' in metadata and type(metadata['ai_generated']) is not bool:
        raise PublishError('ai_generated 必须为布尔值；不能把未知声明当作未使用 AI')
    assets = pack.get('assets', [])
    videos = [a for a in assets if a['kind'] == 'video']
    cover = pack.get('cover_asset_id')
    if len(videos) != 1 or any(a['kind'] != 'video' and a['id'] != cover for a in assets):
        raise PublishError('当前试验仅支持单视频及可选封面')
    fields = pack['fields']
    if fields.get('text'):
        raise PublishError('B 站 CLI 无独立 text 字段；请在制作端明确 description')
    title = fields.get('title', '')
    if fields.get('links'):
        raise PublishError('当前 CLI 桥尚未覆盖独立 links 字段；不静默丢链接')
    tags = fields.get('tags', [])
    if not title.strip() or not tags or any(',' in t or '\n' in t for t in tags):
        raise PublishError('需要明确标题和无逗号换行的标签')
    binary = probe(directory)['binary']
    args = [binary, '-u', str(Path(credentials).expanduser().resolve()), 'upload',
            '--submit', 'web', '--limit', '1', '--copyright', '1', '--tid', str(tid),
            '--title', title, '--desc', fields.get('description', ''), '--tag', ','.join(tags)]
    if metadata.get('ai_generated') is True:
        args += ['--extra-fields', json.dumps({'creation_statement': {'id': 1}})]
    if cover:
        images = [a for a in assets if a['id'] == cover and a['kind'] == 'image']
        if len(images) != 1:
            raise PublishError('封面素材不符')
        args += ['--cover', images[0]['absolute_path']]
    args += ['--', videos[0]['absolute_path']]
    return {'argv': args, 'executable_plan_only': True, 'upload_enabled': False,
            'unverified': ['本次登录账号 UID', '本次真实投稿及AI声明读回', '独立作品页读回']}


def main():
    parser = argparse.ArgumentParser(description='可选 B 站 CLI 试验；本桥不执行投稿')
    parser.add_argument('action', choices=['install', 'probe', 'login', 'identity', 'plan'])
    parser.add_argument('--directory', required=True)
    parser.add_argument('--credentials'); parser.add_argument('--manifest')
    parser.add_argument('--target'); parser.add_argument('--profile'); parser.add_argument('--tid', type=int)
    parser.add_argument('--expected-uid')
    args = parser.parse_args()
    try:
        if args.action == 'install': result = install(args.directory)
        elif args.action == 'probe': result = probe(args.directory)
        elif args.action == 'login':
            if not args.credentials: raise PublishError('需要私有 --credentials 路径')
            result = login(args.directory, args.credentials)
        elif args.action == 'identity':
            if not args.credentials: raise PublishError('需要私有 --credentials 路径')
            result = identity(args.credentials, args.expected_uid)
        else:
            if not all((args.manifest, args.target, args.profile, args.credentials, args.tid)):
                raise PublishError('plan 需要 manifest、target、profile、credentials 和 tid')
            pack = build_pack(read_json(args.manifest), args.target, load_profile(args.profile))
            result = plan(pack, args.directory, args.credentials, args.tid)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2)); return 0
    except (PublishError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc) if isinstance(exc, PublishError) else type(exc).__name__}, ensure_ascii=False)); return 2


if __name__ == '__main__':
    sys.exit(main())
