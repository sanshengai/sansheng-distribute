"""Bounded multi-target dispatch using the existing ledger and account locks.

Campaign definitions are immutable local snapshots, not a second job ledger.
Only publish_core.run may perform remote work. Recovery never submits prepared jobs.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from publish_core import HEX, PublishError, Store, digest, preflight, read_json, run, target_of, validate, write_json

FINISHED = {"draft_ready", "prefill_ready", "published", "broadcasted"}


def _select(available, requested):
    selected = list(available) if requested is None else requested
    if (not isinstance(selected, (list, tuple)) or not selected
            or not all(isinstance(x, str) for x in selected)
            or len(set(selected)) != len(selected)):
        raise PublishError("批量目标必须是非空、无重复的目标 ID 列表")
    if not set(selected).issubset(available):
        raise PublishError("选择超出本次批量任务的目标范围")
    # Manifest order is deterministic, regardless of CLI flag order.
    return [x for x in available if x in selected]


def _path(store, campaign_id):
    if not isinstance(campaign_id, str) or not HEX.fullmatch(campaign_id):
        raise PublishError("非法批量任务 ID")
    return store.directory / "campaigns" / f"{campaign_id}.json"


def preflight_campaign(manifest_path, profile, target_ids=None):
    """Collect local blockers before any job, grant, browser or upload is created.

    Reuse the single-target execution preflight, including its capability and
    field checks. A pass is neither authorization nor a remote login check.
    Invalid shared input is rejected before individual platform checks.
    """
    manifest = read_json(manifest_path)
    common = validate(manifest)
    selected = _select([t["id"] for t in manifest["targets"]], target_ids)
    rows = []
    for ident in selected:
        target = target_of(manifest, ident)
        row = {"target_id": ident, "platform": target["platform"],
               "account": target["account"], "endpoint": target["endpoint"]}
        try:
            preflight(manifest, ident, profile)
            row.update(local_ready=True)
        except Exception as exc:
            # Do not dump arbitrary dependency errors, which may contain secrets.
            message = str(exc) if isinstance(exc, PublishError) else f"本地检查失败：{type(exc).__name__}"
            row.update(local_ready=False, error=message)
            if isinstance(exc, PublishError):
                from preflight_advice import explain_known_blocker
                issue = explain_known_blocker(message, target, manifest, profile)
                if issue:
                    row['issue'] = issue
        rows.append(row)
    return {"manifest_digest": common["manifest_digest"],
            "selected_targets": selected, "targets": rows,
            "ready_count": sum(r["local_ready"] for r in rows),
            "blocked_count": sum(not r["local_ready"] for r in rows),
            "all_local_checks_passed": all(r["local_ready"] for r in rows),
            "local_only": True, "authorization_checked": False,
            "remote_checked": False}


def _load(store, campaign_id):
    definition = read_json(_path(store, campaign_id))
    if definition.get("schema_version") != 1 or not isinstance(definition.get("manifest"), dict):
        raise PublishError("批量任务快照格式不正确")
    manifest = definition["manifest"]
    targets = manifest.get("targets", [])
    if not isinstance(targets, list) or not all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in targets):
        raise PublishError("批量任务目标快照损坏")
    selected = _select([x["id"] for x in targets], definition.get("target_ids", []))
    if digest({"manifest": manifest, "target_ids": selected}) != campaign_id:
        raise PublishError("批量任务快照与 ID 不一致")
    return definition


def _job(store, manifest, ident):
    target = target_of(manifest, ident)
    job = store.status(store.key(manifest, target))
    if any(job[k] != target[k if k != "target_id" else "id"]
           for k in ("target_id", "platform", "account", "endpoint")):
        raise PublishError("批量任务与目标账本身份不一致")
    # A completed local target remains completed even if another destination was
    # appended later. Nonterminal tasks must recover the exact sealed version.
    if job["state"] not in FINISHED and job["manifest_digest"] != digest(manifest):
        raise PublishError("目标已绑定另一个版本；沿其原批量任务恢复")
    return job


def _actions(job, observation):
    state = job["state"]
    if state == "prepared":
        return "run", None
    if state == "submission_unknown":
        return "recover", "登录或平台验证确需本人时介入；查询前不要重复提交"
    if state == "pending":
        stage = observation.get("stage")
        if stage == 'cover_editing_required':
            return 'inspect', observation.get('author_action') or '核对原编辑器与封面方向；需换素材时重新封存，不重传已完成目标'
        if stage in {"native_media_insertion_required", "visual_review_required", "external_link_repair_required"}:
            return "advance", "需要 Agent 补齐同一草稿并读回；本人登录或视觉确认时再介入"
        return "resume", observation.get("author_action")
    if state == "failed":
        return "inspect", "检查失败原因后再决定后续操作；不自动重新发送"
    if state == "prefill_ready":
        return "author_submit", "预填已完成，由作者确认后点击发布"
    if state == "draft_ready" and job["platform"] == "xiaohongshu":
        return "author_submit", "小红书草稿已完成，最后发布由作者点击"
    return "done", None


def campaign_status(campaign_id, store):
    """Return durable per-target truth, including saved errors and next actions."""
    definition = _load(store, campaign_id)
    report = {"campaign_id": campaign_id, "targets": [], "counts": {}}
    for ident in definition["target_ids"]:
        target = target_of(definition["manifest"], ident)
        row = {"target_id": ident, "platform": target["platform"], "account": target["account"], "endpoint": target["endpoint"]}
        try:
            job = _job(store, definition["manifest"], ident)
            row.update(job)
            event = store.db.execute("SELECT data FROM events WHERE job_key=? ORDER BY id DESC LIMIT 1", (job["key"],)).fetchone()
            observation = json.loads(event[0]) if event else {}
            row["next_action"], row["author_action"] = _actions(job, observation)
            row["authorized"] = bool(store.db.execute(
                "SELECT 1 FROM grants WHERE key=?", (digest([job["key"], job["manifest_digest"]]),)).fetchone())
            if job["state"] == "prepared" and not row["authorized"]:
                row["next_action"] = "authorize"
                row["author_action"] = "尚无绑定此版本与目标的授权；已有用户授权则记录原话，否则请作者确认具体发布包"
            for field in ("stage", "reason", "message"):
                if field in observation:
                    row[field] = observation[field]
            error_path = store.directory / "campaigns" / campaign_id / f"{ident}.json"
            if error_path.exists():
                error = read_json(error_path)
                if error.get("job_updated_at") == job["updated_at"]:
                    row["error"] = error["error"]
        except (PublishError, OSError) as exc:
            row.update(state="blocked", error=str(exc), next_action="inspect", author_action=None)
        report["targets"].append(row)
        report["counts"][row["state"]] = report["counts"].get(row["state"], 0) + 1
    report["all_endpoints_reached"] = all(x["state"] in FINISHED and "error" not in x for x in report["targets"])
    return report


def prepare_campaign(manifest_path, store, target_ids=None):
    """Validate and persist a campaign; this grants no publishing permission."""
    manifest = read_json(manifest_path)
    validate(manifest)
    selected = _select([t["id"] for t in manifest["targets"]], target_ids)
    campaign_id = digest({"manifest": manifest, "target_ids": selected})
    definition = {"schema_version": 1, "manifest": manifest, "target_ids": selected}
    path = _path(store, campaign_id)
    if path.exists():
        _load(store, campaign_id)
    else:
        write_json(path, definition)
    for ident in selected:
        store.prepare(manifest, ident)
    return campaign_status(campaign_id, store)


def authorize_campaign(campaign_id, store, statement, target_ids=None):
    """Record the actual user statement only for explicitly selected targets.

    Caller must supply the user's actual approval and approved target scope.
    This API cannot infer semantic intent from a natural-language statement.
    """
    if not isinstance(statement, str) or not statement.strip():
        raise PublishError("需要实际用户授权原话，不得自动生成授权")
    definition = _load(store, campaign_id)
    selected = _select(definition["target_ids"], target_ids)
    # Validate all requested identities before granting any target.
    for ident in selected:
        _job(store, definition["manifest"], ident)
    grants = [store.authorize(definition["manifest"], ident, statement) for ident in selected]
    return {"campaign_id": campaign_id, "authorized_targets": selected, "grants": grants}


def run_campaign(campaign_id, profile, store, target_ids=None, *, resume=False, max_parallel=1):
    """Bound concurrency by platform; recover begun jobs without resending.

    resume=True is strictly read-only at the remote side: prepared jobs are left
    prepared. An absence_verified readback may reset a job to prepared, but it
    cannot cause a second execution in this invocation.
    """
    if type(max_parallel) is not int or max_parallel not in (1, 2):
        raise PublishError("并行数只支持 1 或 2")
    definition = _load(store, campaign_id)
    manifest = definition["manifest"]
    selected = _select(definition["target_ids"], target_ids)
    if max_parallel == 2:
        # Each worker owns its SQLite connection. A platform stays serial even
        # across different accounts, so a shared creator tab cannot be raced.
        groups = {}
        for ident in selected:
            groups.setdefault(target_of(manifest, ident)["platform"], []).append(ident)

        def worker(ids):
            local = Store(store.directory)
            try:
                return run_campaign(campaign_id, profile, local, ids, resume=resume)["outcomes"]
            finally:
                local.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, groups.values()))
        outcomes = {key: value for result in results for key, value in result.items()}
        report = campaign_status(campaign_id, store)
        report.update(selected_targets=selected, outcomes={i: outcomes[i] for i in selected},
                      readonly=resume, max_parallel=max_parallel)
        return report
    outcomes = {}
    for ident in selected:
        error_path = store.directory / "campaigns" / campaign_id / f"{ident}.json"
        try:
            job = _job(store, manifest, ident)
            if job["state"] in FINISHED and not (job["state"] == "prefill_ready" and store.needs_live_readback(job["key"])):
                outcomes[ident] = "already_complete"
                continue
            if resume and job["state"] == "prepared":
                outcomes[ident] = "not_started"
                continue
            snapshot = store.directory / "snapshots" / f"{job['key']}.json"
            # Core validates the snapshot digest during recovery. Validate it
            # here too, before non-resume runs can enter the submission window.
            if digest(read_json(snapshot)) != job["manifest_digest"]:
                raise PublishError("恢复快照与账本不一致")
            run(snapshot, ident, profile, store, resume=resume or job["state"] != "prepared")
            error_path.unlink(missing_ok=True)
            outcomes[ident] = "queried" if resume or job["state"] != "prepared" else "executed"
        except Exception as exc:
            # Keep unknown state and account locks intact. Never catch process
            # interrupts; KeyboardInterrupt/SystemExit remain visible.
            try:
                job = store.status(store.key(manifest, target_of(manifest, ident)))
                stamp = job["updated_at"]
            except PublishError:
                stamp = None
            error = str(exc) if isinstance(exc, PublishError) else f"本地操作失败：{type(exc).__name__}"
            write_json(error_path, {"error": error, "job_updated_at": stamp})
            outcomes[ident] = "error"
    report = campaign_status(campaign_id, store)
    report.update(selected_targets=selected, outcomes=outcomes, readonly=resume, max_parallel=max_parallel)
    return report
