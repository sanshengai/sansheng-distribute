#!/usr/bin/env python3
"""Validate native Weibo inbox observations and import locally; never makes requests."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import sys

from interaction_import import import_manual
from publish_core import PublishError

SOURCE_URL = 'https://weibo.com/ajax/message/cmt'
MONTHS = dict(zip('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(), range(1, 13)))
FIELDS = {'id', 'root_id', 'parent_id', 'work_id', 'work_author', 'author_id',
          'author_name', 'created_at', 'text_html', 'restrict_state', 'disable_reply',
          'cut_tail', 'parent'}
PARENT_FIELDS = {'id', 'root_id', 'author_id', 'author_name', 'created_at', 'text_html'}


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value):
        raise PublishError('微博身份必须是原响应中的十进制字符串 ID')
    return value


def _text(value, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise PublishError('微博文字字段为空或格式无效')
    return value


def _iso(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise PublishError('时间必须是带时区的 ISO 时间') from None


def _created(value):
    # Native response format is English regardless of the local machine locale.
    match = re.fullmatch(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Z][a-z]{2}) '
                         r'(\d{2}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2}) (\d{4})',
                         _text(value, 80))
    if not match:
        raise PublishError('微博评论时间格式未识别')
    _, month, day, hour, minute, second, sign, zh, zm, year = match.groups()
    try:
        if int(zh) > 23 or int(zm) > 59:
            raise ValueError()
        offset = timedelta(hours=int(zh), minutes=int(zm)) * (-1 if sign == '-' else 1)
        return datetime(int(year), MONTHS[month], int(day), int(hour), int(minute), int(second),
                        tzinfo=timezone(offset)).astimezone(timezone.utc).isoformat()
    except (ValueError, KeyError):
        raise PublishError('微博评论时间无效') from None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'iframe', 'object'}:
            raise PublishError('评论 HTML 含不可导入内容')
        if tag == 'br':
            self.parts.append('\n')
        elif tag == 'img':
            self.parts.append(dict(attrs).get('alt', ''))

    def handle_endtag(self, tag):
        if tag in {'p', 'div'}:
            self.parts.append('\n')

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    parser = _PlainText()
    parser.feed(_text(value))
    parser.close()
    return _text(''.join(parser.parts).strip())


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) != {'schema', 'account', 'uid', 'not_before'}:
        raise PublishError('微博私有配置字段无效')
    if raw['schema'] != 'weibo-comment-config.v1':
        raise PublishError('微博私有配置版本无效')
    _text(raw['account'], 256)
    _id(raw['uid'])
    _iso(raw['not_before'])
    return raw


def normalize(capture, config):
    config = validate_config(config)
    keys = {'schema', 'account', 'uid', 'observed_at', 'source_url', 'total',
            'next_cursor', 'missing', 'items'}
    if not isinstance(capture, dict) or set(capture) != keys:
        raise PublishError('微博观察文件字段无效')
    if capture['schema'] != 'weibo-comment-capture.v1' or capture['source_url'] != SOURCE_URL:
        raise PublishError('不是已核实的微博收件箱响应来源')
    if capture['account'] != config['account'] or capture['uid'] != config['uid']:
        raise PublishError('观察账号与私有配置不一致')
    observed = _iso(capture['observed_at'])
    if observed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise PublishError('观察时间不能位于未来')
    if _iso(config['not_before']) > observed + timedelta(minutes=5):
        raise PublishError('历史基线截止时间不能晚于观察时间')
    for key in ('total', 'missing'):
        if type(capture[key]) is not int or capture[key] < 0:
            raise PublishError('微博统计字段无效')
    if not isinstance(capture['next_cursor'], str) or not re.fullmatch(r'[0-9]{1,20}', capture['next_cursor']):
        raise PublishError('微博游标无效')
    rows = capture['items']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200 or capture['total'] < len(rows):
        raise PublishError('观察须含 1 至 200 条实际评论；空输入不证明平台没有留言')
    normalized, seen, parent_evidence = [], set(), []
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise PublishError('微博评论字段不完整')
        for key in ('id', 'root_id', 'work_id', 'work_author', 'author_id'):
            _id(row[key])
        if row['work_author'] != config['uid']:
            raise PublishError('不导入非本人作品下的评论')
        if row['id'] in seen:
            raise PublishError('观察含重复评论 ID')
        seen.add(row['id'])
        root = row['id'] == row['root_id']
        if root:
            if row['parent_id'] is not None or row['parent'] is not None:
                raise PublishError('根评论不应有父级')
        elif (_id(row['parent_id']) == row['id']):
            raise PublishError('评论不能以自己为直接父级')
        if (type(row['restrict_state']) is not int or row['restrict_state'] != 0 or
                type(row['disable_reply']) is not int or row['disable_reply'] != 0 or
                row['cut_tail'] is not False):
            raise PublishError('评论受限、禁止回复或正文截断；仅保留原观察，不导入回复候选')
        body, published = plain_text(row['text_html']), _created(row['created_at'])
        if _iso(published) > observed + timedelta(minutes=5):
            raise PublishError('评论时间晚于观察时间')
        parent = row['parent']
        if parent is not None:
            if not isinstance(parent, dict) or set(parent) != PARENT_FIELDS:
                raise PublishError('父级上下文字段无效')
            for key in ('id', 'root_id', 'author_id'):
                _id(parent[key])
            if parent['id'] != row['parent_id'] or parent['root_id'] != row['root_id']:
                raise PublishError('直接父级或评论串身份不一致')
            parent_time = _created(parent['created_at'])
            if _iso(parent_time) > _iso(published):
                raise PublishError('父评论不能晚于子评论')
            parent_evidence.append({'comment_id': row['id'], 'id': parent['id'],
                                    'root_id': parent['root_id'], 'work_id': row['work_id'],
                                    'author_id': parent['author_id'],
                                    'body': plain_text(parent['text_html']),
                                    'published_at': parent_time})
        normalized.append({'comment_id': row['id'], 'video_id': row['work_id'],
                           'thread_id': row['root_id'], 'parent_id': row['parent_id'],
                           'author_channel': row['author_id'], 'author_name': _text(row['author_name'], 256),
                           'body': body, 'published_at': published,
                           'is_own': row['author_id'] == config['uid']})
    by_id = {row['comment_id']: row for row in normalized}
    for row in normalized:
        for ancestor in (row['thread_id'], row['parent_id']):
            known = by_id.get(ancestor)
            if known and (known['video_id'] != row['video_id'] or known['thread_id'] != row['thread_id']):
                raise PublishError('同一父级或评论串被绑定到不同作品')
        current, walked = row, set()
        while current is not None:
            if current['comment_id'] in walked:
                raise PublishError('父级关系存在环')
            walked.add(current['comment_id'])
            parent = by_id.get(current['parent_id'])
            if parent and _iso(parent['published_at']) > _iso(current['published_at']):
                raise PublishError('父评论不能晚于子评论')
            current = parent
    for parent in parent_evidence:
        known = by_id.get(parent['id'])
        if known and any(known[key] != parent[other] for key, other in
                         [('video_id', 'work_id'), ('thread_id', 'root_id'),
                          ('author_channel', 'author_id'), ('body', 'body'), ('published_at', 'published_at')]):
            raise PublishError('父级上下文与已捕获的评论内容矛盾')
    return {'platform': 'weibo', 'account': config['account'], 'channel_id': config['uid'],
            'items': normalized}


def import_capture(root, capture, config):
    document = normalize(capture, config)
    result = import_manual(root, document, baseline_before=config['not_before'])
    result.update({'schema': 'weibo-comment-import.v1', 'platform': 'weibo',
                   'observed_at': capture['observed_at'], 'source_url': SOURCE_URL,
                   'capture_sha256': hashlib.sha256(json.dumps(capture, sort_keys=True,
                       ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
                   'captured': len(document['items']), 'declared_total': capture['total'],
                   'next_cursor': capture['next_cursor'], 'missing': capture['missing'],
                   'coverage': '原生页面响应的人工观察；未进行自动同步，未证明全量覆盖',
                   'parent_context': '嵌套父级仅保留在原观察文件中；不虚构父级的父级关系',
                   'sends': 0})
    return result


def _private_json(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise PublishError('需要本机绝对路径的私有 JSON 普通文件')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o777 != 0o600 or info.st_size > 2 * 1024 * 1024):
            raise PublishError('私有文件须由当前用户持有、权限 0600 且不超过 2 MiB')
        with os.fdopen(fd, encoding='utf-8') as source:
            fd = -1
            return json.load(source)
    finally:
        if fd >= 0:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description='校验并本地导入微博原生评论观察；不登录、联网或发送')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--capture', required=True)
    args = parser.parse_args()
    try:
        result = import_capture(args.state_dir, _private_json(args.capture), _private_json(args.config))
        print(json.dumps(result, ensure_ascii=False))
    except (PublishError, OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, PublishError) else
                          '私有观察读取失败；请核对文件及格式'}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
