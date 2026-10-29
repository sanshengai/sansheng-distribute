#!/usr/bin/env python3
"""Read back a known own comment. No sending, authorization or receipt adoption."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from bilibili_comments_candidate import Client, _comment, _id, _private_json, validate_config
from interaction_import import _safe_state_dir, _string
from publish_core import PublishError, digest, now

FIELDS = {'schema', 'account', 'uid', 'aid', 'bvid', 'comment_id', 'root_id', 'parent_id', 'text'}


def validate_probe(raw, cfg):
    cfg = validate_config(cfg)
    if not isinstance(raw, dict) or set(raw) != FIELDS or raw['schema'] != 'bilibili-comment-probe.v1':
        raise PublishError('评论核验需完整的私有目标，不接受未知字段')
    if raw['account'] != cfg['account'] or _id(raw['uid']) != _id(cfg['uid']):
        raise PublishError('评论核验账号不符')
    value = dict(raw)
    for key in ('uid', 'aid', 'comment_id'):
        value[key] = _id(raw[key])
    for key in ('root_id', 'parent_id'):
        if raw[key] == '0' or type(raw[key]) is int and raw[key] == 0:
            value[key] = '0'
        else:
            value[key] = _id(raw[key])
    if (value['root_id'] == '0') != (value['parent_id'] == '0'):
        raise PublishError('一级评论与子回复的父级字段不一致')
    if value['root_id'] != '0' and value['comment_id'] in (value['root_id'], value['parent_id']):
        raise PublishError('子回复不能以自身为根或父级')
    if not any(_id(w['aid']) == value['aid'] and w['bvid'] == value['bvid'] for w in cfg['works']):
        raise PublishError('评论不在已配置本人作品内')
    value['text'] = _string(raw['text'], 'text', 20000)
    return value


def _checked(raw, aid, uid, root=None):
    if (not isinstance(raw, dict) or type(raw.get('state')) is not int
            or type(raw.get('invisible')) is not bool):
        raise PublishError('平台未明确返回评论可见性')
    # Reuse identity validation while retaining, rather than accepting, the real visibility.
    identity = _comment({**raw, 'state': 0, 'invisible': False}, aid, uid, root)
    return identity, {'state_code': raw['state'], 'invisible': raw['invisible']}


def _visible(flags):
    return flags['state_code'] == 0 and flags['invisible'] is False


def _page(raw, pn):
    page, rows = raw.get('page'), raw.get('replies')
    if (not isinstance(page, dict) or type(page.get('num')) is not int or page['num'] != pn
            or type(page.get('size')) is not int or page['size'] != 20
            or type(page.get('count')) is not int or page['count'] < 0
            or rows is not None and not isinstance(rows, list)):
        raise PublishError('评论分页结构无效')
    rows = rows or []
    if len(rows) != min(20, max(0, page['count'] - (pn - 1) * 20)):
        raise PublishError('评论分页缺项，不能确认核验完成')
    return rows, pn * 20 >= page['count']


def _root_on_list(client, aid, uid, expected, max_pages):
    seen = set()
    for pn in range(1, max_pages + 1):
        raw = client.get('threads', type=1, oid=aid, sort=0, pn=pn, ps=20)
        rows, complete = _page(raw, pn)
        for row in rows:
            item, flags = _checked(row, aid, uid)
            if item['comment_id'] in seen:
                raise PublishError('一级评论分页重复，停止核验')
            seen.add(item['comment_id'])
            if item['comment_id'] == expected['comment_id']:
                if item != expected or not _visible(flags):
                    raise PublishError('根评论列表与指定对话的身份或可见性不一致')
                return True, pn, complete
        if complete:
            return False, pn, True
    return False, max_pages, False


def readback(cfg, probe, client, *, max_pages=3):
    probe = validate_probe(probe, cfg)
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise PublishError('核验页数须为 1 至 10')
    uid, aid, rid = probe['uid'], probe['aid'], probe['comment_id']
    who = client.get('identity')
    if who.get('isLogin') is not True or _id(who.get('mid')) != uid:
        raise PublishError('实际登录身份不符；停止核验')
    view = client.get('work', aid=aid)
    if (_id(view.get('aid')) != aid or view.get('bvid') != probe['bvid']
            or _id((view.get('owner') or {}).get('mid')) != uid):
        raise PublishError('实际作品与本人账号不符')
    root_id = rid if probe['root_id'] == '0' else probe['root_id']
    found, flags, root_flags, pages, complete, seen = None, None, None, 0, False, set()
    root_snapshot = None
    for pn in range(1, max_pages + 1):
        raw = client.get('replies', type=1, oid=aid, root=root_id, pn=pn, ps=20)
        root, current_root_flags = _checked(raw.get('root'), aid, uid)
        if root['comment_id'] != root_id:
            raise PublishError('平台返回了其他根评论')
        if root_flags is not None and current_root_flags != root_flags:
            raise PublishError('核验期间根评论可见性已变化')
        if root_snapshot is not None and root != root_snapshot:
            raise PublishError('核验期间根评论身份或正文已变化')
        root_snapshot = root
        root_flags = current_root_flags
        pages += 1
        if probe['root_id'] == '0':
            found, flags, complete = root, root_flags, True
            break
        rows, complete = _page(raw, pn)
        for row in rows:
            item, visibility = _checked(row, aid, uid, root_id)
            if item['comment_id'] in seen:
                raise PublishError('子回复分页重复，停止核验')
            seen.add(item['comment_id'])
            if item['comment_id'] == rid:
                found, flags = item, visibility
        if found or complete:
            break
    result = {'schema': 'bilibili-comment-readback.v1', 'probe_digest': digest(probe),
              'account': probe['account'], 'uid': uid, 'aid': aid, 'bvid': probe['bvid'],
              'comment_id': rid, 'root_id': probe['root_id'], 'parent_id': probe['parent_id'],
              'checked_at': now(), 'pages': pages, 'scan_complete': complete,
              'verified_public': False, 'may_resend': False, 'writes': 0, 'official_api': False}
    if found is None:
        return {**result, 'state': 'unconfirmed', 'reason': '有限查询未找到目标；不据此认定发送失败或重新发送'}
    expected_parent = None if probe['parent_id'] == '0' else probe['parent_id']
    if (found['author_channel'] != uid or found['parent_id'] != expected_parent
            or found['body'] != probe['text']):
        raise PublishError('已知评论的作者、直接父级或正文与封存目标不符')
    result.update(visibility=flags, root_visibility=root_flags)
    if not _visible(flags) or not _visible(root_flags):
        return {**result, 'state': 'restricted', 'reason': '目标或根评论可见性受限；不计公开成功，不自动重发'}
    on_list, list_pages, list_complete = _root_on_list(client, aid, uid, root_snapshot, max_pages)
    result.update(list_pages=list_pages, list_complete=list_complete, root_on_list=on_list)
    if not on_list:
        return {**result, 'state': 'unconfirmed', 'reason': '指定 ID 可读，但一级列表尚未确认根评论；不重发'}
    return {**result, 'state': 'visible_on_readback', 'verified_public': True,
            'reason': '指定目标的身份、正文、父级及当前可见性已独立读回；不代表全站或长期可见性'}


def main():
    p = argparse.ArgumentParser(description='只读核验已知 B站本人评论；不发送、不认领原发送回执')
    p.add_argument('--config', required=True); p.add_argument('--probe', required=True)
    p.add_argument('--state-dir', required=True); p.add_argument('--max-pages', type=int, default=3)
    args = p.parse_args()
    try:
        root = _safe_state_dir(args.state_dir)
        cfg = validate_config(_private_json(args.config))
        probe = validate_probe(_private_json(args.probe), cfg)
        result = readback(cfg, probe, Client(cfg['credentials']), max_pages=args.max_pages)
        filename = root / ('bilibili-readback-' + uuid.uuid4().hex + '.json')
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(result, file, ensure_ascii=False, indent=2)
        print(json.dumps({**result, 'local_evidence': str(filename)}, ensure_ascii=False))
        return 0 if result['verified_public'] else 3
    except (PublishError, OSError, ValueError, TypeError, KeyError):
        print('B站已知评论核验未完成；保留原发送记录，不自动重发', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
