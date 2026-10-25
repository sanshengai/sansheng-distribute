"""Native scheduling prefill and timer observation; never submits."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from publish_core import PublishError

INPUT = 'input[placeholder="日期和时间"]'
LABELS = 'label[class^="radio-"]'
OPTIONS_READ = '''ns=>ns.filter(n=>{const r=n.getBoundingClientRect();
 return r.width>0&&r.height>0&&getComputedStyle(n).visibility==='visible';})
 .map(n=>({text:n.innerText.trim(),inputs:[...n.querySelectorAll('input')]
 .map(x=>({type:x.type,value:x.value,checked:x.checked,disabled:x.disabled}))}))'''
DATE_CONTEXT = '''epoch=>{const d=new Date(epoch*1000),pad=n=>String(n).padStart(2,'0');
 return {input:`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`,
 offset_minutes:-d.getTimezoneOffset(),timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
 roundtrip_epoch:new Date(d.getFullYear(),d.getMonth(),d.getDate(),d.getHours(),d.getMinutes(),0,0).getTime()/1000,
 now:Date.now()/1000};}'''
LIMIT_TEXT = re.compile(r'支持([1-9][0-9]*)(分钟|小时|天)后及([1-9][0-9]*)(分钟|小时|天)内的定时发布')


def native_timer(aweme):
    """Observe the work timer, independently of the editor date field.

    Native modules 23934/6803 distinguish pending, online, banned and
    unbanned-pending timers. This alone never verifies a public work.
    """
    if not isinstance(aweme, dict):
        raise PublishError('抖音作品原生数据无效')
    timer = aweme.get('timer')
    if timer is None:
        return None
    try:
        code, epoch = timer['status'], timer['public_time']
        states = {0: 'scheduled_pending', 1: 'online',
                  4: 'account_blocked', 5: 'account_unbanned_pending'}
        if (type(code) is not int or code not in states or type(epoch) is not int
                or epoch < 0 or code != 1 and epoch == 0):
            raise ValueError()
        dt = datetime.fromtimestamp(epoch, timezone.utc)
        return dict(native_status=code, state=states[code], public_at_epoch=epoch,
                    utc=dt.isoformat(),
                    beijing=dt.astimezone(timezone(timedelta(hours=8))).isoformat(),
                    publication_verified=False)
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise PublishError('抖音原生排期状态或时间未覆盖；不能认定已经发布') from exc


def requested(metadata):
    if 'publish_at' not in metadata:
        return None
    value = metadata['publish_at']
    if (not isinstance(value, str) or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00(?:Z|[+-][0-9]{2}:[0-9]{2})', value)):
        raise PublishError('publish_at 须带时区并精确到整分钟，例如 2026-10-30T18:30:00+08:00')
    try:
        if value.endswith('-00:00') or (value[-1] != 'Z' and int(value[-2:]) > 59):
            raise ValueError()
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        epoch = int(dt.timestamp())
        result = dict(epoch=epoch, utc=dt.astimezone(timezone.utc).isoformat(),
                      beijing=dt.astimezone(timezone(timedelta(hours=8))).isoformat())
    except (ValueError, OverflowError, OSError) as exc:
        raise PublishError('publish_at 不是有效的带时区日期') from exc
    return result


def require_future(expected, now=None):
    if expected and expected['epoch'] <= (datetime.now(timezone.utc).timestamp() if now is None else now):
        raise PublishError('抖音定时时间已过；先修改交接包，不上传或改为立即发布')


def selected(rows, scheduled):
    if not isinstance(rows, list):
        raise PublishError('抖音定时选项无法读回')
    for label, value, checked in [('立即发布', '0', not scheduled), ('定时发布', '1', scheduled)]:
        found = [r for r in rows if isinstance(r, dict) and r.get('text') == label]
        want = dict(type='checkbox', value=value, checked=checked, disabled=False)
        if (len(found) != 1 or found[0].get('inputs') != [want]
                or any(type(found[0]['inputs'][0].get(k)) is not bool for k in ('checked', 'disabled'))):
            raise PublishError('抖音定时选项缺失、禁用或与预期不同；不改变其他设置')


def context(page, expected):
    actual = page.evaluate(DATE_CONTEXT, expected['epoch'])
    try:
        offset = actual['offset_minutes']
        if type(offset) is not int or not -840 <= offset <= 840:
            raise ValueError()
        dt = datetime.fromtimestamp(expected['epoch'], timezone(timedelta(minutes=offset)))
        if (not isinstance(actual.get('timezone'), str) or not actual['timezone']
                or actual['input'] != dt.strftime('%Y-%m-%d %H:%M')
                or type(actual.get('roundtrip_epoch')) not in (int, float)
                or actual['roundtrip_epoch'] != expected['epoch']
                or type(actual.get('now')) not in (int, float)):
            raise ValueError()
        native_zone = ZoneInfo(actual['timezone'])
        if datetime.fromtimestamp(expected['epoch'], native_zone).utcoffset() != timedelta(minutes=offset):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise PublishError('浏览器定时时区或时间换算不能核对') from exc
    return actual


def limits(text, expected, now, zone='Asia/Shanghai'):
    matches = list(LIMIT_TEXT.finditer(text))
    if len(matches) != 1:
        raise PublishError('抖音当前账号的定时范围无法唯一核对')
    start, su, end, eu = matches[0].groups()
    def bound(value, unit):
        # Native dayjs.add(n, 'd') advances calendar days in the browser zone;
        # 14 local days can differ from 14*86400 seconds across DST.
        if unit == '天':
            return (datetime.fromtimestamp(now, ZoneInfo(zone))+timedelta(days=value)).timestamp()
        return now + value*{'分钟': 60, '小时': 3600}[unit]
    try:
        lower, upper = bound(int(start), su), bound(int(end), eu)
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError) as exc:
        raise PublishError('抖音定时范围的时区或日期无法换算') from exc
    if lower >= upper or not lower <= expected['epoch'] <= upper:
        raise PublishError('定时时间超出当前账号显示的范围；不让平台静默改时间')


def verify(page, metadata, saved):
    expected = requested(metadata)
    if expected is None:
        return None
    selected(page.locator(LABELS).evaluate_all(OPTIONS_READ), True)
    actual = context(page, expected)
    field = page.locator(INPUT)
    if field.count() != 1 or not field.is_visible() or field.input_value() != actual['input']:
        raise PublishError('抖音定时日期被平台调整、未提交输入或已改变')
    receipt = dict(expected, input=actual['input'], offset_minutes=actual['offset_minutes'],
                   timezone=actual['timezone'], assurance='visible_editor_only')
    if saved != receipt:
        raise PublishError('抖音定时预填回执缺失或与当前编辑器不符')
    return receipt


def fill(page, metadata):
    expected = requested(metadata)
    if expected is None:
        return None
    require_future(expected)
    selected(page.locator(LABELS).evaluate_all(OPTIONS_READ), False)
    choice = page.locator(LABELS).filter(has_text=re.compile(r'^\s*定时发布\s*$'))
    if choice.count() != 1:
        raise PublishError('抖音定时选择入口不唯一')
    choice.click(timeout=5000)
    field = page.locator(INPUT)
    field.wait_for(state='visible', timeout=5000)
    selected(page.locator(LABELS).evaluate_all(OPTIONS_READ), True)
    hint = page.get_by_text(LIMIT_TEXT)
    if hint.count() != 1:
        raise PublishError('抖音定时范围提示不唯一')
    actual = context(page, expected)
    limits(hint.inner_text(), expected, actual['now'], actual['timezone'])
    field.fill(actual['input'])
    field.press('Enter')
    field.press('Tab')
    receipt = dict(expected, input=actual['input'], offset_minutes=actual['offset_minutes'],
                   timezone=actual['timezone'], assurance='visible_editor_only')
    verify(page, metadata, receipt)
    return receipt
