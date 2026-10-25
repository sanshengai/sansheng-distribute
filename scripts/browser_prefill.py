#!/usr/bin/env python3
"""Optional bounded prefill steps inside an existing Agent execute lease.

No navigation, arbitrary JavaScript, submit action, or automatic reply. The
Agent still checks native topics, declarations, cover and independent readback.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import agent_adapter
from environment import load_profile
from platform_pack import build_pack
from publish_core import PublishError, digest, file_hash, read_json, write_json
from social_adapter import _account, work_path

ORIGINS = {
    'x': {'https://x.com'},
    'douyin': {'https://creator.douyin.com'},
    'bilibili': {'https://member.bilibili.com'},
    'wechat_channels': {'https://channels.weixin.qq.com'},
    'xiaohongshu': {'https://creator.xiaohongshu.com'},
}


def browser_arguments(session):
    try:
        return session.send('Browser.getBrowserCommandLine')['arguments']
    except Exception:
        # The existing user profile may intentionally omit enable-automation.
        # Resolve the connected browser's own PID, not an unrelated Chrome.
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


def bind(request_path, profile):
    task = agent_adapter.inspect(request_path)
    if task['action'] != 'execute':
        raise PublishError('预填只能使用当前 execute 接力，不能在只读恢复阶段写入')
    request = read_json(request_path)
    if request != task['request']:
        raise PublishError('接力请求已变化')
    manifest = read_json(request['manifest_path'])
    if digest(manifest) != request['manifest_digest']:
        raise PublishError('封存清单已变化')
    pack = build_pack(manifest, request['target']['id'], profile)
    if request['target'] != next(t for t in manifest['targets'] if t['id'] == pack['target_id']):
        raise PublishError('请求目标与封存目标不符')
    identity, browser = _account(profile, request['target'])
    work = read_json(work_path(request_path))
    if (work.get('pack_digest') != digest(pack) or work.get('pack') != pack
            or work.get('expected_account_identity') != identity
            or work.get('browser_profile') != browser):
        raise PublishError('工作单、账号或专用浏览器绑定不符')
    return task, pack, identity, browser


def make_steps(pack, recipe):
    """Recipes come from trusted private config, never from article content."""
    if not isinstance(recipe, dict) or recipe.get('enabled') is not True:
        raise PublishError('未启用本平台的私有预填配方')
    if not recipe.get('page_path') or not recipe.get('identity_selector'):
        raise PublishError('配方需要精确页面路径和账号身份定位器')
    result = []
    assets = {a['id']: a for a in pack['assets']}
    steps = recipe.get('steps')
    if not isinstance(steps, list) or not steps:
        raise PublishError('配方步骤不能为空')
    for step in steps:
        if not isinstance(step, dict) or set(step) - {'action', 'selector', 'field', 'asset_id'}:
            raise PublishError('配方含未知步骤字段')
        action, selector = step.get('action'), step.get('selector')
        if action not in {'fill', 'upload', 'wait_visible'} or not isinstance(selector, str) or not selector:
            raise PublishError('仅支持填字、上传和等待；最终按钮及原生话题由 Agent 核对')
        item = {'action': action, 'selector': selector}
        if action == 'fill':
            field = step.get('field')
            if field not in {'title', 'text', 'description'}:
                raise PublishError('文字仅可引用封存发布包的 title/text/description')
            item['value'] = pack['fields'][field]
        if action == 'upload':
            asset = assets.get(step.get('asset_id'))
            if not asset or asset['kind'] not in {'video', 'image'}:
                raise PublishError('上传素材不在封存发布包中或类型不支持')
            item['value'] = asset['absolute_path']
            item['sha256'] = asset['sha256']
        result.append(item)
    return result


def assert_page(page, pack, recipe):
    url = urlsplit(page.url)
    if f'{url.scheme}://{url.netloc}' not in ORIGINS.get(pack['platform'], set()):
        raise PublishError('页面来源与发布平台不符')
    if url.path != recipe['page_path']:
        raise PublishError('当前不是配方绑定的编辑页面')


def perform(page, steps, guard):
    timings = []
    for index, step in enumerate(steps):
        guard()
        start = time.monotonic()
        node = page.locator(step['selector'])
        if node.count() != 1:
            raise PublishError(f'步骤 {index + 1} 定位不唯一；停止交给 Agent')
        if step['action'] == 'fill':
            node.fill(step['value'], timeout=30000)
            # Readback supports input/textarea and contenteditable.
            tag = node.evaluate('(el) => el.tagName')
            actual = node.input_value() if tag in {'INPUT', 'TEXTAREA'} else node.inner_text()
            if actual != step['value']:
                raise PublishError(f'步骤 {index + 1} 文字读回不符')
        elif step['action'] == 'upload':
            if file_hash(Path(step['value'])) != step['sha256']:
                raise PublishError('上传前素材摘要变化；交回制作端重新封存')
            node.set_input_files(step['value'], timeout=30000)
        else:
            node.wait_for(state='visible', timeout=120000)
        timings.append({'index': index + 1, 'action': step['action'],
                        'seconds': round(time.monotonic() - start, 3)})
    return timings


def execute(request_path, profile):
    task, pack, identity, browser = bind(request_path, profile)
    cfg = profile.get('browser_prefill', {})
    recipe = cfg.get('recipes', {}).get(pack['platform'], {})
    steps = make_steps(pack, recipe)
    endpoint = cfg.get('cdp_url', '')
    parts = urlsplit(endpoint)
    if (parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', 'localhost'}
            or not parts.port or parts.path not in {'', '/'} or parts.query or parts.fragment
            or parts.username or parts.password):
        raise PublishError('仅连接显式配置的本机专用 CDP 端点')
    marker = Path(request_path).with_suffix('.prefill-attempt.json')
    # A receipt is deliberately irreversible by this helper. Even a timeout may
    # follow an accepted upload. Agent must inspect the same page before retry.
    if marker.exists():
        raise PublishError('已尝试预填；先检查原页面，禁止自动重复上传')
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PublishError('可选依赖未安装：使用现有 Playwright 环境或由 Agent 原有浏览器工具执行') from exc
    with sync_playwright() as pw:
        connection = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        # Never restart Chrome just to obtain its profile binding.
        session = connection.new_browser_cdp_session()
        try:
            arguments = browser_arguments(session)
        except Exception as exc:
            raise PublishError('不能核对实际 Chrome profile；保留当前实例，由 Agent 原有工具接管') from exc
        profiles = [a.partition('=')[2] for a in arguments if a.startswith('--user-data-dir=')]
        if len(profiles) != 1 or Path(profiles[0]).resolve() != Path(browser).resolve():
            raise PublishError('实际连接的 Chrome 不是约定的专用 profile')
        candidates = []
        for context in connection.contexts:
            for page in context.pages:
                try:
                    assert_page(page, pack, recipe)
                except PublishError:
                    continue
                candidates.append(page)
        if len(candidates) != 1:
            raise PublishError('编辑页不是唯一；请 Agent 选择并关闭同平台重复编辑页')
        page = candidates[0]

        def guard():
            live = agent_adapter.inspect(request_path)
            if live['nonce'] != task['nonce'] or live['action'] != 'execute':
                raise PublishError('接力阶段已变化；停止写入')
            assert_page(page, pack, recipe)
            node = page.locator(recipe['identity_selector'])
            if node.count() != 1 or node.inner_text().strip() != identity:
                raise PublishError('实际账号身份与私有配置不符')

        guard()
        with marker.open('x', encoding='utf-8') as out:
            json.dump({'pack_digest': digest(pack), 'nonce': task['nonce'], 'state': 'started'}, out)
        timings = perform(page, steps, guard)
        receipt = {'pack_digest': digest(pack), 'nonce': task['nonce'], 'state': 'steps_completed',
                   'timings': timings, 'requires_agent_readback': True,
                   'remaining': ['上传完成状态', '原生话题', '封面', '合集/活动', '原创及AI声明', '独立读回']}
        write_json(marker, receipt)
        # Do not call browser.close(): it would close the user's dedicated Chrome.
        return receipt


def main():
    parser = argparse.ArgumentParser(description='可选浏览器预填助手；永不点击发表')
    parser.add_argument('request'); parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps({'ok': True, 'result': execute(args.request, load_profile(args.profile))}, ensure_ascii=False))
    except Exception as exc:
        # Browser exceptions may contain article text or file paths; don't echo.
        message = str(exc) if isinstance(exc, PublishError) else f'预填停止：{type(exc).__name__}；检查原编辑页'
        print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False)); return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
