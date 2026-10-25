"""Passive native-upload VID receipts. No private API calls or posting path.

The native SDK contract is source-observed; live receipt acceptance is pending.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from publish_core import PublishError, digest

HOSTS = {'vod.bytedanceapi.com', 'open.bytedanceapi.com'}
META_FIELDS = ('Md5', 'Size', 'Duration', 'Width', 'Height', 'Uri')


def integer(value, low=1, high=256 * 1024 * 1024):
    return type(value) is int and low <= value <= high


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def exact_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate native field')
        value[key] = item
    return value


def source(asset):
    """Hash the same local bytes for the sealed SHA and SDK MD5 contract."""
    try:
        path = Path(asset['absolute_path'])
        sha = hashlib.sha256(); md5 = hashlib.md5(usedforsecurity=False); size = 0
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                size += len(block); sha.update(block); md5.update(block)
        if not integer(size) or sha.hexdigest() != asset['sha256']:
            raise ValueError('source changed')
        return dict(source_asset_id=asset['id'], source_sha256=sha.hexdigest(),
                    md5=md5.hexdigest(), size=size)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise PublishError('抖音上传原片摘要无法核对；未选择上传') from exc


def native(body):
    """Select only non-secret identity fields from the native SDK result."""
    try:
        meta = body['ResponseMetadata']; results = body['Result']['Results']
        if (not isinstance(meta, dict) or meta.get('Error') is not None
                or not isinstance(results, list) or len(results) != 1):
            raise ValueError()
        result = results[0]; value = result['VideoMeta']; vid = result['Vid']
        if (not isinstance(vid, str) or not re.fullmatch(r'v[A-Za-z0-9_-]{15,79}', vid)
                or not isinstance(value, dict)
                or not isinstance(value['Md5'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['Md5'])
                or not integer(value['Size'])
                or not number(value['Duration']) or not 0 < value['Duration'] <= 86400
                or not integer(value['Width'], 1, 16384) or not integer(value['Height'], 1, 16384)
                or not isinstance(value['Uri'], str)
                or not re.fullmatch(r'tos-cn-v-[A-Za-z0-9-]+/[A-Za-z0-9_-]+', value['Uri'])):
            raise ValueError()
        return dict(vid=vid, metadata={k: value[k] for k in META_FIELDS})
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PublishError('抖音原生视频提交回执缺失、失败或字段尚未覆盖') from exc


class Capture:
    """Only listen to the page's own CommitUploadInner response."""
    def __init__(self, save):
        self.events = []; self.errors = []; self.diagnostics = []; self.save = save

    def response(self, response):
        parts = urlsplit(response.url)
        query = parse_qs(parts.query)
        if (parts.scheme != 'https' or parts.netloc not in HOSTS
                or parts.path != '/' or query.get('Action') != ['CommitUploadInner']):
            return
        phase = 'request'; data = None; shape = None
        try:
            if (query.get('Version') != ['2020-11-19'] or query.get('SpaceName') != ['aweme']
                    or response.request.method != 'POST' or type(response.status) is not int
                    or response.status != 200 or len(self.events) >= 4):
                raise ValueError()
            phase = 'body'; data = response.body()
            if not isinstance(data, bytes) or not 0 < len(data) <= 65536:
                raise ValueError()
            phase = 'json'; body = json.loads(data, object_pairs_hook=exact_object)
            if isinstance(body, dict):
                # Names are fixed, values are types only. Never persist tokens,
                # request headers, raw responses or arbitrary server key names.
                shape = {k: type(body.get(k)).__name__ for k in ('ResponseMetadata', 'Result')}
                result = body.get('Result')
                if isinstance(result, dict):
                    values = result.get('Results')
                    shape['Results'] = type(values).__name__
                    if isinstance(values, list):
                        shape['result_count'] = len(values)
                        if values and isinstance(values[0], dict):
                            meta = values[0].get('VideoMeta')
                            shape['Vid'] = type(values[0].get('Vid')).__name__
                            shape['VideoMeta'] = type(meta).__name__
                            if isinstance(meta, dict):
                                shape['VideoMeta_fields'] = {k: type(meta.get(k)).__name__ for k in META_FIELDS}
            phase = 'native_fields'
            event = dict(native(body), observed_at=time.time())
            self.events.append(event)
            phase = 'save'
            self.save(self.events, self.errors)
        except Exception as exc:
            self.diagnostics.append(dict(phase=phase, error_type=type(exc).__name__,
                response_status=response.status if type(response.status) is int else None,
                version=query['Version'][0] if len(query.get('Version', [])) == 1
                    and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', query['Version'][0]) else 'unrecognized',
                space_is_aweme=query.get('SpaceName') == ['aweme'],
                body_bytes=len(data) if isinstance(data, bytes) else None,
                body_sha256=hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else None,
                field_types=shape))
            self.errors.append('native_video_commit_unrecorded')
            try: self.save(self.events, self.errors)
            except Exception: pass


def receipt(events, local, preview, *, now=None):
    """Bind actual commit VID to the independently byte-verified native object."""
    now = time.time() if now is None else now
    try:
        if (not isinstance(events, list) or not 1 <= len(events) <= 4
                or not isinstance(local['source_asset_id'], str) or not local['source_asset_id']
                or not re.fullmatch(r'[0-9a-f]{64}', local['source_sha256'])
                or not re.fullmatch(r'[0-9a-f]{32}', local['md5']) or not integer(local['size'])
                or preview['source_asset_id'] != local['source_asset_id']
                or preview['source_sha256'] != local['source_sha256']
                or type(preview['size']) is not int or preview['size'] != local['size']
                or not integer(preview['width'],1,16384) or not integer(preview['height'],1,16384)
                or not number(preview['duration']) or preview['duration'] <= 0
                or not number(now)):
            raise ValueError()
        previous = 0; identities = []
        for event in events:
            stamp = event['observed_at']; vid = event['vid']; meta = event['metadata']
            validated = native(dict(ResponseMetadata={}, Result=dict(Results=[dict(Vid=vid, VideoMeta=meta)])))
            if (not number(stamp) or stamp < previous or stamp > now + 5
                    or meta['Md5'] != local['md5'] or meta['Size'] != local['size']
                    or meta['Width'] != preview['width'] or meta['Height'] != preview['height']
                    or not number(preview['duration']) or abs(meta['Duration'] - preview['duration']) > 0.05
                    or digest(['video-cn.douyin.com', meta['Uri']]) != preview['preview_key']):
                raise ValueError()
            previous = stamp; identities.append(validated)
        if any(value != identities[0] for value in identities):
            raise ValueError()
        return dict(vid=identities[0]['vid'], source_asset_id=local['source_asset_id'],
                    source_sha256=local['source_sha256'], size=local['size'],
                    width=preview['width'], height=preview['height'], duration=preview['duration'],
                    preview_key=preview['preview_key'])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PublishError('抖音上传VID与封存原片、原生预览字节或对象回执不符；不重传') from exc


def verify(pack, saved, preview):
    if saved.get('video_upload_errors'):
        raise PublishError('抖音原生视频提交回执记录失败；保持原尝试，不重传')
    events = saved.get('video_upload_events', [])
    if not events:
        if saved.get('cloud_video') is not None:
            raise PublishError('抖音VID缺少原始提交回执')
        return None
    asset = next(a for a in pack['assets'] if a['kind'] == 'video')
    local = source(asset)
    if local != saved.get('video_source'):
        raise PublishError('抖音上传原片摘要与原尝试不符')
    value = receipt(events, local, preview)
    if value != saved.get('cloud_video'):
        raise PublishError('抖音VID与原尝试保存的回执不符')
    return value


def post_video_binding(details, pack, saved):
    """Only the video binding; callers must still verify all other post fields."""
    try:
        asset = next(a for a in pack['assets'] if a['kind'] == 'video')
        local = saved['video_source']; preview = saved['source_video']
        if (saved.get('pack_digest') != digest(pack) or saved.get('video_upload_errors')
                or local['source_asset_id'] != asset['id'] or local['source_sha256'] != asset['sha256']):
            raise ValueError()
        bound = receipt(saved['video_upload_events'], local, preview)
        current = details['video']
        if (saved.get('cloud_video') != bound or current['vid'] != bound['vid']
                or not integer(current['width'],1,16384) or current['width'] != bound['width']
                or not integer(current['height'],1,16384) or current['height'] != bound['height']
                or not integer(current['duration_ms'],1,86400000)
                or abs(current['duration_ms'] - bound['duration'] * 1000) > 50):
            raise ValueError()
        return bound
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise PublishError('抖音已发布作品的VID、原片回执或媒体属性不符') from exc
