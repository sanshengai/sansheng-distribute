"""X only: prove an unknown task never wrote an attempt record before stopping.

Runs only when the executor stopped before its first durable attempt record
(for example no unique X anchor page). Requires an explicit author
confirmation file bound to the exact job, an empty read-only timeline check,
and no other trace in the request directory. Any trace refuses.
"""
from datetime import datetime
from pathlib import Path
from publish_core import PublishError, digest, read_json

PLATFORMS = {'x'}
FIELDS = {'schema_version', 'job_key', 'identity', 'author_statement', 'confirmed_at',
          'ledger_check', 'timeline_check'}
TIMELINE = {'observed_at', 'account_identity', 'query', 'matches', 'evidence_path'}


def confirmation_path(requests, key):
    return Path(requests) / f'{key}.no-attempt-unsubmitted.json'


def traces(requests, key):
    """Every request-directory file for this key other than the request itself,
    the executor's pre-attempt error and the confirmation is a trace."""
    allowed = {f'{key}.json', f'{key}.browser-error.json', f'{key}.no-attempt-unsubmitted.json'}
    return sorted(p.name for p in Path(requests).glob(key + '*') if p.name not in allowed)


def ledger_clean(events):
    """events: list of (state, data-json-string). Only empty unknown markers allowed."""
    return bool(events) and all(s == 'submission_unknown' and d in ('{}', '') for s, d in events)


def _time(v):
    t = datetime.fromisoformat(v)
    if t.tzinfo is None: raise ValueError
    return t


def check(value, *, key, identity, requests):
    listing = value.get('timeline_check') if isinstance(value, dict) else None
    try:
        _time(value['confirmed_at']); _time(listing['observed_at']); ok_time = True
    except (KeyError, TypeError, ValueError):
        ok_time = False
    if (not isinstance(value, dict) or set(value) != FIELDS or value.get('schema_version') != 1
            or value['job_key'] != key or value['identity'] != identity
            or not isinstance(value['author_statement'], str) or len(value['author_statement'].strip()) < 4
            or value.get('ledger_check') != {'events_without_observation': True, 'remote_id': None}
            or not isinstance(listing, dict) or set(listing) != TIMELINE
            or listing['account_identity'] != identity or listing['matches'] != []
            or not isinstance(listing['query'], str) or not listing['query'].strip()
            or not isinstance(listing['evidence_path'], str) or not Path(listing['evidence_path']).is_file()
            or not ok_time):
        raise PublishError('无尝试记录的未提交确认不完整或不符；保持未知')
    found = traces(requests, key)
    if found:
        raise PublishError('请求目录存在上传/发送痕迹；不能认定未提交：' + ', '.join(found))
    return digest(value)


def recover(action, request, pack, bound, marker):
    """Called only when no attempt marker and no preinput archive exist."""
    if action != 'verify' or pack['platform'] not in PLATFORMS:
        return None
    job = request['job']
    if job.get('remote_id') or job.get('state') != 'submission_unknown':
        return None
    path = confirmation_path(marker.parent, job['key'])
    if not path.exists() or marker.exists():
        return None
    proof = check(read_json(path), key=job['key'], identity=bound['identity'], requests=marker.parent)
    return dict({k: pack[k] for k in ('manifest_digest', 'target_id', 'platform', 'account')},
                state='not_submitted', absence_verified=True,
                absence_scope='author_confirmed_no_attempt_record',
                confirmation_digest=proof, evidence_path=str(path),
                observed_account_identity=bound['identity'])
