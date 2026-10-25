#!/usr/bin/env python3
"""Controlled pinned CLI upload with durable IDs and independent Bilibili readback.

This uses third-party web protocols, not Bilibili's institutional Open API.
No retry path uploads again. Unknown submissions retain their private evidence.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
import urllib.request

from biliup_bridge import plan
from environment import load_profile
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json

MAX_LOG = 16 * 1024 * 1024
ACCEPTED = re.compile(r'ResponseData \{ code: 0, data: Some\(Object \{"aid": Number\((\d+)\), "bvid": String\("(BV[A-Za-z0-9]+)"\)')
RISK = re.compile(r'限流|captcha|验证码|HTTP[/ ]\S*\s+412|code:\s*-(?:352|412)|Too Many Requests', re.I)


def settings(profile, target):
    account = profile.get('accounts', {}).get(target['account'], {})
    if target['platform'] != 'bilibili' or 'bilibili' not in account.get('platforms', []):
        raise PublishError('B站账号或平台绑定不符')
    uid = account.get('public_identities', {}).get('bilibili')
    if not isinstance(uid, str) or not uid.isdigit():
        raise PublishError('B站 public_identities.bilibili 必须为已确认 UID')
    cfg = profile.get('adapters', {}).get('bilibili', {}).get('cli', {})
    if not isinstance(cfg, dict) or set(cfg) - {'directory', 'credentials', 'tid', 'timeout_seconds'}:
        raise PublishError('B站 cli 配置字段无效')
    for key in ('directory', 'credentials'):
        if not isinstance(cfg.get(key), str) or not Path(cfg[key]).is_absolute():
            raise PublishError('B站 CLI 工具与凭证须为显式绝对路径')
    if type(cfg.get('tid')) is not int or cfg['tid'] <= 0:
        raise PublishError('B站 cli.tid 须为已查询的正整数分区')
    timeout = cfg.get('timeout_seconds', 1500)
    if type(timeout) is not int or not 30 <= timeout <= 1800:
        raise PublishError('B站 CLI 超时须为 30 至 1800 秒')
    return cfg, uid


def credential_header(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise PublishError('需要权限为 0600 的 B站私有凭证')
    cookies = read_json(path).get('cookie_info', {}).get('cookies')
    if not isinstance(cookies, list) or not cookies:
        raise PublishError('B站凭证缺少网页 Cookie')
    values = []
    for item in cookies:
        if not isinstance(item, dict): raise PublishError('B站凭证字段格式不符')
        name, value = item.get('name'), item.get('value')
        if (not isinstance(name, str) or not name or not isinstance(value, str)
                or any(c in name + value for c in '\r\n;') or '=' in name):
            raise PublishError('B站凭证字段格式不符')
        values.append(name + '=' + value)
    return '; '.join(values)


class Reader:
    def __init__(self, credentials, uid):
        self.cookie = credential_header(credentials)
        self.uid = uid

    def get(self, kind, **params):
        endpoints = {
            'identity': 'https://api.bilibili.com/x/web-interface/nav',
            'archive': 'https://member.bilibili.com/x/vupre/web/archive/view',
            'public': 'https://api.bilibili.com/x/web-interface/view',
        }
        if kind not in endpoints: raise PublishError('不支持的 B站只读查询')
        headers = {'User-Agent':'Mozilla/5.0', 'Referer':'https://member.bilibili.com/'}
        if kind != 'public': headers['Cookie'] = self.cookie
        request = urllib.request.Request(endpoints[kind] + ('?' + urlencode(params) if params else ''), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.load(response)
        except Exception as exc:
            raise PublishError('B站只读查询未完成；保留原稿，不更换身份重试') from exc
        if not isinstance(result, dict) or result.get('code') != 0 or not isinstance(result.get('data'), dict):
            raise PublishError('B站只读查询未通过；不把接口错误当作作品不存在')
        return result['data']

    def identity(self):
        value = self.get('identity')
        if value.get('isLogin') is not True or str(value.get('mid')) != self.uid:
            raise PublishError('B站登录失效或实际 UID 不符；停止')


def duration(pack):
    binary = shutil.which('ffprobe')
    if not binary: raise PublishError('B站视频核验需要 ffprobe；未上传')
    video = next(a for a in pack['assets'] if a['kind'] == 'video')
    result = subprocess.run([binary, '-v', 'error', '-show_entries', 'format=duration', '-of',
        'json', video['absolute_path']], capture_output=True, text=True, timeout=30)
    try: value = float(json.loads(result.stdout)['format']['duration'])
    except (ValueError, KeyError, TypeError) as exc: raise PublishError('无法读取成片时长；未上传') from exc
    if result.returncode or not math.isfinite(value) or value <= 0:
        raise PublishError('成片时长无效；未上传')
    return value


def preflight_bilibili(manifest, target, profile, argv):
    cfg, uid = settings(profile, target)
    outer_timeout = profile['adapters']['bilibili'].get('timeout_seconds', 300)
    if (type(outer_timeout) not in (int, float) or not math.isfinite(outer_timeout)
            or outer_timeout < cfg.get('timeout_seconds', 1500) + 180):
        raise PublishError('B站外层 timeout_seconds 须覆盖 CLI 时限及 180 秒核验清理预算')
    if '--profile' not in argv or argv.index('--profile') + 1 >= len(argv):
        raise PublishError('B站适配器需要 --profile')
    selected = load_profile(argv[argv.index('--profile') + 1])
    if settings(selected, target) != (cfg, uid) or selected['accounts'][target['account']] != profile['accounts'][target['account']]:
        raise PublishError('B站适配器与调度器账号或通道配置不一致')
    pack = build_pack(manifest, target['id'], profile)
    if type(pack['platform_metadata'].get('ai_generated')) is not bool:
        raise PublishError('B站执行器需要显式 ai_generated 布尔声明；未知时交回确认')
    command = plan(pack, cfg['directory'], cfg['credentials'], cfg['tid'])
    credential_header(cfg['credentials'])
    duration(pack)
    return {'local_only':True, 'argv_validated':bool(command['argv'])}


def receipt_path(request_path):
    return Path(request_path).with_suffix('.bilibili-attempt.json')


def stage_sources(pack, request_path):
    """The producer may keep editing. CLI reads only verified private snapshots."""
    root = Path(request_path).with_suffix('.bilibili-assets')
    root.mkdir(mode=0o700, exist_ok=False)
    staged = copy.deepcopy(pack)
    try:
        for asset in staged['assets']:
            source = Path(asset['absolute_path'])
            target = root/(asset['id'] + source.suffix.lower())
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            h = hashlib.sha256()
            with os.fdopen(fd, 'wb') as output, source.open('rb') as incoming:
                while block := incoming.read(1024*1024): h.update(block); output.write(block)
                output.flush(); os.fsync(output.fileno())
            if h.hexdigest() != asset['sha256']:
                raise PublishError('B站上传素材在核对期间改变；未上传，交回制作端重新封存')
            target.chmod(0o400)
            asset['absolute_path'] = str(target)
    except Exception:
        # This exact newly created directory belongs to this invocation only.
        shutil.rmtree(root)
        raise
    return staged


def parse_log(path):
    path = Path(path)
    if not path.is_file(): return None, None, False
    if path.stat().st_size > MAX_LOG: raise PublishError('CLI 诊断超过有界范围；只读检查原稿')
    text = path.read_text(errors='replace')
    accepted = set(ACCEPTED.findall(text))
    if len(accepted) > 1: raise PublishError('CLI 返回多个不同稿件身份；禁止猜测')
    covers = set(re.findall(r'https?://[^\s"<>]*\.hdslb\.com/bfs/archive/[a-fA-F0-9]+\.[A-Za-z0-9]+', text))
    return (dict(zip(('aid', 'bvid'), next(iter(accepted)))) if accepted else None,
            next(iter(covers)) if len(covers) == 1 else None, bool(RISK.search(text)))


def run_cli(command, request_path, receipt, fd, timeout):
    marker = receipt_path(request_path)
    if marker.exists(): raise PublishError('B站已尝试执行；仅允许只读恢复，不重复上传')
    log = Path(request_path).with_suffix('.bilibili-private.log')
    # O_EXCL and 0600 are established before starting a child with network writes.
    out_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(out_fd, 'wb') as stream:
        write_json(marker, receipt)
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, pass_fds=(fd,),
            env=dict(os.environ, NO_COLOR='1', RUST_LOG='info'))
        deadline = time.monotonic() + timeout
        stopped = None
        try:
            while True:
                accepted, cover, risk = parse_log(log)
                if accepted:
                    receipt.update(accepted); receipt['cover_url'] = cover
                    write_json(marker, receipt)  # Persist IDs even before the CLI exits.
                if risk: stopped = 'platform_verification_or_rate_limit'; break
                if process.poll() is not None: break
                if time.monotonic() >= deadline: stopped = 'timeout'; break
                time.sleep(.2)
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        accepted, cover, _ = parse_log(log)
        if accepted: receipt.update(accepted)
        if cover: receipt['cover_url'] = cover
        receipt.update(exit_code=process.returncode, stopped=stopped)
        write_json(marker, receipt)
    if not receipt.get('aid'):
        raise PublishError('B站投稿结果未知；保留原日志，只查询，不重新上传')
    return receipt


def same_cover(first, second):
    def key(value):
        if not isinstance(value, str): return None
        p = urlsplit(value)
        return (p.hostname, p.path) if p.scheme in {'http','https'} and p.hostname and p.hostname.endswith('.hdslb.com') and not p.query and not p.fragment else None
    return key(first) is not None and key(first) == key(second)


def verify_content(pack, receipt, data, uid, tid):
    recorded_duration = receipt.get('duration_seconds')
    if (type(recorded_duration) not in (int, float) or not math.isfinite(recorded_duration)
            or recorded_duration <= 0):
        raise PublishError('B站提交凭据中的成片时长无效')
    archive = data.get('archive', {}); videos = data.get('videos', [])
    if not isinstance(archive, dict) or not isinstance(videos, list): raise PublishError('B站原稿结构不符')
    fields = pack['fields']
    required = {'aid':receipt['aid'], 'bvid':receipt['bvid'], 'mid':uid,
                'title':fields['title'], 'desc':fields['description'], 'copyright':1, 'tid':tid}
    for key, expected in required.items():
        actual = str(archive.get(key)) if key in {'aid','mid'} else archive.get(key)
        if actual != expected: raise PublishError('B站原稿字段读回不符：' + key)
    if set(str(archive.get('tag', '')).split(',')) != set(fields['tags']):
        raise PublishError('B站原生标签读回不符')
    declaration = archive.get('creation_statement') or {}
    expected_ai = pack['platform_metadata'].get('ai_generated')
    if expected_ai is True and declaration.get('id') != 1: raise PublishError('B站 AI 声明读回不符')
    if expected_ai is False and declaration.get('id') == 1: raise PublishError('B站 AI 声明与明确选择不符')
    if len(videos) != 1 or not videos[0].get('cid'):
        raise PublishError('B站视频数量或播放身份读回不符')
    try: observed_duration = float(videos[0]['duration'])
    except (KeyError, TypeError, ValueError) as exc: raise PublishError('B站视频缺少可核验时长') from exc
    if not math.isfinite(observed_duration) or abs(observed_duration - recorded_duration) > 3:
        raise PublishError('B站视频时长与原片不符')
    cover_ok = not pack.get('cover_asset_id') or same_cover(receipt.get('cover_url'), archive.get('cover'))
    return archive, videos[0], cover_ok


def dispatch(request_path, action, profile, fd):
    request = read_json(request_path); manifest = read_json(request['manifest_path'])
    if digest(manifest) != request['manifest_digest']: raise PublishError('B站封存快照变化')
    target = next((x for x in manifest.get('targets', []) if x['id'] == request['target']['id']), None)
    if target != request['target']: raise PublishError('B站请求目标与封存目标不符')
    cfg, uid = settings(profile, target)
    if action == 'execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('B站账号受限，仅准备本地包')
    pack = build_pack(manifest, target['id'], profile, check_files=action == 'execute')
    if action == 'execute' and type(pack['platform_metadata'].get('ai_generated')) is not bool:
        raise PublishError('B站执行器需要显式 ai_generated 布尔声明')
    marker = receipt_path(request_path)
    binding = {'manifest_digest':digest(manifest), 'pack_digest':digest(pack), 'uid':uid}
    reader = Reader(cfg['credentials'], uid); reader.identity()
    if action == 'execute':
        # Reject unsupported fields before any snapshot/CLI preparation.
        plan(pack, cfg['directory'], cfg['credentials'], cfg['tid'])
        staged = stage_sources(pack, request_path)
        command = plan(staged, cfg['directory'], cfg['credentials'], cfg['tid'])['argv']
        receipt = dict(binding, duration_seconds=duration(staged),
            uploaded_assets=[{'id':a['id'], 'sha256':a['sha256']} for a in staged['assets']])
        receipt = run_cli(command, request_path, receipt, fd, cfg.get('timeout_seconds', 1500))
    else:
        if not marker.is_file(): raise PublishError('B站没有本次提交凭据；禁止猜稿或重新上传')
        receipt = read_json(marker)
        if any(receipt.get(k) != v for k, v in binding.items()): raise PublishError('B站恢复凭据与当前账号或内容不符')
        if not receipt.get('aid'):
            accepted, cover, _ = parse_log(Path(request_path).with_suffix('.bilibili-private.log'))
            if not accepted: raise PublishError('B站缺少已受理稿件 ID；保持未知，不按标题猜测')
            receipt.update(accepted, cover_url=cover); write_json(marker, receipt)
    observation = {k:request[k] for k in ('manifest_digest',)}
    observation.update(target_id=target['id'], platform='bilibili', account=target['account'],
        state='pending', remote_id=receipt['aid'], remote_url='https://www.bilibili.com/video/'+receipt['bvid']+'/',
        observed_account_identity=uid)
    if action == 'execute': return observation
    if request['job'].get('remote_id') and request['job']['remote_id'] != receipt['aid']:
        raise PublishError('B站远端稿件身份改变')
    data = reader.get('archive', aid=receipt['aid'])
    archive, video, cover_ok = verify_content(pack, receipt, data, uid, cfg['tid'])
    evidence = {'archive':{k:archive.get(k) for k in ('aid','bvid','mid','title','desc','tag','cover','copyright','tid','state','creation_statement')},
                'video':{k:video.get(k) for k in ('cid','duration')}, 'cover_verified':cover_ok, 'public_verified':False}
    if archive.get('state') == 0 and cover_ok:
        try: public = reader.get('public', bvid=receipt['bvid'])
        except PublishError: public = {}
        if (str(public.get('aid')) == receipt['aid'] and public.get('bvid') == receipt['bvid']
                and str((public.get('owner') or {}).get('mid')) == uid and public.get('title') == pack['fields']['title']
                and public.get('state') == 0 and str(public.get('cid')) == str(video['cid'])):
            evidence['public_verified'] = True
            observation.update(state='published', content_verified=True, verified_pack_digest=digest(pack),
                verified_asset_ids=[a['id'] for a in pack['assets']])
    evidence_path = Path(request_path).with_suffix('.bilibili-readback.json')
    write_json(evidence_path, evidence); observation['evidence_path'] = str(evidence_path)
    observation['content_verified'] = observation.get('content_verified', False)
    return observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('execute','verify')); parser.add_argument('--request', required=True)
    parser.add_argument('--profile', required=True); args = parser.parse_args()
    try:
        fd = int(os.environ['SANDY_PUBLISH_LOCK_FD']); os.fstat(fd)
        print(json.dumps(dispatch(args.request, args.action, load_profile(args.profile), fd), ensure_ascii=False)); return 0
    except Exception as exc:
        print(json.dumps({'ok':False, 'error':str(exc) if isinstance(exc, PublishError) else 'B站适配器停止；只读核对原稿'}, ensure_ascii=False), file=sys.stderr); return 2


if __name__ == '__main__': sys.exit(main())
