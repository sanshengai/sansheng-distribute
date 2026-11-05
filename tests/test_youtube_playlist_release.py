"""Unknown playlist insert may be released only after read-only proof the video is absent."""
import unittest
from unittest.mock import patch
import test_youtube_component_ledger as base
from publish_core import PublishError, run_youtube_component, release_youtube_playlist

class PlaylistReleaseTest(unittest.TestCase):
    setUp=base.ComponentLedgerTest.setUp
    task=base.ComponentLedgerTest.task
    response=base.ComponentLedgerTest.response
    configure_component=base.ComponentLedgerTest.configure_component

    def unknown(self):
        self.configure_component();self.spec['cover']=None
        key=self.task()['key'];self.store.authorize_youtube_component(key,'actual user authorization')
        def fail(_c,action,_p,**k):raise PublishError('YouTube 请求未完成；先查询同一任务')
        with patch('youtube_adapter.preflight_youtube'), patch('publish_core.invoke',side_effect=fail):
            with self.assertRaises(PublishError):run_youtube_component(self.store,key,'playlist',self.profile)
        self.assertEqual(self.store.youtube_component_status(key)['playlist_state'],'unknown')
        return key
    def lock(self):
        return self.store.db.execute('SELECT job_key FROM account_locks WHERE platform=? AND account=?',('youtube','owner')).fetchone()

    def test_absent_releases_lock_and_requires_new_grant(self):
        key=self.unknown();self.assertIsNotNone(self.lock())
        obs=dict(self.response(key,'playlist','unknown'),absent=True)
        with patch('youtube_adapter.preflight_youtube'), patch('publish_core.invoke',return_value=obs) as call:
            r=release_youtube_playlist(self.store,key,self.profile)
        self.assertEqual([c.args[1] for c in call.call_args_list],['verify_component'])
        self.assertEqual(r['playlist_state'],'prepared');self.assertIsNone(self.lock())
        with patch('youtube_adapter.preflight_youtube'), patch('publish_core.invoke') as call:
            with self.assertRaises(PublishError):run_youtube_component(self.store,key,'playlist',self.profile)
            call.assert_not_called()

    def test_unproven_absence_keeps_unknown(self):
        key=self.unknown()
        with patch('youtube_adapter.preflight_youtube'), patch('publish_core.invoke',return_value=self.response(key,'playlist','unknown')):
            with self.assertRaises(PublishError):release_youtube_playlist(self.store,key,self.profile)
        self.assertEqual(self.store.youtube_component_status(key)['playlist_state'],'unknown');self.assertIsNotNone(self.lock())

    def test_present_is_verified_not_released(self):
        key=self.unknown()
        with patch('youtube_adapter.preflight_youtube'), patch('publish_core.invoke',return_value=self.response(key,'playlist','verified')):
            self.assertEqual(release_youtube_playlist(self.store,key,self.profile)['playlist_state'],'verified')

    def test_non_unknown_refused(self):
        self.configure_component();self.spec['cover']=None;key=self.task()['key']
        with patch('publish_core.invoke') as call:
            with self.assertRaises(PublishError):release_youtube_playlist(self.store,key,self.profile)
            call.assert_not_called()

if __name__=='__main__':unittest.main()
