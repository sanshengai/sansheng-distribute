import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from campaign import authorize_campaign, campaign_status, prepare_campaign, preflight_campaign, run_campaign
from publish_core import PublishError, Store, digest, validate, write_json


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source.txt").write_text("原定稿文字", encoding="utf-8")
        self.manifest = {
            "schema_version": 1, "job_id": "campaign-test", "producer": "test",
            "content_status": "final", "source_root": str(self.root),
            "documents": [{"id": "source", "path": "source.txt", "format": "text"}],
            "assets": [], "media_slots": [],
            "targets": [
                {"id": "x", "platform": "x", "account": "social", "endpoint": "draft", "content_type": "text", "text": "原定稿文字", "document_id": "source"},
                {"id": "weibo", "platform": "weibo", "account": "social", "endpoint": "draft", "content_type": "text", "text": "原定稿文字", "document_id": "source"},
            ],
        }
        validate(self.manifest, sealing=True)
        self.path = self.root / "manifest.json"
        write_json(self.path, self.manifest)
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.script = self.root / "adapter.py"
        self.script.write_text('''import json,sys
from pathlib import Path
root=Path(__file__).parent
r=json.loads(Path(sys.argv[-1]).read_text())
t=r['target']; action=sys.argv[1]
with (root/'calls.jsonl').open('a') as out: out.write(json.dumps([t['id'],action])+'\\n')
mode=json.loads((root/'modes.json').read_text()).get(t['id'],'ok')
if mode=='crash' and action=='execute': raise SystemExit(5)
if mode=='unknown': raise SystemExit(6)
o={'manifest_digest':r['manifest_digest'],'target_id':t['id'],'platform':t['platform'],'account':t['account'],'remote_id':'remote-'+t['id'],'state':'draft_ready','content_verified':True}
if mode=='absent' and action=='verify': o.update(state='not_submitted',remote_id=None,absence_verified=True)
if mode=='cover_pending': o.update(state='pending',stage='cover_editing_required')
print(json.dumps(o))
''')
        write_json(self.root / "modes.json", {})
        self.profile = {
            "accounts": {"social": {"platforms": ["x", "weibo"]}},
            "adapters": {p: {"enabled_capabilities": ["text:draft"], "execute": [sys.executable, str(self.script), "execute"], "verify": [sys.executable, str(self.script), "verify"]} for p in ("x", "weibo")},
        }
        self.cid = prepare_campaign(self.path, self.store)["campaign_id"]

    def authorize(self, targets=None):
        return authorize_campaign(self.cid, self.store, "测试夹具：只在模拟器中创建所选测试草稿", targets)

    def calls(self):
        p = self.root / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def states(self, report):
        return {x["target_id"]: x["state"] for x in report["targets"]}

    def test_local_preflight_reports_all_targets_without_running_or_granting(self):
        self.profile['adapters']['x']['enabled_capabilities'] = []
        before = self.store.status()
        report = preflight_campaign(self.path, self.profile)
        self.assertEqual([r['local_ready'] for r in report['targets']], [False, True])
        self.assertEqual((report['ready_count'], report['blocked_count']), (1, 1))
        self.assertFalse(report['all_local_checks_passed'])
        self.assertFalse(report['authorization_checked'])
        self.assertFalse(report['remote_checked'])
        self.assertIn('尚未启用', report['targets'][0]['error'])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.store.status(), before)
        for table in ['grants', 'events', 'timings', 'account_locks']:
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)

    def test_local_preflight_selection_and_changed_source_fail_closed(self):
        self.profile['adapters']['x']['enabled_capabilities'] = []
        report = preflight_campaign(self.path, self.profile, ['weibo'])
        self.assertTrue(report['all_local_checks_passed'])
        self.assertEqual(report['selected_targets'], ['weibo'])
        for selected in ([], ['missing'], ['x', 'x']):
            with self.assertRaises(PublishError):
                preflight_campaign(self.path, self.profile, selected)
        (self.root / 'source.txt').write_text('被修改的原稿', encoding='utf-8')
        with self.assertRaises(PublishError):
            preflight_campaign(self.path, self.profile)
        self.assertEqual(self.calls(), [])

    def test_batch_preflight_hashes_shared_source_once_then_rechecks(self):
        source = (self.root / 'source.txt').resolve()
        real_open = Path.open
        reads = []
        def counted(path, *args, **kwargs):
            if path == source and args and args[0] == 'rb':
                reads.append(path)
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', counted):
            report = preflight_campaign(self.path, self.profile)
        self.assertTrue(report['all_local_checks_passed'])
        self.assertEqual(len(reads), 2)  # cached pass, then uncached final pass
        self.assertEqual(self.calls(), [])

    def test_batch_preflight_rechecks_bytes_even_if_metadata_cache_misses_change(self):
        # Simulate an unreliable filesystem timestamp: a changed source must
        # still be rejected by the independent full final validation.
        import campaign
        source = self.root / 'source.txt'
        original = campaign.preflight
        def mutate_after_check(*args, **kwargs):
            result = original(*args, **kwargs)
            source.write_text('改动原稿文字', encoding='utf-8')
            return result
        with patch('publish_core._fingerprint', return_value=(1, 2, 3, 4, 5)), \
             patch('campaign.preflight', side_effect=mutate_after_check):
            with self.assertRaisesRegex(PublishError, '源文件已改变'):
                preflight_campaign(self.path, self.profile)
        self.assertEqual(self.calls(), [])

    def test_batch_cache_does_not_reuse_digest_after_same_size_edit(self):
        from publish_core import batch_preflight_hash_cache, file_hash
        source = (self.root / 'source.txt').resolve()
        with batch_preflight_hash_cache():
            before = file_hash(source)
            source.write_bytes(b'x' * source.stat().st_size)
            after = file_hash(source)
        self.assertNotEqual(before, after)

    def test_local_preflight_cli_exit_status_and_no_state_directory(self):
        cli = Path(__file__).resolve().parents[1] / 'scripts' / 'publish.py'
        config = self.root / 'profile.json'
        state = self.root / 'not-created'
        self.profile['adapters']['x']['enabled_capabilities'] = []
        write_json(config, self.profile)
        command = [sys.executable, str(cli), '--profile', str(config), '--state-dir', str(state),
                   'batch-preflight', str(self.path)]
        first = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(first.returncode, 2, first.stdout + first.stderr)
        result = json.loads(first.stdout)
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['result']['targets']), 2)
        self.assertFalse(state.exists())
        second = subprocess.run(command+['--target', 'weibo'], text=True, capture_output=True)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(json.loads(second.stdout)['ok'])
        self.assertFalse(state.exists())
        self.assertEqual(self.calls(), [])

    def test_partial_failure_continues_and_unknown_never_resends(self):
        self.authorize()
        write_json(self.root / "modes.json", {"x": "unknown"})
        first = run_campaign(self.cid, self.profile, self.store)
        self.assertEqual(self.states(first), {"x": "submission_unknown", "weibo": "draft_ready"})
        self.assertEqual(first["outcomes"], {"x": "error", "weibo": "executed"})
        self.assertIn("error", first["targets"][0])
        run_campaign(self.cid, self.profile, self.store)
        self.assertEqual(self.calls(), [["x", "execute"], ["weibo", "execute"], ["weibo", "verify"], ["x", "verify"]])

    def test_completed_targets_and_repeated_prepare_do_not_resend(self):
        self.authorize()
        report = run_campaign(self.cid, self.profile, self.store)
        self.assertTrue(report["all_endpoints_reached"])
        self.assertEqual(prepare_campaign(self.path, self.store)["campaign_id"], self.cid)
        run_campaign(self.cid, self.profile, self.store)
        self.assertEqual(len(self.calls()), 4)

    def test_scoped_authorization_and_execution(self):
        self.authorize(["weibo"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM grants").fetchone()[0], 1)
        report = run_campaign(self.cid, self.profile, self.store)
        self.assertEqual(self.states(report), {"x": "prepared", "weibo": "draft_ready"})
        self.assertEqual(self.calls(), [["weibo", "execute"], ["weibo", "verify"]])
        self.assertIn("授权", report["targets"][0]["error"])
        for selection in ([], ["missing"], ["x", "x"]):
            with self.assertRaises(PublishError):
                self.authorize(selection)
        with self.assertRaises(PublishError):
            authorize_campaign(self.cid, self.store, " ")

    def test_resume_survives_original_manifest_and_sources_cleanup(self):
        self.authorize()
        write_json(self.root / "modes.json", {"x": "crash"})
        run_campaign(self.cid, self.profile, self.store)
        self.path.unlink()
        (self.root / "source.txt").unlink()
        report = run_campaign(self.cid, self.profile, self.store, resume=True)
        self.assertTrue(report["all_endpoints_reached"])
        self.assertEqual(self.calls().count(["x", "execute"]), 1)
        self.assertEqual(report["outcomes"]["x"], "queried")
        self.assertNotIn("error", report["targets"][0])

    def test_resume_never_starts_prepared_or_absence_verified_target(self):
        self.authorize()
        report = run_campaign(self.cid, self.profile, self.store, resume=True)
        self.assertEqual(report["outcomes"], {"x": "not_started", "weibo": "not_started"})
        self.assertEqual(self.calls(), [])
        write_json(self.root / "modes.json", {"x": "crash"})
        run_campaign(self.cid, self.profile, self.store, ["x"])
        write_json(self.root / "modes.json", {"x": "absent"})
        report = run_campaign(self.cid, self.profile, self.store, resume=True)
        self.assertEqual(self.states(report), {"x": "prepared", "weibo": "prepared"})
        self.assertEqual(self.calls(), [["x", "execute"], ["x", "verify"]])

    def test_definition_and_recovery_snapshot_tampering_rejected(self):
        self.authorize()
        key = campaign_status(self.cid, self.store)["targets"][0]["key"]
        p = self.store.directory / "snapshots" / f"{key}.json"
        m = copy.deepcopy(self.manifest)
        m["targets"][0]["text"] = "未经批准"
        write_json(p, m)
        report = run_campaign(self.cid, self.profile, self.store, ["x"])
        self.assertIn("不一致", report["targets"][0]["error"])
        self.assertEqual(self.calls(), [])
        definition_path = self.store.directory / "campaigns" / f"{self.cid}.json"
        definition = json.loads(definition_path.read_text())
        definition["target_ids"] = ["x"]
        write_json(definition_path, definition)
        with self.assertRaisesRegex(PublishError, "不一致"):
            campaign_status(self.cid, self.store)

    def test_empty_manifest_and_out_of_campaign_scope_rejected(self):
        write_json(self.path, {})
        with self.assertRaises(PublishError):
            prepare_campaign(self.path, self.store)
        write_json(self.path, self.manifest)
        subset = prepare_campaign(self.path, self.store, ["weibo"])["campaign_id"]
        with self.assertRaises(PublishError):
            authorize_campaign(subset, self.store, "测试夹具", ["x"])
        with self.assertRaises(PublishError):
            run_campaign(subset, self.profile, self.store, ["x"])

    def test_same_account_lock_blocks_next_target_but_other_platform_continues(self):
        extra = dict(self.manifest["targets"][0], id="x2", text="第二条")
        self.manifest["targets"].insert(1, extra)
        write_json(self.path, self.manifest)
        self.cid = prepare_campaign(self.path, self.store)["campaign_id"]
        self.authorize()
        write_json(self.root / "modes.json", {"x": "unknown"})
        report = run_campaign(self.cid, self.profile, self.store)
        self.assertEqual(self.states(report), {"x": "submission_unknown", "x2": "prepared", "weibo": "draft_ready"})
        self.assertIn("账号", report["targets"][1]["error"])
        self.assertNotIn(["x2", "execute"], self.calls())

    def test_xiaohongshu_publication_cannot_enter_campaign(self):
        self.manifest["targets"][0].update(platform="xiaohongshu", content_type="images", endpoint="publish")
        write_json(self.path, self.manifest)
        with self.assertRaisesRegex(PublishError, "作者"):
            prepare_campaign(self.path, self.store)

    def test_incomplete_cover_reports_inspect_and_does_not_claim_all_endpoints(self):
        self.authorize()
        write_json(self.root/'modes.json',{'weibo':'cover_pending'})
        report=run_campaign(self.cid,self.profile,self.store)
        self.assertFalse(report['all_endpoints_reached'])
        row=next(t for t in report['targets'] if t['target_id']=='weibo')
        self.assertEqual(row['state'],'pending');self.assertEqual(row['next_action'],'inspect')
        self.assertIn('封面方向',row['author_action'])
        before=self.calls()
        run_campaign(self.cid,self.profile,self.store,resume=True)
        self.assertEqual(self.calls()[len(before):],[['weibo','verify']])


if __name__ == "__main__":
    unittest.main()
