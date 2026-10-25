"""Passive native photo-upload receipts, bound to the exact selected file chunks.

Only observes requests made by X's file input. Never constructs or replays an
upload request. Multipart bytes and headers are transient, never stored.
"""
from __future__ import annotations
from email import policy
from email.parser import BytesParser
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from browser_assets import checked_path
from publish_core import PublishError

LIMIT = 5 * 1024 * 1024


def one(params, name):
    values = params.get(name)
    if not isinstance(values, list) or len(values) != 1 or not values[0]:
        raise ValueError('missing or duplicate native parameter')
    return values[0]


def decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,32}', value):
        raise ValueError('invalid native string id')
    return value


def integer(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,8})', value):
        raise ValueError('invalid native integer')
    return int(value)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError('duplicate native JSON key')
        value[key] = item
    return value


def chunks(request, params, command):
    content_type = request.header_value('content-type')
    raw = request.post_data_buffer
    if (not isinstance(content_type, str) or '\r' in content_type or '\n' in content_type
            or not isinstance(raw, bytes) or not 0 < len(raw) <= LIMIT + 65536):
        raise ValueError('native multipart missing or too large')
    message = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + content_type.encode('ascii') + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw)
    parts = list(message.iter_parts())
    if (message.get_content_type() != 'multipart/form-data' or message.defects or len(parts) != 1
            or parts[0].get_content_disposition() != 'form-data'
            or parts[0].get_param('name', header='content-disposition') != 'media'
            or parts[0].defects or parts[0].get('Content-Transfer-Encoding') is not None):
        raise ValueError('unexpected native multipart')
    data = parts[0].get_payload(decode=True)
    if not isinstance(data, bytes) or not 0 < len(data) <= LIMIT:
        raise ValueError('empty or oversized native chunk')
    if command == 'APPEND':
        indices = [integer(one(params, 'segment_index'))]; size = len(data)
    else:
        indices = [integer(v) for v in one(params, 'segment_indexes').split(',')]
        size = integer(one(params, 'max_segment_size'))
        if (not size or len(indices) != math.ceil(len(data) / size)
                or indices != list(range(indices[0], indices[0] + len(indices)))
                or hashlib.md5(data).hexdigest() != one(params, 'media_md5')):
            raise ValueError('native multisegment identity or checksum mismatch')
    if len(indices) > 80 or max(indices) > 79:
        raise ValueError('too many native segments')
    return [dict(index=index, size=len(data[n*size:(n+1)*size]),
                 sha256=hashlib.sha256(data[n*size:(n+1)*size]).hexdigest())
            for n, index in enumerate(indices)]


class Capture:
    """Retain only upload IDs, exact chunk hashes and bounded native facts."""
    def __init__(self, save):
        self.save = save; self.events = []; self.errors = []

    def response(self, response):
        u = urlsplit(response.url)
        if (u.scheme != 'https' or u.netloc != 'upload.x.com'
                or u.path not in {'/i/media/upload.json', '/i/media/upload2.json'}): return
        try:
            if u.fragment or len(self.events) >= 96: raise ValueError()
            params = parse_qs(u.query, keep_blank_values=True)
            command = one(params, 'command')
            if (command not in {'INIT', 'APPEND', 'APPENDMULTI', 'FINALIZE', 'STATUS'}
                    or response.request.method != ('GET' if command == 'STATUS' else 'POST')
                    or type(response.status) is not int or not 200 <= response.status < 300):
                raise ValueError()
            event = dict(command=command, status=response.status, observed_at=time.time())
            if command != 'INIT': event['media_id'] = decimal(one(params, 'media_id'))
            if command == 'INIT':
                event.update(total_bytes=integer(one(params, 'total_bytes')),
                             media_type=one(params, 'media_type'))
                if 'media_category' in params and one(params, 'media_category') != 'tweet_image':
                    raise ValueError()
                if not 0 < event['total_bytes'] <= LIMIT or event['media_type'] not in {
                        'image/png', 'image/jpeg', 'image/webp'}: raise ValueError()
            if command in {'APPEND', 'APPENDMULTI'}:
                event['chunks'] = chunks(response.request, params, command)
            else:
                raw = response.body()
                if not isinstance(raw, bytes) or len(raw) > 65536: raise ValueError()
                body = json.loads(raw, object_pairs_hook=unique)
                if not isinstance(body, dict) or body.get('errors') or body.get('error'): raise ValueError()
                event['body'] = {k: body[k] for k in ('media_id_string', 'media_key', 'size',
                    'expires_after_secs', 'processing_info', 'image') if k in body}
                for field, keys in (('image', ('w','h','image_type')),
                                    ('processing_info', ('state','check_after_secs','progress_percent'))):
                    if field in body:
                        if not isinstance(body[field], dict): raise ValueError()
                        event['body'][field] = {k: body[field][k] for k in keys if k in body[field]}
            self.events.append(event)
            self.save(self.events, self.errors)
        except Exception:
            self.errors.append('native_photo_upload_unrecorded')
            try: self.save(self.events, self.errors)
            except Exception: pass


def wait_complete(page, capture, count):
    """Yield to native responses without selecting files or retrying requests."""
    deadline = time.monotonic() + 60
    while True:
        if capture.errors:
            raise PublishError('X 图片上传回执记录失败；不发送、不重传')
        finals = [e for e in capture.events if e['command'] == 'FINALIZE']
        latest = {}
        for event in capture.events:
            if event['command'] in {'FINALIZE', 'STATUS'}:
                latest[event['media_id']] = event['body']
        if len(finals) >= count and all(
                b.get('processing_info', {}).get('state') not in {'pending', 'in_progress'}
                for b in latest.values()): return
        if time.monotonic() >= deadline:
            raise PublishError('X 图片原生上传尚未完成；保留同一选择，不发送、不重传')
        page.wait_for_timeout(100)


def receipts(events, assets, previews, *, now=None):
    """Match unordered native uploads to ordered source images by actual bytes."""
    now = time.time() if now is None else now
    try:
        if (not isinstance(events, list) or not 3 <= len(events) <= 96
                or len(previews) != len(assets) or not 1 <= len(assets) <= 4
                or len({a['sha256'] for a in assets}) != len(assets)): raise ValueError()
        groups = {}; previous = 0
        for event in events:
            stamp = event['observed_at']; command = event['command']
            if (not number(stamp) or stamp < previous
                    or not 0 < stamp <= now + 5 or type(event['status']) is not int
                    or not 200 <= event['status'] < 300): raise ValueError()
            previous = stamp
            if command == 'INIT':
                ident = decimal(event['body']['media_id_string'])
                if ident in groups: raise ValueError()
                groups[ident] = [event]
            else:
                ident = decimal(event['media_id'])
                if ident not in groups: raise ValueError()
                groups[ident].append(event)
        if len(groups) != len(assets): raise ValueError()
        expected = {}
        for asset, preview in zip(assets, previews):
            data = Path(checked_path(asset)).read_bytes()
            if (not 0 < len(data) <= LIMIT or hashlib.sha256(data).hexdigest() != asset['sha256']
                    or preview['source_asset_id'] != asset['id']
                    or preview['source_sha256'] != asset['sha256']): raise ValueError()
            expected[asset['sha256']] = (asset, preview, data)
        result = {}
        for ident, group in groups.items():
            commands = [e['command'] for e in group]
            if (commands[0] != 'INIT' or commands.count('INIT') != 1
                    or commands.count('FINALIZE') != 1): raise ValueError()
            final = commands.index('FINALIZE')
            if (final < 2 or any(c not in {'APPEND', 'APPENDMULTI'} for c in commands[1:final])
                    or any(c != 'STATUS' for c in commands[final+1:])): raise ValueError()
            init = group[0]; size = init['total_bytes']; segments = {}
            for event in group[1:final]:
                for item in event['chunks']:
                    index = item['index']
                    if (type(index) is not int or not 0 <= index < 80 or index in segments
                            or type(item['size']) is not int or not 0 < item['size'] <= LIMIT
                            or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])): raise ValueError()
                    segments[index] = item
            if not segments or sorted(segments) != list(range(len(segments))): raise ValueError()
            ordered = [segments[i] for i in range(len(segments))]
            if sum(s['size'] for s in ordered) != size: raise ValueError()
            matches = []
            for sha, (asset, preview, data) in expected.items():
                offset = 0; equal = len(data) == size
                mime = {'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.webp':'image/webp'}[
                    Path(asset['path']).suffix.lower()]
                for item in ordered:
                    equal &= hashlib.sha256(data[offset:offset+item['size']]).hexdigest() == item['sha256']
                    offset += item['size']
                if equal and init['media_type'] == mime: matches.append(sha)
            if len(matches) != 1 or matches[0] in result: raise ValueError()
            sha = matches[0]; asset, preview, _ = expected[sha]
            key = init['body']['media_key']; deadlines = []
            if key != '3_' + ident: raise ValueError()
            for event in [group[0], *group[final:]]:
                body = event['body']; expiry = body.get('expires_after_secs')
                # INIT establishes the ID/key; subsequent native requests carry
                # that ID. Do not invent metadata fields absent from the SDK's
                # generic finalize contract; reject them if present and wrong.
                if (body.get('media_id_string', ident) != ident or body.get('media_key', key) != key
                        or (expiry is not None and (not number(expiry) or expiry <= 0))
                        or ('size' in body and (type(body['size']) is not int or body['size'] != size))):
                    raise ValueError()
                if event['command'] == 'INIT' and expiry is None: raise ValueError()
                if expiry is not None: deadlines.append(event['observed_at'] + expiry)
                state = body.get('processing_info', {}).get('state')
                if state not in {None, 'pending', 'in_progress', 'succeeded'}: raise ValueError()
                image = body.get('image')
                if image is not None and any(type(image[k]) is not int or image[k] != preview[name]
                        for k, name in (('w','width'), ('h','height'))): raise ValueError()
            body = group[-1]['body']
            if (body.get('processing_info', {}).get('state') not in {None, 'succeeded'}
                    or min(deadlines) <= now): raise ValueError()
            result[sha] = dict(source_asset_id=asset['id'], source_sha256=sha,
                native=dict(media_id=ident, media_key=key, type='photo',
                            width=preview['width'], height=preview['height']),
                expires_at=min(deadlines))
        return [result[a['sha256']] for a in assets]
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OSError) as exc:
        raise PublishError('X 图片云端回执与封存字节、顺序或原生身份不能核对；不发送、不重传') from exc
