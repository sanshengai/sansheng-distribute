"""Optional, read-only inspection of a sealed video's actual local metadata."""
from __future__ import annotations

import json
import math
import subprocess

from publish_core import PublishError, file_hash, resolve_file, source_root, target_of, validate


def _rotation(stream):
    """Only report a display rotation when FFprobe gives an unambiguous right angle."""
    values = []
    tags = stream.get('tags', {})
    if not isinstance(tags, dict):
        raise PublishError('视频旋转信息无法判定')
    if 'rotate' in tags:
        values.append(tags['rotate'])
    side_data = stream.get('side_data_list', [])
    if not isinstance(side_data, list) or any(not isinstance(item, dict) for item in side_data):
        raise PublishError('视频旋转信息无法判定')
    values.extend(item['rotation'] for item in side_data if 'rotation' in item)
    angles = []
    for value in values:
        if isinstance(value, bool):
            raise PublishError('视频旋转信息无法判定')
        try:
            angle = float(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise PublishError('视频旋转信息无法判定') from exc
        if not math.isfinite(angle) or angle % 90 != 0:
            raise PublishError('视频旋转信息无法判定')
        angles.append(int(angle) % 360)
    if len(set(angles)) > 1:
        raise PublishError('视频旋转信息相互矛盾')
    return angles[0] if angles else 0


def inspect(manifest, target_id):
    """Inspect one target's single sealed video, without invoking a platform.

    FFprobe is optional; missing tools, bad files and unclear metadata fail closed.
    This reports observations, not platform compliance or a publication receipt.
    """
    validate(manifest)
    target = target_of(manifest, target_id)
    if target.get('content_type') != 'video':
        raise PublishError('媒体检查需要视频目标')
    selected = [asset for asset in manifest['assets']
                if asset['id'] in target.get('asset_ids', []) and asset['kind'] == 'video']
    if len(selected) != 1:
        raise PublishError('媒体检查需要唯一主视频')
    asset = selected[0]
    path = resolve_file(source_root(manifest), asset['path'])
    expected_size = path.stat().st_size
    command = [
        'ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'stream=codec_type,width,height:stream_tags=rotate:'
        'stream_side_data=rotation:format=duration,format_name,size',
        '-of', 'json', '-i', str(path),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError as exc:
        raise PublishError('未找到 ffprobe，无法检查视频；请安装后重试') from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError('本地视频检查失败或超时') from exc
    if proc.returncode or len(proc.stdout) > 65536:
        raise PublishError('视频无法解析或元数据过大')
    try:
        data = json.loads(proc.stdout)
        streams = data['streams']
        fmt = data['format']
        if not isinstance(streams, list) or not isinstance(fmt, dict):
            raise ValueError('invalid structure')
        if any(not isinstance(item, dict) for item in streams):
            raise ValueError('invalid stream')
        videos = [item for item in streams if item.get('codec_type') == 'video']
        if len(videos) != 1:
            raise PublishError('视频轨道不唯一，无法判断主画面')
        video = videos[0]
        width, height = video['width'], video['height']
        if any(type(x) is not int or not 0 < x <= 32768 for x in (width, height)):
            raise ValueError('invalid dimensions')
        if isinstance(fmt['duration'], bool):
            raise ValueError('invalid duration')
        duration = float(fmt['duration'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('invalid duration')
        size = int(fmt['size'])
        if size != expected_size or size <= 0:
            raise ValueError('invalid file size')
        format_name = fmt['format_name']
        if not isinstance(format_name, str) or not format_name or len(format_name) > 128:
            raise ValueError('invalid format name')
        rotation = _rotation(video)
    except PublishError:
        raise
    except (KeyError, ValueError, TypeError, OverflowError, json.JSONDecodeError) as exc:
        raise PublishError('视频元数据不完整或无效') from exc
    # The original source can change while the external decoder runs.
    try:
        unchanged = path.is_file() and file_hash(path) == asset['sha256'] and path.stat().st_size == size
    except OSError:
        unchanged = False
    if not unchanged:
        raise PublishError('检查期间源视频发生变化')
    return {
        'target_id': target_id,
        'asset_id': asset['id'],
        'size_bytes': size,
        'encoded_width': width,
        'encoded_height': height,
        'rotation_degrees': rotation,
        'display_width': height if rotation in (90, 270) else width,
        'display_height': width if rotation in (90, 270) else height,
        'duration_seconds': duration,
        'container_format': format_name,
        'local_only': True,
        'platform_rules_verified': False,
        'notice': '仅检查本地素材，不代表平台规则、账号权限或发布验收。',
    }
