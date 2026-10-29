#!/usr/bin/env python3
"""Opt-in, bounded read probe for the official Weibo assistant; no social writes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

from interaction_import import _safe_state_dir
from publish_core import PublishError, now
from weibo_comments_capture import _id, _private_json, _text

HOST = 'https://open-im.api.weibo.com'
READ_PATHS = {'received': '/open/interactive/comments/to_me',
              'work': '/open/interactive/comments/show'}


def validate_config(raw):
    fields = {'schema', 'enabled', 'account', 'uid', 'credentials', 'work_ids'}
    if not isinstance(raw, dict) or set(raw) != fields or raw['schema'] != 'weibo-official-probe.v1':
        raise PublishError('微博官方只读试验配置无效')
    if raw['enabled'] is not True:
        raise PublishError('微博官方只读试验尚未显式启用')
    _text(raw['account'], 256)
    _id(raw['uid'])
    credential_path = Path(_text(raw['credentials'], 4096))
    if not credential_path.is_absolute():
        raise PublishError('微博凭据文件须为私有绝对路径')
    if credential_path.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise PublishError('微博凭据不能保存在公开 Skill 仓内')
    works = raw['work_ids']
    if not isinstance(works, list) or not 1 <= len(works) <= 5:
        raise PublishError('仅允许显式配置 1 至 5 个已核实本人作品 ID')
    for work in works:
        _id(work)
    if len(set(works)) != len(works):
        raise PublishError('微博官方试验作品 ID 不得重复')
    return raw


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, request=None):
        self.request = request or self._request
        self.token = None

    @staticmethod
    def _request(method, path, params):
        if (method, path) not in {('POST', '/open/auth/ws_token'),
                                  *(('GET', value) for value in READ_PATHS.values())}:
            raise PublishError('微博只读试验拒绝未列明的路径或写入方法')
        body = json.dumps(params).encode() if method == 'POST' else None
        url = HOST + path
        if method == 'GET':
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=body, method=method,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=20) as response:
                payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise PublishError('微博响应超出试验体积上限')
            return json.loads(payload)
        except urllib.error.HTTPError as exc:
            raise PublishError(f'微博官方只读请求 HTTP {exc.code}；未重试，不回显含令牌的请求地址') from None
        except (OSError, ValueError):
            raise PublishError('微博官方只读请求或解析失败；未重试，不回显凭据或响应正文') from None

    def login(self, credentials, expected_uid):
        if not isinstance(credentials, dict) or set(credentials) != {'app_id', 'app_secret'}:
            raise PublishError('微博私有凭据须仅含 app_id 与 app_secret')
        for name in ('app_id', 'app_secret'):
            _text(credentials[name], 4096)
        result = self.request('POST', '/open/auth/ws_token', credentials)
        if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0:
            raise PublishError('微博官方凭据校验失败；不回显返回的敏感信息')
        data = result.get('data')
        if not isinstance(data, dict):
            raise PublishError('微博官方授权响应字段未识别')
        uid = data.get('uid')
        if type(uid) is int:
            uid = str(uid)
        if _id(uid) != expected_uid:
            raise PublishError('微博官方凭据的 UID 与已配置账号不一致；未查询评论')
        self.token = _text(data.get('token'), 20000)

    def read(self, kind, work=None):
        if not self.token or kind not in READ_PATHS:
            raise PublishError('微博官方只读查询需要已核对身份且类型受支持')
        params = {'token': self.token}
        if kind == 'work':
            params['id'] = _id(work)
        result = self.request('GET', READ_PATHS[kind], params)
        if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0:
            raise PublishError('微博官方评论查询未成功；停止试验，不重试或调用写入接口')
        data = result.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('comments'), list):
            raise PublishError('微博官方评论响应结构未识别；不推断零评论')
        comments = data['comments']
        if len(comments) > 1000:
            raise PublishError('微博评论条数超出本次试验上限')
        if type(data.get('count')) is not int or data['count'] < 0:
            raise PublishError('微博官方评论计数字段未识别')
        # Keep only the declared comment data. Never persist auth responses or tokens.
        return {'kind': kind, 'work_id': work, 'comments': comments, 'count': data['count'],
                'response_fields': sorted(data), 'coverage': '待核对分页、父级及历史范围，不证明全量覆盖'}


def run(root, config, client=None):
    config = validate_config(config)
    root = _safe_state_dir(root)
    client = client or Client()
    stamp = now()
    filename = 'weibo-official-probe-' + stamp.replace(':', '').replace('.', '') + '.private.json'
    path = root / filename
    # Reserve the evidence file before any request; interrupted probes remain visible.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as evidence:
        document = {'schema': 'weibo-official-probe-result.v1', 'account': config['account'],
                    'uid': config['uid'], 'started_at': stamp, 'state': 'started',
                    'social_writes': 0, 'requests': [], 'automatic_sync': False}
        json.dump(document, evidence, ensure_ascii=False); evidence.flush(); os.fsync(evidence.fileno())
        try:
            credentials = _private_json(config['credentials'])
            client.login(credentials, config['uid'])
            for kind, work in [('received', None), *(('work', value) for value in config['work_ids'])]:
                result = client.read(kind, work)
                document['requests'].append(result)
                evidence.seek(0); json.dump(document, evidence, ensure_ascii=False)
                evidence.truncate(); evidence.flush(); os.fsync(evidence.fileno())
            document['state'] = 'read_probe_complete'
        except (PublishError, OSError, ValueError):
            document['state'] = 'failed'
            raise
        finally:
            client.token = None
            document['finished_at'] = now()
            evidence.seek(0); json.dump(document, evidence, ensure_ascii=False)
            evidence.truncate(); evidence.flush(); os.fsync(evidence.fileno())
    return {'state': document['state'], 'read_requests': len(document['requests']),
            'returned_rows': sum(len(row['comments']) for row in document['requests']),
            'evidence': str(path), 'automatic_sync': False, 'social_writes': 0}


def main():
    parser = argparse.ArgumentParser(description='微博官方助手评论只读试验；不安装插件、不写入社交内容')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.state_dir, _private_json(args.config)), ensure_ascii=False))
    except (PublishError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, PublishError) else
                          '微博官方只读试验失败；请检查私有文件'}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
