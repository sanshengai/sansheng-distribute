"""Safe, optional explanations for known local publish-package checks.

This module only describes checks already performed by platform_pack. It does
not determine readiness, query platforms, or infer limits from a platform name.
"""
from __future__ import annotations

import re


def _policy(profile, platform):
    if not isinstance(profile, dict):
        return {}
    policies = profile.get('platform_policies')
    value = policies.get(platform) if isinstance(policies, dict) else None
    return value if isinstance(value, dict) else {}


def explain_known_blocker(message, target, manifest, profile):
    """Return a small fixed-wording explanation, or None for unknown errors.

    Only numeric values from a validated local rule and non-content counts are
    returned. Neither source text nor filenames, account data, or paths are
    copied into the optional explanation.
    """
    if not isinstance(message, str) or not isinstance(target, dict):
        return None
    policy = _policy(profile, target.get('platform'))
    match = re.fullmatch(r'(title|text|description|composed_text) 超过已配置限制；交回生产端修改，不截断', message)
    if match:
        field = match.group(1)
        result = {'category': 'length', 'field': field,
                  'suggestion': '请缩短对应文案后重新封存，原文不会被自动截断。'}
        limits = policy.get('max_lengths')
        limit = limits.get(field) if isinstance(limits, dict) else None
        if type(limit) is int and limit > 0:
            result['allowed_max'] = limit
        value = target.get(field)
        if field != 'composed_text' and isinstance(value, str):
            result['actual_count'] = len(value)
        return result
    match = re.fullmatch(r'(image|video|audio|subtitle) 数量超过配置', message)
    if match:
        kind = match.group(1)
        result = {'category': 'asset_count', 'field': kind,
                  'suggestion': '请调整该类素材数量，确认保留所需内容后重新封存。'}
        limits = policy.get('max_assets')
        limit = limits.get(kind) if isinstance(limits, dict) else None
        if type(limit) is int and limit >= 0:
            result['allowed_max'] = limit
        assets = manifest.get('assets') if isinstance(manifest, dict) else None
        ids = target.get('asset_ids')
        if isinstance(assets, list) and isinstance(ids, list):
            lookup = {a.get('id'): a.get('kind') for a in assets if isinstance(a, dict)}
            result['actual_count'] = sum(lookup.get(ident) == kind for ident in ids)
        return result
    match = re.fullmatch(r'(image|video|audio|subtitle) 文件格式不符合配置', message)
    if match:
        return {'category': 'asset_format', 'field': match.group(1),
                'suggestion': '请核对本地已配置的允许格式，调整素材后重新封存。'}
    match = re.fullmatch(r'平台要求字段：(title|text|description|composed_text|tags|links)', message)
    if match:
        return {'category': 'required_field', 'field': match.group(1),
                'suggestion': '请补齐该交接字段，再重新封存。'}
    known = {
        '平台要求封面': ('cover', '请补齐已配置要求的封面，再重新封存。'),
        '平台要求字幕': ('subtitle', '请补齐已配置要求的字幕，再重新封存。'),
        '封面必须引用目标图片': ('cover', '请将封面引用到本目标的图片素材，再重新封存。'),
    }
    if message in known:
        field, suggestion = known[message]
        return {'category': 'asset_required', 'field': field, 'suggestion': suggestion}
    return None
