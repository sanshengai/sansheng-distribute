"""Versioned handoff and durable dispatch. Only configured adapters perform remote I/O."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


class PublishError(ValueError):
    pass


PLATFORMS = {
    "wechat": {"article"},
    "x": {"text", "images", "video", "article"},
    "weibo": {"text", "images", "video"},
    "douyin": {"video"},
    "tencent_video": {"video"},
    "wechat_channels": {"video"},
    "bilibili": {"video"},
    "youtube": {"video"},
    "alipay": {"video"},
    "xiaohongshu": {"images", "video"},
}
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")
HEX = re.compile(r"^[a-f0-9]{64}$")
MEDIA_MARKER = re.compile(r"【发布媒体:([a-zA-Z0-9_-]+)】")


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublishError(f"无法读取 JSON：{path} ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise PublishError("JSON 根必须为对象")
    return value


def write_json(path, value):
    """Replace atomically, with restrictive permissions and no lingering temp files."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible_text(html):
    parser = VisibleText()
    parser.feed(html)
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def marker(slot_id):
    if not isinstance(slot_id, str) or not ID.fullmatch(slot_id):
        raise PublishError("非法 slot_id")
    return f"【发布媒体:{slot_id}】"


def resolve_file(root, rel):
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        raise PublishError("文件必须是 source_root 下的相对路径")
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise PublishError(f"文件缺失或越界：{rel}")
    return path


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise PublishError(f"缺少 {label}")
    return value


def _identifier(value, label):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise PublishError(f"非法 {label}")
    return value


def _indexed(rows, label):
    if not isinstance(rows, list):
        raise PublishError(f"{label} 必须为数组")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PublishError(f"{label} 条目必须为对象")
        ident = _identifier(row.get("id"), label + ".id")
        if ident in result:
            raise PublishError(f"重复 {label}.id: {ident}")
        result[ident] = row
    return result


def source_root(manifest):
    raw = _nonempty(manifest.get("source_root"), "source_root")
    if not Path(raw).is_absolute() or not Path(raw).is_dir():
        raise PublishError("source_root 必须为已有绝对目录")
    return Path(raw).resolve()


def validate(manifest, *, sealing=False):
    """Validate content identity; never infer author approval from a valid manifest."""
    if not manifest or manifest.get("schema_version") != 1:
        raise PublishError("需要非空 schema_version=1 的交接包")
    _identifier(manifest.get("job_id"), "job_id")
    _nonempty(manifest.get("producer"), "producer")
    if manifest.get("content_status") != "final":
        raise PublishError("交接内容尚未定稿")
    root = source_root(manifest)
    assets = _indexed(manifest.get("assets"), "assets")
    documents = _indexed(manifest.get("documents"), "documents")
    targets = _indexed(manifest.get("targets"), "targets")
    if not targets:
        raise PublishError("targets 不能为空")
    if not assets and not documents:
        # Text-only posts are represented by an immutable source text document.
        raise PublishError("需要至少一个可核验的源文件")
    for collection in (assets, documents):
        for row in collection.values():
            p = resolve_file(root, row.get("path"))
            if p.stat().st_size == 0:
                raise PublishError(f"空文件：{row['path']}")
            if sealing:
                row["sha256"] = file_hash(p)
            elif not HEX.fullmatch(str(row.get("sha256", ""))) or file_hash(p) != row["sha256"]:
                raise PublishError(f"源文件已改变：{row['path']}")
    for row in assets.values():
        if row.get("kind") not in {"audio", "video", "image", "subtitle"}:
            raise PublishError(f"未知素材类型：{row['id']}")
    for row in documents.values():
        if row.get("format") not in {"html", "markdown", "text"}:
            raise PublishError(f"未知文稿格式：{row['id']}")
        for field, collection in (("asset_ids", assets), ("source_document_ids", documents)):
            ids = row.get(field, [])
            if not isinstance(ids, list) or any(x not in collection for x in ids) or len(ids) != len(set(ids)):
                raise PublishError(f"文稿依赖不存在或重复：{field}")
        if row["id"] in row.get("source_document_ids", []):
            raise PublishError("文稿不可依赖自身")
    for row in targets.values():
        platform = row.get("platform")
        if platform not in PLATFORMS or row.get("content_type") not in PLATFORMS[platform]:
            raise PublishError(f"平台/内容类型不支持：{row['id']}")
        _identifier(row.get("account"), "account")
        if row.get("endpoint") not in {"draft", "prefill", "publish", "broadcast"}:
            raise PublishError(f"非法终点：{row['id']}")
        if row["endpoint"] == "broadcast" and platform != "wechat":
            raise PublishError("broadcast 只适用于公众号，不能与发表混用")
        if platform == "xiaohongshu" and row["endpoint"] not in {"prefill", "draft"}:
            raise PublishError("小红书最终点击须由作者完成")
        if platform in {"youtube", "alipay"}:
            metadata = row.get("platform_metadata", {})
            if not isinstance(metadata, dict):
                raise PublishError("platform_metadata 必须为对象")
            if platform == "youtube":
                if type(metadata.get("made_for_kids")) is not bool:
                    raise PublishError("YouTube 需要明确的 made_for_kids 布尔值；不能推断受众")
                if metadata.get("visibility") not in ("public", "unlisted", "private"):
                    raise PublishError("YouTube 需要明确 visibility：public、unlisted 或 private")
            for field in ("ai_generated", "altered_content"):
                if field in metadata and type(metadata[field]) is not bool:
                    raise PublishError(f"{field} 必须为布尔值；不能猜测声明")
            for field in ("collection_name", "playlist_name"):
                if field in metadata and (not isinstance(metadata[field], str) or not metadata[field].strip()):
                    raise PublishError(f"{field} 必须为非空名称")
        if row.get("document_id") and row["document_id"] not in documents:
            raise PublishError("目标引用了不存在的文稿")
        ids = row.get("asset_ids", [])
        if not isinstance(ids, list) or any(x not in assets for x in ids) or len(ids) != len(set(ids)):
            raise PublishError("目标素材列表错误")
        if row.get("cover_asset_id") and (row["cover_asset_id"] not in ids or assets[row["cover_asset_id"]]["kind"] != "image"):
            raise PublishError("封面必须登记在目标图片素材中")
        if row["content_type"] == "video" and sum(assets[x]["kind"] == "video" for x in ids) != 1:
            raise PublishError("视频目标必须关联唯一主视频")
        if row["content_type"] == "images" and not any(assets[x]["kind"] == "image" for x in ids):
            raise PublishError("图文目标没有图片")
        if row["content_type"] == "article" and not row.get("document_id"):
            raise PublishError("文章目标缺文稿")
        if 'existing_draft' in row:
            binding = row['existing_draft']
            if platform != 'wechat' or row['content_type'] != 'article' or row['endpoint'] != 'draft' or not isinstance(binding, dict):
                raise PublishError('认领只适用于公众号文章草稿')
            _nonempty(binding.get('remote_id'), '认领草稿 ID')
            expected_id = binding.get('expected_document_id')
            if expected_id not in documents or documents[expected_id]['format'] != 'text':
                raise PublishError('认领缺预期正文凭证')
            if expected_id not in documents[row['document_id']].get('source_document_ids', []):
                raise PublishError('认领预期正文必须绑定为文稿来源')
        _nonempty(row.get("title") or row.get("text"), "目标标题/文案")
    slots = _indexed(manifest.get("media_slots", []), "media_slots")
    for sid, slot in slots.items():
        if slot.get("document_id") not in documents or slot.get("asset_id") not in assets:
            raise PublishError(f"插槽引用不存在的文件：{sid}")
        doc = documents[slot["document_id"]]
        if doc["format"] != "html" or assets[slot["asset_id"]]["kind"] not in {"audio", "video"}:
            raise PublishError("正文媒体插槽必须为 HTML 中的音视频")
        text = visible_text(resolve_file(root, doc["path"]).read_text(encoding="utf-8"))
        m = marker(sid)
        if text.count(m) != 1:
            raise PublishError(f"插槽必须唯一匹配：{sid}")
        index = text.index(m)
        context = {"before": text[max(0, index - 48):index], "after": text[index + len(m):index + len(m) + 48]}
        if sealing:
            slot["context"] = context
        elif slot.get("context") != context:
            raise PublishError(f"插槽上下文已改变：{sid}")
        if not any(t.get("document_id") == slot["document_id"] for t in targets.values()):
            raise PublishError(f"插槽文稿没有发布目标：{sid}")
    for did, doc in documents.items():
        if doc["format"] == "html":
            text = visible_text(resolve_file(root, doc["path"]).read_text(encoding="utf-8"))
            found = MEDIA_MARKER.findall(text)
            expected = [sid for sid, slot in slots.items() if slot["document_id"] == did]
            if sorted(found) != sorted(expected):
                raise PublishError(f"存在未登记或重复的媒体标记：{did}")
    used_assets = {a for t in targets.values() for a in t.get('asset_ids', [])}
    reachable = {t['document_id'] for t in targets.values() if t.get('document_id')}
    pending = list(reachable)
    while pending:
        for dep in documents[pending.pop()].get('source_document_ids', []):
            if dep not in reachable:
                reachable.add(dep)
                pending.append(dep)
    used_assets.update(a for did in reachable for a in documents[did].get('asset_ids', []))
    used_assets.update(s['asset_id'] for s in slots.values() if s['document_id'] in reachable)
    if any(a['kind'] in {'audio', 'video'} and aid not in used_assets for aid, a in assets.items()):
        raise PublishError('音视频未关联目标或媒体插槽，不能静默遗漏')
    for target in targets.values():
        if target['content_type'] != 'article':
            continue
        doc = documents[target['document_id']]
        required = set(target.get('asset_ids', [])) | set(doc.get('asset_ids', []))
        required = {aid for aid in required if assets[aid]['kind'] in {'audio', 'video'}}
        placed = {s['asset_id'] for s in slots.values() if s['document_id'] == doc['id']}
        if required - placed:
            raise PublishError('文章音视频缺少明确插入位置：' + ', '.join(sorted(required - placed)))
    return {"manifest_digest": digest(manifest), "targets": len(targets), "assets": len(assets), "slots": len(slots)}


def seal(spec_path, output):
    value = read_json(spec_path)
    validate(value, sealing=True)
    validate(value)
    write_json(output, value)
    return value


def target_of(manifest, ident):
    for row in manifest["targets"]:
        if row["id"] == ident:
            return row
    raise PublishError(f"未知目标：{ident}")


class Store:
    """One local ledger; account locks survive crashes until an explicit readback."""
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "publish.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, manifest_digest TEXT NOT NULL, target_id TEXT NOT NULL,
            platform TEXT NOT NULL, account TEXT NOT NULL, endpoint TEXT NOT NULL,
            state TEXT NOT NULL, remote_id TEXT, remote_url TEXT, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS grants (
            key TEXT PRIMARY KEY, statement TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS account_locks (
            platform TEXT NOT NULL, account TEXT NOT NULL, job_key TEXT NOT NULL,
            PRIMARY KEY(platform, account));
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, job_key TEXT NOT NULL, state TEXT NOT NULL,
            data TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS timings (
            id INTEGER PRIMARY KEY, job_key TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('preflight','execute','continue','verify')),
            outcome TEXT NOT NULL CHECK(outcome IN ('completed','failed')),
            elapsed_ns INTEGER NOT NULL CHECK(elapsed_ns >= 0), started_at TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def key(self, manifest, target):
        # Execution identity is local to the target. Adding another destination
        # or editing producer metadata must not resend an already accepted post.
        by_id = {d["id"]: d for d in manifest.get("documents", [])}
        doc_ids = {target["document_id"]} if target.get("document_id") else set()
        pending = list(doc_ids)
        while pending:
            for dep in by_id[pending.pop()].get("source_document_ids", []):
                if dep not in doc_ids:
                    doc_ids.add(dep); pending.append(dep)
        docs = [by_id[d] for d in doc_ids]
        slots = [s for s in manifest.get("media_slots", []) if s["document_id"] == target.get("document_id")]
        ids = set(target.get("asset_ids", [])) | {s["asset_id"] for s in slots}
        for doc in docs:
            ids.update(doc.get("asset_ids", []))
        assets = [a for a in manifest.get("assets", []) if a["id"] in ids]
        return digest({"target": target, "documents": sorted(docs, key=lambda x: x["id"]),
                       "slots": sorted(slots, key=lambda x: x["id"]), "assets": sorted(assets, key=lambda x: x["id"])})

    def prepare(self, manifest, ident):
        report = validate(manifest)
        target = target_of(manifest, ident)
        key = self.key(manifest, target)
        with self.tx():
            self.db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,NULL,NULL,?)",
                            (key, report["manifest_digest"], ident, target["platform"], target["account"], target["endpoint"], "prepared", now()))
            job = self.status(key)
            if job["state"] == "prepared":
                # A sealed snapshot is for recovery, even after source cleanup.
                snapshot = self.directory / "snapshots" / f"{key}.json"
                write_json(snapshot, manifest)
                contents = {d["id"]: resolve_file(Path(manifest["source_root"]), d["path"]).read_text(encoding="utf-8")
                            for d in manifest.get("documents", [])}
                write_json(snapshot.with_suffix(".documents.json"), contents)
                self.db.execute("UPDATE jobs SET manifest_digest=? WHERE key=?", (report["manifest_digest"], key))
        return self.status(key)

    def status(self, key=None):
        if key:
            row = self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
            if not row:
                raise PublishError("任务尚未 prepare")
            return dict(row)
        return [dict(x) for x in self.db.execute("SELECT * FROM jobs ORDER BY updated_at DESC")]

    @contextmanager
    def measure(self, key, phase):
        """Private best-effort timing; never supersedes a durable remote ID/error."""
        if phase not in {'preflight','execute','continue','verify'}:
            raise PublishError('未知计量阶段')
        self.status(key)
        started_at, started = now(), time.monotonic_ns()
        outcome = 'failed'
        try:
            yield
            outcome = 'completed'
        finally:
            elapsed = max(0, time.monotonic_ns() - started)
            try:
                with self.tx():
                    self.db.execute('INSERT INTO timings(job_key,phase,outcome,elapsed_ns,started_at) VALUES (?,?,?,?,?)',
                                    (key, phase, outcome, elapsed, started_at))
            except sqlite3.Error:
                # Telemetry is not a dispatch gate. A missing sample is unknown,
                # never zero time; accepted IDs must already have been saved.
                pass

    def metrics(self, key):
        self.status(key)
        rows = [dict(r) for r in self.db.execute(
            'SELECT phase,outcome,elapsed_ns,started_at FROM timings WHERE job_key=? ORDER BY id', (key,))]
        phases = {}
        for row in rows:
            value = phases.setdefault(row['phase'], dict(samples=0, completed=0, failed=0, elapsed_ns=0))
            value['samples'] += 1; value[row['outcome']] += 1; value['elapsed_ns'] += row['elapsed_ns']
        return dict(key=key, sample_count=len(rows), phases=phases, samples=rows,
                    scope='core_preflight_and_adapter_action_with_observation_persistence',
                    unmeasured=['agent_operations','screenshots','human_intervention',
                                'upload_vs_transcode_breakdown','historical_runs_before_instrumentation'])

    def authorize(self, manifest, ident, statement):
        _nonempty(statement, "实际用户授权原话")
        job = self.prepare(manifest, ident)
        with self.tx():
            self.db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,?)", (digest([job["key"], digest(manifest)]), statement, now()))
        return {"key": job["key"], "authorized_endpoint": job["endpoint"]}

    def begin(self, manifest, ident, *, continuing=False):
        validate(manifest)
        target = target_of(manifest, ident)
        if target.get('existing_draft') and not continuing:
            raise PublishError('已绑定旧草稿，必须 adopt 只读认领，不能新建')
        key = self.key(manifest, target)
        with self.tx():
            job = self.status(key)
            if job["manifest_digest"] != digest(manifest):
                raise PublishError("任务快照已改变，请 prepare 并核对当前授权")
            if not self.db.execute("SELECT 1 FROM grants WHERE key=?", (digest([key, digest(manifest)]),)).fetchone():
                raise PublishError("缺少绑定当前版本与目标的用户授权记录")
            if continuing and (job['state']!='pending' or not job.get('remote_id')):
                raise PublishError('只允许对已读回且有远端身份的 pending 任务继续')
            if not continuing and job["state"] != "prepared":
                raise PublishError("任务已有执行记录，请先查询远端，不重复提交")
            try:
                self.db.execute("INSERT INTO account_locks VALUES (?,?,?)", (job["platform"], job["account"], key))
            except sqlite3.IntegrityError as exc:
                raise PublishError("该账号已有执行/结果未知任务，须先读回核实") from exc
            self.db.execute("UPDATE jobs SET state='submission_unknown',updated_at=? WHERE key=?", (now(), key))
            self.db.execute("INSERT INTO events(job_key,state,data,created_at) VALUES (?,?,?,?)",
                            (key, "submission_unknown", "{}", now()))
        return self.status(key)

    def bind_existing(self, manifest, ident):
        """Reserve a remote identity before readback; this does not authorize a write."""
        validate(manifest)
        target = target_of(manifest, ident)
        binding = target.get('existing_draft')
        if not binding:
            raise PublishError('缺显式旧草稿绑定')
        key = self.key(manifest, target)
        with self.tx():
            job = self.status(key)
            if job['manifest_digest'] != digest(manifest):
                raise PublishError('认领版本与已封存快照不符')
            remote = binding['remote_id']
            if self.db.execute('SELECT 1 FROM jobs WHERE platform=? AND account=? AND remote_id=? AND key<>?',
                               (target['platform'], target['account'], remote, key)).fetchone():
                raise PublishError('该草稿已由另一任务接管；沿原任务恢复')
            if job['state'] != 'prepared':
                if job.get('remote_id') != remote:
                    raise PublishError('认领与已有远端身份不符')
                return job
            try:
                self.db.execute('INSERT INTO account_locks VALUES (?,?,?)', (job['platform'], job['account'], key))
            except sqlite3.IntegrityError as exc:
                raise PublishError('该账号尚有结果未知任务；先读回核实') from exc
            self.db.execute("UPDATE jobs SET state='submission_unknown',remote_id=?,updated_at=? WHERE key=?", (remote, now(), key))
            self.db.execute('INSERT INTO events(job_key,state,data,created_at) VALUES (?,?,?,?)',
                            (key, 'adoption_started', canonical(binding), now()))
        return self.status(key)

    def save_observation(self, key, observation, *, verified):
        """Only a verifier readback may turn an attempt into success."""
        allowed = {"draft_ready", "prefill_ready", "pending", "published", "broadcasted", "not_submitted", "failed"}
        if observation.get("state") not in allowed:
            raise PublishError("适配器返回非法状态")
        with self.tx():
            job = self.status(key)
            if job["state"] == "prepared":
                raise PublishError("未启动的任务不可记为远端成功")
            for field in ("manifest_digest", "target_id", "platform", "account"):
                if observation.get(field) != job[field]:
                    raise PublishError(f"读回身份不匹配：{field}")
            state = observation["state"]
            if str(observation.get('remote_id', '')).startswith('browser-editor:') and observation.get('tracking_scope') != 'chrome_session_only':
                raise PublishError('浏览器编辑身份必须明确标为临时会话')
            if observation.get('tracking_scope') == 'chrome_session_only' and (
                    job['endpoint'] != 'prefill' or state not in {'pending', 'prefill_ready'}):
                raise PublishError('浏览器临时编辑会话不能充当平台草稿或已发布作品')
            expected = {"draft": "draft_ready", "prefill": "prefill_ready", "publish": "published", "broadcast": "broadcasted"}[job["endpoint"]]
            if state in {"draft_ready", "prefill_ready", "published", "broadcasted"}:
                if state != expected or not verified or observation.get("content_verified") is not True:
                    raise PublishError("终点不匹配或未完成内容读回")
            if state != "not_submitted" and not observation.get("remote_id"):
                raise PublishError("缺少可追踪的远端 ID")
            if job.get("remote_id") and job["remote_id"] != observation.get("remote_id"):
                raise PublishError("远端 ID 在读回时改变")
            if state == "not_submitted" and (not verified or observation.get("absence_verified") is not True):
                raise PublishError("无法证明尚未提交，禁止重试")
            if state in {"published", "broadcasted"} and not str(observation.get("remote_url", "")).startswith("https://"):
                raise PublishError("正式发布缺永久链接")
            if not verified and state != "pending":
                raise PublishError("执行响应只能记录 pending；成功须独立读回")
            stored_state = "prepared" if state == "not_submitted" else state
            self.db.execute("UPDATE jobs SET state=?,remote_id=?,remote_url=?,updated_at=? WHERE key=?",
                            (stored_state, observation.get("remote_id"), observation.get("remote_url"), now(), key))
            self.db.execute("INSERT INTO events(job_key,state,data,created_at) VALUES (?,?,?,?)",
                            (key, stored_state, canonical(observation), now()))
            if verified:
                self.db.execute("DELETE FROM account_locks WHERE job_key=?", (key,))
        return self.status(key)

    def needs_live_readback(self, key):
        event = self.db.execute('SELECT data FROM events WHERE job_key=? ORDER BY id DESC LIMIT 1', (key,)).fetchone()
        return bool(event and json.loads(event['data']).get('tracking_scope') == 'chrome_session_only')


def _first_publication_trial(profile, target, manifest):
    """One expiring social publication trial; ordinary authorization still applies."""
    if manifest is None or target['endpoint'] != 'publish' or target['platform'] not in {'x', 'weibo', 'douyin', 'bilibili', 'tencent_video', 'wechat_channels'}:
        return False
    expected = {'manifest_digest': digest(manifest), 'target_id': target['id'],
                'platform': target['platform'], 'account': target['account'],
                'content_type': target['content_type'], 'endpoint': 'publish'}
    entries = profile.get('first_publication_runs', [])
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != set(expected) | {'expires_at', 'acknowledge_publication'}:
            continue
        if entry.get('acknowledge_publication') is not True or any(entry.get(k) != v for k, v in expected.items()):
            continue
        try:
            expires = datetime.fromisoformat(entry['expires_at'])
            seconds = (expires - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            continue
        if 0 < seconds <= 24 * 60 * 60:
            return True
    return False


def adapter_config(profile, target, *, readonly=False, manifest=None):
    account = profile.get("accounts", {}).get(target["account"], {})
    if target["platform"] not in account.get("platforms", []):
        raise PublishError("账号配置没有声明目标平台")
    if account.get("restricted") and not readonly:
        raise PublishError("账号受限，仅允许本地准备")
    config = profile.get("adapters", {}).get(target["platform"], {})
    capability = f"{target['content_type']}:{target['endpoint']}"
    trials = profile.get('acceptance_runs', [])
    trial = (not readonly and manifest is not None and target['endpoint'] in {'draft', 'prefill'}
             and isinstance(trials, list) and any(isinstance(t, dict) and all(t.get(k) == v for k, v in {
                 'manifest_digest': digest(manifest), 'target_id': target['id'],
                 'platform': target['platform'], 'account': target['account'], 'endpoint': target['endpoint']
             }.items()) for t in trials))
    public_trial = not readonly and _first_publication_trial(profile, target, manifest)
    if capability not in config.get("enabled_capabilities", []) and not readonly and not trial and not public_trial:
        raise PublishError(f"该平台能力尚未启用/验收：{capability}")
    for name in (("verify",) if readonly else ("execute", "verify")):
        argv = config.get(name)
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise PublishError(f"适配器缺少 {name} 命令数组")
    return config


def preflight(manifest, ident, profile):
    """Local-only checks before claiming a remote submission window."""
    report = validate(manifest)
    target = target_of(manifest, ident)
    config = adapter_config(profile, target, manifest=manifest)
    if target['platform'] != 'wechat':
        from platform_pack import build_pack
        build_pack(manifest, ident, profile)
    for action in ('execute', 'verify'):
        argv = config[action]
        if not shutil.which(argv[0]):
            raise PublishError(f'适配器 {action} 的可执行程序不存在')
        if len(argv) > 1 and argv[1].endswith('.py') and not Path(argv[1]).is_file():
            raise PublishError(f'适配器 {action} 的 Python 脚本不存在')
        if any(Path(a).name == 'social_adapter.py' for a in argv):
            from social_adapter import preflight_social
            preflight_social(manifest, target, profile, argv)
        if any(Path(a).name == 'wechat_adapter.py' for a in argv):
            from wechat_adapter import preflight_wechat
            preflight_wechat(manifest, target, profile, argv)
        if any(Path(a).name == 'bilibili_adapter.py' for a in argv):
            from bilibili_adapter import preflight_bilibili
            preflight_bilibili(manifest, target, profile, argv)
        if any(Path(a).name == 'browser_engine.py' for a in argv):
            from browser_engine import preflight_browser
            preflight_browser(manifest, target, profile, argv)
    return dict(report, target_id=ident, endpoint=target['endpoint'], local_only=True)


def safe_adapter_failure(stderr):
    """Expose only recognized codes, never adapter prose, URLs or credentials."""
    if not isinstance(stderr, str) or len(stderr) > 16_384: return ''
    try: value = json.loads(stderr)
    except (ValueError, TypeError): return ''
    if not isinstance(value, dict) or value.get('error_type') != 'WechatError': return ''
    code = value.get('errcode')
    hints = {
        40007:'媒体或草稿 ID 无效；核对原账号和原 ID，不自动新建替代稿',
        40001:'访问令牌无效', 42001:'访问令牌过期',
        40013:'AppID 不正确', 40125:'AppSecret 不正确',
        40164:'调用出口不在公众号 IP 白名单', 40165:'调用出口不在公众号 IP 白名单',
        45009:'接口调用次数达到限制', 48001:'当前账号没有该接口权限',
    }
    if type(code) is not int or code not in hints: return ''
    return f'；微信 errcode={code}：{hints[code]}'


def invoke(config, action, request_path, *, lock_fd):
    # argv is trusted local configuration, never a command supplied by article text.
    argv = [*config[action], "--request", str(request_path)]
    try:
        # The executing child retains the same flock after a parent SIGKILL.
        # Adapters must keep foreground ownership and forward this fd if spawning.
        env = dict(os.environ, SANDY_PUBLISH_LOCK_FD=str(lock_fd))
        # A bounded executor owns its own process group. On timeout, stopping only
        # its Python parent would leave a CLI uploading with the inherited lock.
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              pass_fds=(lock_fd,), env=env, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=config.get('timeout_seconds', 300))
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.communicate(timeout=5)
                raise
            result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError(f"适配器 {action} 未完成（{type(exc).__name__}）；必须先查询，不重复发送") from exc
    if result.returncode != 0:
        # Do not echo arbitrary adapter stderr, which may contain tokens/URLs.
        detail = safe_adapter_failure(result.stderr)
        raise PublishError(f"适配器 {action} 失败（exit={result.returncode}）" +
                           (detail or '；适配器没有可公开的结构化错误码；原始响应不回显'))
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise PublishError("适配器必须只输出一个 JSON 对象") from exc
    if not isinstance(value, dict):
        raise PublishError("适配器响应不是对象")
    return value


def run(manifest_path, ident, profile, store, *, resume=False, advance=False, adopt=False, recover_remote_id=None, recovery_platform="wechat"):
    # A live executor must not be reclassified by a concurrent "not found" query.
    # A foreground child inherits the lock even if its parent dies.
    manifest = read_json(manifest_path)
    if not resume:
        validate(manifest)
    target = target_of(manifest, ident)
    lock_dir = store.directory / "locks"
    lock_dir.mkdir(exist_ok=True)
    lock = lock_dir / (digest({"platform": target["platform"], "account": target["account"]}) + ".lock")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublishError("该账号执行器仍在运行，不能并发提交或读回重置") from exc
        # Close our reference without LOCK_UN: an orphan child may still own it.
        return _run_locked(manifest, ident, profile, store, resume=resume, advance=advance, adopt=adopt,
                           recover_remote_id=recover_remote_id, recovery_platform=recovery_platform, lock_fd=stream.fileno())


def _run_locked(manifest, ident, profile, store, *, resume=False, advance=False, adopt=False, recover_remote_id=None, recovery_platform="wechat", lock_fd):
    if not resume:
        validate(manifest)
    target = target_of(manifest, ident)
    key = store.key(manifest, target)
    job = store.status(key)
    if recover_remote_id is not None:
        _nonempty(recover_remote_id, '候选远端 ID')
        allowed_recovery = (recovery_platform=='wechat' and target['platform']=='wechat' and target['endpoint']=='draft') or (recovery_platform in {'x','weibo'} and target['platform']==recovery_platform and target['endpoint']=='publish')
        if (not resume or advance or adopt or not allowed_recovery
                or job['state'] != 'submission_unknown' or job.get('remote_id')):
            raise PublishError('恢复绑定只适用于平台与终点匹配、无远端 ID 的未知提交任务')
        if store.db.execute('SELECT 1 FROM jobs WHERE platform=? AND account=? AND remote_id=? AND key<>?',
                            (job['platform'], job['account'], recover_remote_id, key)).fetchone():
            raise PublishError('候选草稿或作品已归属其他发布任务')
    if not resume and job["state"] in {"draft_ready", "prefill_ready", "published", "broadcasted"}:
        if job['state'] == 'prefill_ready' and store.needs_live_readback(key):
            # A local editor target is not a saved platform draft. Recheck that
            # exact session on every reuse; never silently refill a lost editor.
            resume = True
        else:
            return job
    snapshot_path = store.directory / "snapshots" / f"{key}.json"
    if resume:
        manifest = read_json(snapshot_path)
        if digest(manifest) != job["manifest_digest"]:
            raise PublishError("恢复快照与账本不一致")
        target = target_of(manifest, ident)
    config = adapter_config(profile, target, readonly=resume or adopt, manifest=manifest)
    if adopt:
        job = store.bind_existing(manifest, ident)
        resume = True
    request_path = store.directory / "requests" / f"{key}.json"
    request = {"schema_version": 1, "manifest_path": str(snapshot_path), "manifest_digest": digest(manifest),
               "document_snapshot_path": str(snapshot_path.with_suffix(".documents.json")),
               "target": target, "job": job}
    if recover_remote_id is not None:
        request['recovery_remote_id'] = recover_remote_id
        request['recovery_platform'] = recovery_platform
    if advance:
        if target['platform']!='wechat' or target['endpoint']!='draft':
            raise PublishError('advance 当前只允许公众号已建草稿补原生媒体')
        command=config.get('continue')
        if not isinstance(command,list) or not command or not all(isinstance(x,str) for x in command):
            raise PublishError('未配置经验证的媒体接力器')
        write_json(request_path,request)
        with store.measure(key,'verify'):
            observation=invoke(config,'verify',request_path,lock_fd=lock_fd)
            store.save_observation(key,observation,verified=True)
        if observation.get('state')=='draft_ready': return store.status(key)
        if observation.get('state')!='pending' or observation.get('stage') not in {'native_media_insertion_required','visual_review_required','external_link_repair_required'}:
            raise PublishError('当前草稿不在可补媒体阶段；先核对差异')
        request['job']=store.status(key); request['continuation']=observation; write_json(request_path,request)
        store.begin(manifest,ident,continuing=True)
        with store.measure(key,'continue'):
            response=invoke(config,'continue',request_path,lock_fd=lock_fd)
            store.save_observation(key,dict(response,state='pending'),verified=False)
        request['job']=store.status(key); write_json(request_path,request)
        with store.measure(key,'verify'):
            observation=invoke(config,'verify',request_path,lock_fd=lock_fd)
            return store.save_observation(key,observation,verified=True)
    if not resume:
        if not advance:
            with store.measure(key,'preflight'):
                preflight(manifest, ident, profile)
        # Prepare all fallible local writes before claiming the remote mutation window.
        write_json(request_path, request)
        store.begin(manifest, ident)
        with store.measure(key,'execute'):
            response = invoke(config, "execute", request_path, lock_fd=lock_fd)
            if response.get("remote_id"):
                # Persist accepted ID before telemetry or verification.
                pending = dict(response, state="pending")
                store.save_observation(key, pending, verified=False)
    elif job["state"] == "prepared":
        raise PublishError("尚未执行，请使用 run")
    request["job"] = store.status(key)
    write_json(request_path, request)
    with store.measure(key,'verify'):
        observation = invoke(config, "verify", request_path, lock_fd=lock_fd)
        if recover_remote_id is not None and (observation.get('remote_id') != recover_remote_id
                or observation.get('recovery_verified') is not True):
            message = ('候选草稿未经官方正文与封面核验' if recovery_platform=='wechat'
                       else '候选作品未经平台身份、内容与原媒体来源核验')
            raise PublishError(message+'，保持未知提交状态')
        return store.save_observation(key, observation, verified=True)
