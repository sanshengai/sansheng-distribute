from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import douyin_schedule as schedule
from publish_core import PublishError


META = {'publish_at': '2070-10-30T18:30:00+08:00'}


def rows(scheduled):
    return [dict(text=t, inputs=[dict(type='checkbox', value=v, checked=c, disabled=False)])
            for t, v, c in [('立即发布', '0', not scheduled), ('定时发布', '1', scheduled)]]


class Node:
    def __init__(self, page, kind): self.page, self.kind = page, kind
    def count(self): return self.page.counts.get(self.kind, 1)
    def is_visible(self): return self.page.visible
    def filter(self, **kwargs): return Node(self.page, 'choice')
    def evaluate_all(self, script):
        if script != schedule.OPTIONS_READ: raise AssertionError(script)
        return self.page.options if self.page.options is not None else rows(self.page.scheduled)
    def click(self, **kwargs): self.page.clicks += 1; self.page.scheduled = True
    def wait_for(self, **kwargs): pass
    def inner_text(self): return self.page.hint
    def fill(self, value): self.page.fills += 1; self.page.value = value
    def press(self, key):
        self.page.keys.append(key)
        if self.page.clamp and key == 'Tab': self.page.value = '2070-10-31 18:30'
    def input_value(self): return self.page.value


class Page:
    def __init__(self):
        self.scheduled = False; self.options = None; self.visible = True
        self.hint = '支持10分钟后及14天内的定时发布。发布后如需修改时间，可前往作品管理修改'
        self.value = ''; self.clicks = 0; self.fills = 0; self.keys = []; self.clamp = False
        self.counts = {}; self.offset = 480; self.timezone = 'Asia/Taipei'; self.context_override = None
    def locator(self, selector):
        return Node(self, 'field' if selector == schedule.INPUT else 'labels')
    def get_by_text(self, pattern): return Node(self, 'hint')
    def evaluate(self, script, epoch):
        if script != schedule.DATE_CONTEXT: raise AssertionError(script)
        if self.context_override is not None: return self.context_override
        from datetime import timedelta
        return dict(input=datetime.fromtimestamp(epoch, timezone(timedelta(minutes=self.offset))).strftime('%Y-%m-%d %H:%M'), roundtrip_epoch=epoch,
                    offset_minutes=self.offset, timezone=self.timezone, now=epoch-86400)


class ScheduleTests(unittest.TestCase):
    def test_native_timer_states_are_not_editor_prefill_or_publication_proof(self):
        epoch = 1793442600
        for code, state in [(0, 'scheduled_pending'), (1, 'online'),
                            (4, 'account_blocked'), (5, 'account_unbanned_pending')]:
            with self.subTest(code=code):
                value = schedule.native_timer(dict(timer=dict(status=code, public_time=epoch)))
                self.assertEqual(value['state'], state)
                self.assertEqual(value['public_at_epoch'], epoch)
                self.assertFalse(value['publication_verified'])
                self.assertEqual(datetime.fromisoformat(value['utc']).timestamp(), epoch)
                self.assertEqual(datetime.fromisoformat(value['beijing']).timestamp(), epoch)
                self.assertTrue(value['beijing'].endswith('+08:00'))
        self.assertIsNone(schedule.native_timer({}))
        self.assertIsNone(schedule.native_timer(dict(timer=None)))
        self.assertEqual(schedule.native_timer(dict(timer=dict(status=1, public_time=0)))['state'], 'online')

    def test_native_timer_unknown_or_malformed_values_cannot_be_called_published(self):
        for value in [None, [], dict(timer={}), dict(timer=[]),
                      *[dict(timer=dict(status=v, public_time=1793442600)) for v in [True, '1', 2, 3, -1]],
                      *[dict(timer=dict(status=1, public_time=v)) for v in [None, True, '1793442600', -1, 10**100]],
                      *[dict(timer=dict(status=v, public_time=0)) for v in [0, 4, 5]]]:
            with self.subTest(value=value), self.assertRaises(PublishError):
                schedule.native_timer(value)

    def test_equivalent_zones_and_beijing_display(self):
        expected = schedule.requested(META)
        self.assertEqual(expected, schedule.requested({'publish_at': '2070-10-30T10:30:00Z'}))
        self.assertEqual(expected['beijing'], META['publish_at'])

    def test_missing_is_legacy_immediate_and_empty_is_not(self):
        self.assertIsNone(schedule.requested({}))
        self.assertIsNone(schedule.fill(None, {}))
        self.assertIsNone(schedule.verify(None, {}, None))
        for value in [None, '', False, 123, '2070-10-30T18:30:00', '2070-10-30T18:30:01+08:00',
                      '2070-10-30T18:30:00.000+08:00', '2070-02-30T18:30:00Z', '2070-10-30T18:30:00+25:00',
                      '2070-10-30T18:30:00+08:60', '2070-10-30T18:30:00-00:00',
                      '0001-01-01T00:00:00+23:59']:
            with self.subTest(value=value), self.assertRaises(PublishError):
                schedule.requested({'publish_at': value})

    def test_past_rejected_before_any_fill(self):
        p = Page()
        with self.assertRaises(PublishError): schedule.fill(p, {'publish_at': '2000-01-01T00:00:00Z'})
        self.assertEqual((p.clicks, p.fills), (0, 0))

    def test_native_fill_enter_blur_and_readonly_resume(self):
        p = Page(); receipt = schedule.fill(p, META)
        self.assertEqual(p.keys, ['Enter', 'Tab'])
        self.assertEqual(receipt['assurance'], 'visible_editor_only')
        self.assertEqual(schedule.verify(p, META, receipt), receipt)
        self.assertEqual((p.clicks, p.fills), (1, 1))

    def test_browser_zone_is_converted_not_forced(self):
        p = Page(); p.offset = -240; p.timezone = 'America/New_York'
        receipt = schedule.fill(p, META)
        self.assertEqual(receipt['input'], '2070-10-30 06:30')
        self.assertEqual(receipt['beijing'], META['publish_at'])

    def test_uncommitted_or_clamped_date_rejected(self):
        p = Page(); p.clamp = True
        with self.assertRaises(PublishError): schedule.fill(p, META)

    def test_receipt_or_current_date_or_zone_drift_rejected_without_rewrite(self):
        for change in ['receipt', 'date', 'zone', 'visibility', 'duplicate', 'selected']:
            with self.subTest(change=change):
                p = Page(); receipt = schedule.fill(p, META)
                if change == 'receipt': receipt['epoch'] += 60
                if change == 'date': p.value = '2070-10-30 18:31'
                if change == 'zone': p.timezone = 'Etc/GMT-8'
                if change == 'visibility': p.visible = False
                if change == 'duplicate': p.counts['field'] = 2
                if change == 'selected': p.scheduled = False
                with self.assertRaises(PublishError): schedule.verify(p, META, receipt)
                self.assertEqual((p.clicks, p.fills), (1, 1))

    def test_inconsistent_browser_context_rejected(self):
        for value in [dict(), dict(input='2070-10-30 18:30', offset_minutes=True, timezone='X', now=0),
                      dict(input='2070-10-30 18:29', offset_minutes=480, timezone='X', now=0)]:
            p = Page(); p.context_override = value
            with self.subTest(value=value), self.assertRaises(PublishError): schedule.fill(p, META)
            self.assertEqual(p.fills, 0)

    def test_ambiguous_local_time_cannot_silently_choose_another_instant(self):
        p = Page(); value = p.evaluate(schedule.DATE_CONTEXT, schedule.requested(META)['epoch'])
        value['roundtrip_epoch'] -= 3600; p.context_override = value
        with self.assertRaises(PublishError): schedule.fill(p, META)
        self.assertEqual(p.fills, 0)

    def test_calendar_day_bound_observes_dst(self):
        now = datetime.fromisoformat('2026-03-07T12:00:00-05:00').timestamp()
        within = {'epoch': now+23*3600}
        schedule.limits('支持1分钟后及1天内的定时发布', within, now, 'America/New_York')
        with self.assertRaises(PublishError):
            schedule.limits('支持1分钟后及1天内的定时发布', {'epoch': now+23.5*3600}, now, 'America/New_York')

    def test_options_missing_duplicate_disabled_numeric_bool_or_wrong_group_rejected(self):
        variants = [[], rows(True), rows(False)+[rows(False)[0]]]
        for field, val in [('disabled', True), ('checked', 1), ('value', '2'), ('type', 'radio')]:
            value = rows(False); value[0]['inputs'][0][field] = val; variants.append(value)
        for value in variants:
            p = Page(); p.options = value
            with self.subTest(value=value), self.assertRaises(PublishError): schedule.fill(p, META)
            self.assertEqual((p.clicks, p.fills), (0, 0))

    def test_account_bounds_are_read_not_hardcoded(self):
        expected = schedule.requested(META)
        schedule.limits('支持10分钟后及14天内的定时发布', expected, expected['epoch']-1200)
        for text in ['支持2小时后及14天内的定时发布', '',
                     '支持10分钟后及14天内的定时发布 支持10分钟后及14天内的定时发布',
                     '支持15天后及14天内的定时发布']:
            with self.subTest(text=text), self.assertRaises(PublishError):
                schedule.limits(text, expected, expected['epoch']-1200)

    def test_unknown_or_out_of_range_bounds_stop_before_input(self):
        for hint in ['', '支持2小时后及3小时内的定时发布']:
            p = Page(); p.hint = hint
            with self.subTest(hint=hint), self.assertRaises(PublishError): schedule.fill(p, META)
            self.assertEqual(p.fills, 0)


if __name__ == '__main__': unittest.main()
