"""Optional, local pacing rules; they are not a platform policy or risk guarantee."""
from __future__ import annotations

import math
import time


def parse_pacing(profile, target):
    """Validate explicit local rules without contacting a platform or reserving a slot."""
    from publish_core import PublishError, PLATFORMS
    config = (profile or {}).get('pacing', {})
    if not isinstance(config, dict):
        raise PublishError('pacing 必须为对象')
    if any(not isinstance(name, str) or name not in PLATFORMS for name in config):
        raise PublishError('pacing 包含未知平台')
    policy = config.get(target['platform'], {})
    if not isinstance(policy, dict) or set(policy) - {'platform_seconds', 'account_seconds'}:
        raise PublishError('平台 pacing 只接受 platform_seconds 和 account_seconds')
    intervals = []
    for field, scope, identity in (
        ('platform_seconds', 'platform', target['platform']),
        ('account_seconds', 'account', target['account']),
    ):
        if field not in policy:
            continue
        value = policy[field]
        if type(value) is not int or not 1 <= value <= 86400:
            raise PublishError(f'{field} 须为 1 到 86400 秒的整数')
        intervals.append((scope, identity, value))
    return intervals


def apply_pacing(db, profile, target):
    """Reserve both scopes in the caller's transaction before a possible remote write."""
    from publish_core import PublishError
    intervals = parse_pacing(profile, target)
    instant = time.time()
    # An existing reservation remains effective even if another profile disables
    # or shortens the rule. Only a successful transaction creates new windows.
    for scope, identity in (('platform', target['platform']), ('account', target['account'])):
        row = db.execute('SELECT next_at FROM pacing_windows WHERE platform=? AND scope=? AND identity=?',
                         (target['platform'], scope, identity)).fetchone()
        if row and row[0] > instant:
            remaining = math.ceil(row[0] - instant)
            raise PublishError(f'本地{scope}间隔尚未到期（约 {remaining} 秒）；保持原任务待执行，不自动重试')
    for scope, identity, interval in intervals:
        db.execute('INSERT INTO pacing_windows(platform,scope,identity,next_at) VALUES (?,?,?,?) '
                   'ON CONFLICT(platform,scope,identity) DO UPDATE SET next_at=excluded.next_at',
                   (target['platform'], scope, identity, instant + interval))
