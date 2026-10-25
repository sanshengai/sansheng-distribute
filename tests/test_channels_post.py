"""Native frontend contracts; fixtures are synthetic, not live acceptance."""
from contextlib import contextmanager
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import channels_post as post
from publish_core import PublishError

UID = 'fixture-account@finder'
RID = 'export/fixtureRemoteObject12345'


def native():
    return dict(errCode=0, data=dict(baseResp=dict(errcode=0), list=[dict(
        objectId=RID, status=1, visibleType=1,
        originalInfo=dict(isDeclared=1, auditOriginalFlag=0),
        desc=dict(description='「系列」S1E01｜标题\n完整正文 #英语 #校园 ', mediaType=2,
                  shortTitle=[dict(shortTitle='系列 第一集 标题')], media=[dict(
                      videoPlayLen=113, width=1080, height=1920, fileSize='67052772',
                      md5sum='a' * 32, thumbUrl='https://wx.qpic.cn/fixture/cover')]))]))


def value(body=None, request_identity=UID):
    return post.item(native() if body is None else body, expected_native_identity=UID,
                     request_identity=request_identity, remote_id=RID)


class ChannelsPostTests(unittest.TestCase):
    def test_originality_pending_does_not_mean_post_processing(self):
        facts = value()
        self.assertEqual(facts['description'], native()['data']['list'][0]['desc']['description'])
        self.assertEqual(facts['short_title'], '系列 第一集 标题')
        self.assertEqual(facts['states']['originality']['audit'], 'pending')
        self.assertEqual(facts['states']['processing'], 'not_reported')
        self.assertTrue(facts['states']['public_list_candidate'])
        self.assertFalse(facts['states']['publication_verified'])
        self.assertNotIn('content_verified', facts)

    def test_schedule_processing_visibility_and_unknown_status_stay_distinct(self):
        for changes, expected in [({'effectiveTime': 2000000000}, 'scheduled'),
                                  ({'handleStatus': 1}, 'processing'),
                                  ({'handleStatus': 3}, 'failed'),
                                  ({'handleStatus': 77}, 'unknown'),
                                  ({'handleStatus': 0}, 'unknown'),
                                  ({'visibleType': 3}, 'self'), ({'status': 77}, 'status')]:
            with self.subTest(changes=changes):
                body = native(); body['data']['list'][0].update(changes)
                state = value(body)['states']
                self.assertFalse(state['public_list_candidate'])
                if expected == 'scheduled': self.assertTrue(state['scheduled'])
                elif expected == 'self': self.assertEqual(state['visibility'], 'self')
                elif expected != 'status': self.assertEqual(state['processing'], expected)
        for flag, expected in [(1, 'passed'), (2, 'rejected'), (8, 'unknown')]:
            body = native(); body['data']['list'][0]['originalInfo']['auditOriginalFlag'] = flag
            self.assertEqual(value(body)['states']['originality']['audit'], expected)
            self.assertFalse(value(body)['states']['publication_verified'])

    def test_no_identity_guess_from_sph_name_or_cookie(self):
        body = dict(errCode=0, data=dict(finderUser=dict(finderUsername=UID)))
        self.assertEqual(post.account(body, UID), UID)
        for expected in ['', 'sphFixturePublic', '作者 名字', None]:
            with self.subTest(expected=expected), self.assertRaises(PublishError):
                post.account(body, expected)
        body['data']['finderUser']['finderUsername'] = 'other-account@finder'
        with self.assertRaises(PublishError): post.account(body, UID)
        for ident in [None, '', 'other-account@finder']:
            with self.assertRaises(PublishError): value(request_identity=ident)

    def test_missing_duplicate_or_failed_response_never_proves_absence(self):
        cases = [{}, dict(errCode=True, data={}), dict(errCode=1, data={})]
        for change in [lambda b: b['data'].update(list=[]),
                       lambda b: b['data']['list'][0].update(objectId='export/otherObjectSameTitle123'),
                       lambda b: b['data']['list'].append(copy.deepcopy(b['data']['list'][0])),
                       lambda b: b['data'].update(baseResp=dict(errcode=1)),
                       lambda b: b['data'].update(baseResp=dict(errcode=False))]:
            body = native(); change(body); cases.append(body)
        for body in cases:
            with self.subTest(body=body), self.assertRaises(PublishError): value(body)
        for ident in ['https://weixin.qq.com/sph/fixture', '同名作品', '', None, 'export/../another']:
            with self.assertRaises(PublishError): post.object_id(ident)

    def test_short_title_media_and_state_are_not_silently_normalized(self):
        edits = [lambda r: r['desc'].update(description=None),
                 lambda r: r['desc'].update(shortTitle='legacy unsupported scalar'),
                 lambda r: r['desc'].update(shortTitle=[dict(shortTitle='A'), dict(shortTitle='B')]),
                 lambda r: r['desc']['media'][0].update(md5sum='invalid'),
                 lambda r: r['desc']['media'][0].update(fileSize=123),
                 lambda r: r['desc']['media'][0].update(width=True),
                 lambda r: r['desc']['media'][0].update(thumbUrl='javascript:fixture'),
                 lambda r: r.update(visibleType=True), lambda r: r.update(effectiveTime=-1),
                 lambda r: r.update(originalInfo=dict(isDeclared=True, auditOriginalFlag=0)),
                 lambda r: r.update(originalInfo=dict(isDeclared=1))]
        for edit in edits:
            body = native(); edit(body['data']['list'][0])
            with self.subTest(edit=edit), self.assertRaises(PublishError): value(body)

    def page(self):
        page = Mock(); card = Mock(); title = Mock(); image = Mock()
        page.locator.return_value.all.return_value = [card]
        card.locator.side_effect = lambda selector: title if selector == '.post-title' else image
        card.is_visible.return_value = True
        title.count.return_value = 1; title.text_content.return_value = value()['description']
        image.count.return_value = 1; image.is_visible.return_value = True
        image.evaluate.return_value = dict(src=value()['media']['thumb_url'], loaded=True)
        return page, card, title, image

    def test_visible_card_requires_full_text_unique_card_and_loaded_exact_thumb(self):
        page, card, title, image = self.page(); post.observe_visible(page, value())
        for obj, attr, bad in [(title, 'text_content', '截断的正文'), (image, 'count', 2),
                               (card, 'is_visible', False),
                               (image, 'evaluate', dict(src='https://wx.qpic.cn/other', loaded=True)),
                               (image, 'evaluate', dict(src=value()['media']['thumb_url'], loaded=False))]:
            fn = getattr(obj, attr); old = fn.return_value; fn.return_value = bad
            with self.subTest(attr=attr), self.assertRaises(PublishError): post.observe_visible(page, value())
            fn.return_value = old
        page.locator.return_value.all.return_value = [card, card]
        with self.assertRaises(PublishError): post.observe_visible(page, value())

    def read(self, mode='valid'):
        page, card, title, image = self.page(); listeners = {}; calls = []
        page.url = post.MANAGER
        page.on.side_effect = lambda event, fn: listeners.update({event: fn})
        def response(path, body, payload):
            r = Mock(); r.url = post.ORIGIN + path; r.status = 200
            r.request.method = 'POST'; r.request.frame.page = page
            r.request.post_data_json = payload; r.json.return_value = body
            return r
        auth = response(post.AUTH_PATH, dict(errCode=0, data=dict(finderUser=dict(finderUsername=UID))), {})
        listed = response(post.LIST_PATH, native(), dict(_log_finder_id=UID))
        def goto(*args, **kw):
            calls.append('navigate')
            if mode == 'login': page.url = post.ORIGIN + '/login.html'; return
            if mode == 'foreign_origin': listed.url = 'https://channels.weixin.qq.com.evil.test' + post.LIST_PATH
            elif mode == 'other_frame': listed.request.frame.page = Mock()
            elif mode == 'wrong_account': listed.request.post_data_json = dict(_log_finder_id='other@finder')
            elif mode == 'http_failure': listed.status = 500
            elif mode == 'conflicting':
                b = native(); b['data']['list'][0]['desc']['description'] = 'changed'
                listeners['response'](response(post.LIST_PATH, b, dict(_log_finder_id=UID)))
            listeners['response'](auth); listeners['response'](listed)
        page.goto.side_effect = goto
        @contextmanager
        def window(*a, **kw):
            self.assertNotIn('retain', kw)
            try: yield page
            finally: calls.append('closed_owned_window')
        # A short fake monotonic clock prevents missing-response tests from waiting.
        clock = iter([0, 21, 42, 63])
        with patch.object(post, 'background_window', window), patch.object(post.time, 'monotonic', lambda: next(clock)):
            try:
                result = post.readback(Mock(), cdp_url='http://127.0.0.1:9333',
                                       expected_native_identity=UID, remote_id=RID)
                return result, page, calls
            finally:
                page.remove_listener.assert_called_once_with('response', listeners['response'])
                self.assertIn('closed_owned_window', calls)

    def test_readback_is_partial_and_uses_native_requests_without_submitting(self):
        result, page, calls = self.read()
        self.assertEqual(result['remote_id'], RID)
        self.assertFalse(result['content_verified']); self.assertFalse(result['source_media_verified'])
        self.assertIn('collection_membership', result['unverified_fields'])
        self.assertEqual(calls, ['navigate', 'closed_owned_window'])
        page.click.assert_not_called(); page.request.post.assert_not_called()

    def test_readback_refuses_unbound_responses_changed_facts_and_login(self):
        for mode in ['foreign_origin', 'other_frame', 'wrong_account', 'http_failure', 'conflicting', 'login']:
            with self.subTest(mode=mode), self.assertRaises(PublishError): self.read(mode)

    def test_invalid_target_fails_before_creating_browser_window(self):
        with patch.object(post, 'background_window') as window:
            for kwargs in [dict(expected_native_identity='sphPublicId', remote_id=RID),
                           dict(expected_native_identity=UID, remote_id='标题')]:
                with self.assertRaises(PublishError): post.readback(Mock(), cdp_url='http://127.0.0.1:9333', **kwargs)
            window.assert_not_called()


if __name__ == '__main__':
    unittest.main()
