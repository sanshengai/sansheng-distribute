"""Read-only, local platform handoff guidance. It does not assess live access."""
from __future__ import annotations

from platform_pack import CHECKS, SOCIAL
from publish_core import PLATFORMS, PublishError

_FIELDS = {'title', 'text', 'description', 'composed_text', 'tags', 'links'}
_LENGTH_FIELDS = {'title', 'text', 'description', 'composed_text'}
_KINDS = {'image', 'video', 'audio', 'subtitle'}
_POLICY_FIELDS = {'required_fields', 'max_lengths', 'max_assets', 'allowed_extensions',
                  'require_cover', 'require_subtitles'}
_ENDPOINTS = {'draft', 'prefill', 'publish', 'broadcast'}
_PUBLIC_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif', '.svg', '.heic', '.heif',
    '.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi', '.flv', '.mpeg', '.mpg', '.ts', '.m2ts', '.3gp', '.ogv',
    '.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.opus', '.aiff', '.wma',
    '.srt', '.vtt', '.ass', '.ssa', '.ttml', '.dfxp', '.sbv',
}
_HIDDEN = '<未展示的其他扩展名>'


def _policy(value):
    """Return only bounded, typed supported fields; never echo malformed input."""
    if not isinstance(value, dict) or set(value) - _POLICY_FIELDS:
        raise PublishError('平台规格配置无效：仅允许已知规则')
    result = {}
    redacted = False
    for key, val in value.items():
        if key == 'required_fields':
            if not isinstance(val, list) or any(not isinstance(x, str) or x not in _FIELDS for x in val):
                raise PublishError('平台规格 required_fields 无效')
            result[key] = list(val)
        elif key in {'require_cover', 'require_subtitles'}:
            if type(val) is not bool:
                raise PublishError('平台规格开关必须为布尔值')
            result[key] = val
        elif key in {'max_lengths', 'max_assets'}:
            fields, minimum = (_LENGTH_FIELDS, 1) if key == 'max_lengths' else (_KINDS, 0)
            if (not isinstance(val, dict) or any(k not in fields or type(v) is not int or v < minimum
                                                  for k, v in val.items())):
                raise PublishError('平台规格数字限制无效')
            result[key] = dict(val)
        else:
            if (not isinstance(val, dict) or any(
                    kind not in _KINDS or not isinstance(items, list) or not items or
                    any(not isinstance(ext, str) or not ext.startswith('.') for ext in items)
                    for kind, items in val.items())):
                raise PublishError('平台规格扩展名无效')
            result[key] = {}
            for kind, items in val.items():
                result[key][kind] = []
                for extension in items:
                    if extension.lower() in _PUBLIC_EXTENSIONS:
                        result[key][kind].append(extension)
                    else:
                        result[key][kind].append(_HIDDEN)
                        redacted = True
    return result, redacted


def platform_guide(platform, profile=None):
    if platform not in PLATFORMS:
        raise PublishError('未知平台')
    if profile is None:
        profile = {}
    if not isinstance(profile, dict):
        raise PublishError('配置必须为对象')
    policies = profile.get('platform_policies', {})
    if not isinstance(policies, dict):
        raise PublishError('平台规格配置无效')
    policy, policy_redacted = _policy(policies.get(platform, {}))
    adapters = profile.get('adapters', {})
    if not isinstance(adapters, dict) or not isinstance(adapters.get(platform, {}), dict):
        raise PublishError('适配器配置无效')
    adapter = adapters.get(platform, {})
    enabled = adapter.get('enabled', False)
    capabilities = adapter.get('enabled_capabilities', [])
    allowed = {f'{content}:{endpoint}' for content in PLATFORMS[platform] for endpoint in _ENDPOINTS}
    if type(enabled) is not bool or not isinstance(capabilities, list) or any(
            not isinstance(item, str) or item not in allowed for item in capabilities):
        raise PublishError('已启用能力配置无效')
    return {
        'platform': platform,
        'accepted_content_types': sorted(PLATFORMS[platform]),
        'local_pack_available': platform in SOCIAL,
        'wechat_article_guidance': platform == 'wechat',
        'checks': list(CHECKS.get(platform, ['公众号草稿、媒体插入及正式发表是不同终点；按公众号流程核对'])),
        'configured_policy': policy,
        'configured_policy_redacted': policy_redacted,
        'adapter_enabled_config': enabled,
        'adapter_enabled_config_semantics': 'enabled 是配置与诊断标记，不是执行总闸；具体能力还须通过账号、授权、验收范围和适配器检查。',
        'enabled_capabilities_config': sorted(set(capabilities)),
        'required_platform_metadata': ({
            'made_for_kids': {'type': 'boolean', 'required': True},
            'visibility': {'type': 'string', 'required': True, 'values': ['public', 'unlisted', 'private']},
        } if platform == 'youtube' else {}),
        'local_only': True,
        'remote_checked': False,
        'notice': '仅展示本地代码与配置，不代表官方当前规格、账号权限或实际验收；未登录、授权或发布。',
    }
