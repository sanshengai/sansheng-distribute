import importlib.util
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'interaction_dashboard.py'
spec = importlib.util.spec_from_file_location('interaction_dashboard', SCRIPT)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def test_watch_health_includes_drafts_without_creating_missing_records(self):
        from interaction_monitor import Monitor
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(dashboard.watch_note(root, 'youtube', 'owner', 'channel'), '')
            self.assertEqual(list(root.iterdir()), [])
            with Monitor(root, 'youtube', 'owner', 'channel', interval=1800, cycles=3) as run:
                run.begin_cycle(); run.record_cycle('partial'); run.sleep()
                note = dashboard.watch_note(root, 'youtube', 'owner', 'channel')
                self.assertIn('等待下轮', note); self.assertIn('1/3 轮', note)
                self.assertIn('部分覆盖 1', note); self.assertIn('下次', note)
                run.finish('completed', 'test_done')
            self.assertIn('有限观察已结束', dashboard.watch_note(root, 'youtube', 'owner', 'channel'))
            with Monitor(root, 'draft-youtube', 'owner', 'channel', interval=1800, cycles=1) as draft:
                draft.finish('failed', 'test_failure')
            self.assertIn('拟稿观察：失败已停止', dashboard.watch_note(root, 'youtube', 'owner', 'channel'))

    def test_watch_health_distinguishes_overdue_stopped_and_corrupt_records(self):
        from interaction_monitor import Monitor, snapshot
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); stamp = datetime.now(timezone.utc)
            run = Monitor(root, 'weibo', 'owner', '101', interval=1800, cycles=2, clock=lambda: stamp).__enter__()
            try:
                run.begin_cycle(); run.sleep()
                with patch.object(dashboard, 'monitor_snapshot', side_effect=lambda *a: snapshot(*a, clock=lambda: stamp + timedelta(seconds=2200))):
                    self.assertIn('检查超时', dashboard.watch_note(root, 'weibo', 'owner', '101'))
            finally:
                run.file.close(); run.file = None
            self.assertIn('意外停止', dashboard.watch_note(root, 'weibo', 'owner', '101'))
            run.path.write_text('{}'); run.path.chmod(0o600)
            self.assertIn('记录无法核验', dashboard.watch_note(root, 'weibo', 'owner', '101'))

    def test_partial_is_not_zero_and_comment_text_is_escaped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'inbox.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE interaction_sources (platform, account, channel_id, state, last_success_at, error, parents_scanned, parents_total, reply_incomplete)')
                db.execute('CREATE TABLE interaction_items (platform, account, channel_id, is_own, local_status, video_id, author_name, body, published_at, comment_id)')
                db.execute('INSERT INTO interaction_sources VALUES (?,?,?,?,?,?,?,?,?)',
                           ('youtube', 'one', 'channel', 'partial', '2026-10-28T02:00:00Z', '<error>', 2, 5, 1))
                db.execute('INSERT INTO interaction_items VALUES (?,?,?,?,?,?,?,?,?,?)',
                           ('youtube', 'one', 'channel', 0, 'new', '12345678901', '<name>', '<script>alert(1)</script>', '2026-10-28T02:00:00Z', 'c1'))
                db.execute('INSERT INTO interaction_items VALUES (?,?,?,?,?,?,?,?,?,?)',
                           ('youtube', 'one', 'channel', 1, 'new', None, 'self', 'own', None, 'c2'))
                db.execute('INSERT INTO interaction_sources VALUES (?,?,?,?,?,?,?,?,?)',
                           ('weibo', 'two', 'channel2', 'manual', None, None, 0, 0, 0))
                db.execute('INSERT INTO interaction_items VALUES (?,?,?,?,?,?,?,?,?,?)',
                           ('weibo', 'two', 'channel2', 0, 'new', 'v2', 'other', 'hello', None, 'c3'))
                db.execute('INSERT INTO interaction_sources VALUES (?,?,?,?,?,?,?,?,?)',
                           ('x', 'three', 'channel3', 'partial', None, 'only mentions', 0, 0, 0))
                db.execute('INSERT INTO interaction_items VALUES (?,?,?,?,?,?,?,?,?,?)',
                           ('x', 'three', 'channel3', 0, 'new', None, 'other', 'hello', None, '123456789'))
                db.execute('CREATE TABLE interaction_manual_provenance (platform,account,channel_id,comment_id)')
                db.execute('INSERT INTO interaction_manual_provenance VALUES (?,?,?,?)', ('weibo', 'two', 'channel2', 'c3'))
                db.commit()
            page = dashboard.render(path, True, ['youtube', 'xiaohongshu'])
            self.assertIn('检查不完整', page)
            self.assertIn('无同步记录', page)
            self.assertIn('评论串 2/5', page)
            self.assertIn('人工导入（未同步）', page)
            self.assertIn('含人工导入 1 条', page)
            self.assertIn('&lt;script&gt;', page)
            self.assertNotIn('<script>alert(1)</script>', page)
            self.assertNotIn('own</p>', page)
            self.assertIn('10-28 10:00', page)
            self.assertIn('youtube.com/watch?v=12345678901', page)
            self.assertIn('https://x.com/i/web/status/123456789', page)

    def test_missing_db_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'missing.sqlite3'
            with self.assertRaises(FileNotFoundError):
                dashboard.render(path)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
