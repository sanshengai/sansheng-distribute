import copy
import json
import stat
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

import test_campaign as fixture

from campaign import prepare_campaign
from campaign_report import remote_link, render_report, write_report
from publish_core import PublishError, validate, write_json


class _HTMLAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs).get("href"))


class CampaignReportTests(unittest.TestCase):
    # Reuse the fixture methods without inheriting CampaignTests' test methods.
    setUp = fixture.CampaignTests.setUp
    calls = fixture.CampaignTests.calls

    def make_campaign(self):
        first, second = self.manifest["targets"]
        first.update(endpoint="publish", title='已发布 <script>alert(1)</script>',
                     text='文字 <img src=x onerror=alert(2)>',
                     tags=['真实话题', '<svg onload=alert(3)>'],
                     platform_metadata={'collection': '计划合集', 'is_ai_generated': True})
        second.update(title="草稿标题", text="草稿内容")
        third = copy.deepcopy(second)
        third.update(id="weibo-unknown", title="待核实的作品")
        self.manifest["targets"].append(third)
        validate(self.manifest, sealing=True)
        write_json(self.path, self.manifest)
        self.cid = prepare_campaign(self.path, self.store)["campaign_id"]
        states = {"x": ("published", "https://example.test/post/1"),
                  "weibo": ("draft_ready", None),
                  "weibo-unknown": ("submission_unknown", None)}
        with self.store.db:
            for target in self.manifest["targets"]:
                state, url = states[target["id"]]
                self.store.db.execute("UPDATE jobs SET state=?, remote_url=? WHERE key=?",
                                      (state, url, self.store.key(self.manifest, target)))

    def test_states_planned_fields_and_no_remote_side_effect(self):
        self.make_campaign()
        before = self.store.status()
        self.assertEqual(self.calls(), [])
        rendered = render_report(self.cid, self.store)
        self.assertIn('<span class="status ok">已发布</span>', rendered)
        self.assertIn('<span class="status wait">草稿已就绪</span>', rendered)
        self.assertIn('<span class="status warn">结果待确认</span>', rendered)
        self.assertIn("共 3 个目标", rendered)
        self.assertIn("查询前不要重复提交", rendered)
        self.assertIn("话题（计划）", rendered)
        self.assertIn("封面和声明（计划）", rendered)
        self.assertIn("不代表平台已确认", rendered)
        self.assertIn("计划合集", rendered)
        self.assertIn("AI 生成：是", rendered)
        self.assertIn("并非实时刷新", rendered)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.store.status(), before)

    def test_untrusted_text_and_links_cannot_be_executed(self):
        self.make_campaign()
        rendered = render_report(self.cid, self.store)
        parser = _HTMLAudit()
        parser.feed(rendered)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertNotIn("svg", parser.tags)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertEqual(parser.links, ["https://example.test/post/1"])
        for unsafe in ("javascript:alert(1)", "data:text/html,hello",
                       "https://user:secret@example.test/post", "//example.test/post"):
            self.assertEqual(remote_link(unsafe), "")

    def test_cli_creates_private_report_without_overwriting_or_sending(self):
        self.make_campaign()
        profile = self.root / "profile.json"
        write_json(profile, self.profile)
        out = self.root / "report.html"
        cli = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"
        command = [sys.executable, str(cli), "--profile", str(profile),
                   "--state-dir", str(self.store.directory), "batch-report", self.cid,
                   "--output", str(out)]
        before = self.store.status()
        first = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        result = json.loads(first.stdout)
        self.assertTrue(result["ok"])
        self.assertFalse(result["result"]["remote_checked"])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        content = out.read_bytes()
        self.assertIn("结果待确认", content.decode())
        second = subprocess.run(command, text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertFalse(json.loads(second.stdout)["ok"])
        self.assertEqual(out.read_bytes(), content)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.store.status(), before)

    def test_write_report_also_rejects_existing_file(self):
        self.make_campaign()
        out = self.root / "already.html"
        out.write_text("保留已有文件", encoding="utf-8")
        with self.assertRaises(PublishError):
            write_report(self.cid, self.store, out)
        self.assertEqual(out.read_text(encoding="utf-8"), "保留已有文件")

    def test_multiple_campaigns_are_combined_without_repeating_an_id(self):
        first = self.cid
        self.make_campaign()
        rendered = render_report([first, self.cid], self.store)
        self.assertIn('共 5 个目标', rendered)
        self.assertIn('已发布 1', rendered)
        self.assertEqual(rendered.count('待执行 1'), 2)
        with self.assertRaises(PublishError):
            render_report([first, first], self.store)


if __name__ == "__main__":
    unittest.main()
