# -*- coding: utf-8 -*-
"""AI 钩子完整流程 Web API（前缀 /api/hooks）。

新蓝图只调用 hooktool.flow.service，不改动已有上传/切片路由契约。
长任务返回 202 + job_id；冲突 409；内容或不满足要求 422。
"""
from __future__ import annotations

import functools
import os
import sqlite3
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Blueprint, jsonify, request

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hookflow")

_LOCATORS = {
    "plan": ("plan_revisions", "plan_revision_id"),
    "timeline": ("timeline_revisions", "timeline_revision_id"),
    "task": ("generation_tasks", "task_id"),
    "asset": ("generated_assets", "asset_id"),
    "job": ("job_events", "job_id"),
    "review": ("plan_reviews", "review_id"),
}


def create_hooks_bp(deps):
    project_root = deps["project_root"]
    hook_root = os.path.join(project_root, "ai_hook_tool")
    if hook_root not in sys.path:
        sys.path.insert(0, hook_root)

    from hooktool import paths
    from hooktool.flow import errors, service
    from hooktool.flow.errors import FlowError
    from hooktool.flow.store import FlowStore

    projects_root = Path(paths.WORKDIR) / "projects"
    bp = Blueprint("hooks", __name__, url_prefix="/api/hooks")

    def _fail(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except FlowError as exc:
                return jsonify(exc.to_dict()), exc.http_status
            except (OSError, ValueError, sqlite3.Error) as exc:
                return jsonify({"status": "failed", "code": "API_INPUT_ERROR",
                                "message": str(exc), "stage": None,
                                "retryable": False, "blocking_issues": []}), 422
        return wrapper

    def _project_dir(project_id: str) -> str:
        try:
            if str(uuid.UUID(project_id)) != project_id:
                raise ValueError
        except ValueError:
            raise FlowError("PROJECT_NOT_FOUND", "项目 ID 非法", stage="project")
        root = (projects_root / project_id).resolve()
        if not root.is_dir() or not (root / "project.sqlite").is_file():
            raise FlowError("PROJECT_NOT_FOUND", project_id, stage="project")
        return str(root)

    def _locate(kind: str, target: str) -> str:
        table, column = _LOCATORS[kind]
        if not projects_root.is_dir():
            raise FlowError("PROJECT_NOT_FOUND", "尚无项目", stage=kind)
        for directory in sorted(projects_root.iterdir()):
            database = directory / "project.sqlite"
            if not database.is_file():
                continue
            try:
                conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
                try:
                    row = conn.execute(
                        f"SELECT 1 FROM {table} WHERE {column}=?", (target,)).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
            if row:
                return str(directory)
        raise FlowError(kind.upper() + "_NOT_FOUND", target, stage=kind)

    def _body() -> dict:
        return request.get_json(silent=True) or {}

    def _async(project_dir: str, stage: str, runner):
        """立即登记 job 并返回 202；显式 wait=1 时同步执行。"""
        if request.args.get("wait") == "1" or _body().get("wait") is True:
            return jsonify(runner(None))
        store = FlowStore(project_dir)
        job = store.start_job(stage)
        _EXECUTOR.submit(runner, job["job_id"])
        return jsonify({"status": "accepted", "job_id": job["job_id"],
                        "stage": stage, "poll": f"/api/hooks/jobs/{job['job_id']}"}), 202

    # ── 项目与首集 ─────────────────────────────────────
    @bp.post("/projects")
    @_fail
    def create_project():
        body = _body()
        if not body.get("source_dir"):
            raise FlowError("SOURCE_REQUIRED", "缺少 source_dir", stage="project")
        result = service.create_project(
            body["source_dir"],
            episode=body.get("episode"),
            confirm_first_episode=bool(body.get("confirm_first_episode")),
            transcript=body.get("transcript"),
            confirm_transcript=bool(body.get("confirm_transcript")),
            no_transcript=bool(body.get("no_transcript")),
            policy=body.get("policy"))
        return jsonify(result), 201

    @bp.get("/projects/<project_id>")
    @_fail
    def get_project(project_id):
        return jsonify(service.project_state(_project_dir(project_id)))

    @bp.post("/projects/<project_id>/episode-confirmation")
    @_fail
    def confirm_episode(project_id):
        body = _body()
        if "expected_revision" not in body:
            raise FlowError("EXPECTED_REVISION_REQUIRED", "缺少 expected_revision",
                            stage="project")
        return jsonify(service.confirm_episode(
            _project_dir(project_id),
            expected_revision=int(body["expected_revision"]),
            episode=body.get("episode"), transcript=body.get("transcript"),
            confirm_transcript=bool(body.get("confirm_transcript")),
            no_transcript=bool(body.get("no_transcript")),
            policy=body.get("policy")))

    @bp.post("/projects/<project_id>/analysis")
    @_fail
    def analysis(project_id):
        directory = _project_dir(project_id)
        allow_asr = bool(_body().get("allow_asr"))
        return _async(directory, "analyze",
                      lambda job_id: service.analyze_episode(
                          directory, allow_asr=allow_asr, job_id=job_id))

    @bp.get("/projects/<project_id>/evidence")
    @_fail
    def evidence(project_id):
        return jsonify(service.evidence(
            _project_dir(project_id),
            page=int(request.args.get("page", 1)),
            size=int(request.args.get("size", 50))))

    @bp.get("/projects/<project_id>/story")
    @_fail
    def story(project_id):
        return jsonify(service.story(_project_dir(project_id)))

    @bp.post("/projects/<project_id>/proposals")
    @_fail
    def proposals(project_id):
        directory = _project_dir(project_id)
        body = _body()
        limit = body.get("limit")
        return _async(directory, "propose",
                      lambda job_id: service.propose_hooks(
                          directory, evidence_revision_id=body.get("evidence_revision_id"),
                          limit=int(limit) if limit else None, job_id=job_id))

    @bp.get("/projects/<project_id>/proposals")
    @_fail
    def list_proposals(project_id):
        return jsonify(service.list_proposals(_project_dir(project_id)))

    @bp.get("/projects/<project_id>/jobs")
    @_fail
    def list_jobs(project_id):
        return jsonify(service.list_jobs(_project_dir(project_id)))

    # ── 方案版本 ────────────────────────────────────────
    @bp.post("/plans/<plan_id>/revisions")
    @_fail
    def revise_plan(plan_id):
        body = _body()
        if "expected_revision" not in body or "edits" not in body:
            raise FlowError("REVISION_BODY_REQUIRED", "缺少 expected_revision/edits",
                            stage="plan")
        directory = _locate("plan", plan_id)
        return jsonify(service.revise_plan(directory, plan_id, body["edits"],
                                           expected_revision=int(body["expected_revision"])))

    @bp.get("/plans/<plan_id>")
    @_fail
    def plan_detail(plan_id):
        return jsonify(service.plan_detail(_locate("plan", plan_id), plan_id))

    @bp.post("/plans/<plan_id>/preview")
    @_fail
    def plan_preview(plan_id):
        directory = _locate("plan", plan_id)
        return _async(directory, "preview",
                      lambda job_id: service.render_preview(directory, plan_id, job_id))

    @bp.post("/plans/<plan_id>/reviews")
    @_fail
    def plan_review(plan_id):
        body = _body()
        if body.get("decision") not in ("approved", "rejected"):
            raise FlowError("INVALID_DECISION", str(body.get("decision")), stage="review")
        if not body.get("reviewer"):
            raise FlowError("REVIEWER_REQUIRED", "缺少 reviewer", stage="review")
        return jsonify(service.review_plan(
            _locate("plan", plan_id), plan_id, body["decision"],
            reviewer=body["reviewer"], notes=body.get("notes", ""),
            approved_scope=body.get("approved_scope", "full"),
            facts_reviewed=bool(body.get("facts_reviewed")),
            reason_code=body.get("reason_code")))

    @bp.post("/plans/<plan_id>/generation-tasks")
    @_fail
    def generation_tasks(plan_id):
        return jsonify(service.create_generation_tasks(
            _locate("plan", plan_id), plan_id))

    # ── 制作 ──────────────────────────────────────────
    @bp.post("/generation-tasks/<task_id>/assets")
    @_fail
    def import_asset(task_id):
        body = _body()
        if not body.get("file_path"):
            raise FlowError("FILE_REQUIRED", "缺少 file_path", stage="generation")
        return jsonify(service.import_generated_asset(
            _locate("task", task_id), task_id, body["file_path"],
            body.get("metadata"))), 201

    @bp.post("/generated-assets/<asset_id>/reviews")
    @_fail
    def asset_review(asset_id):
        body = _body()
        if body.get("decision") not in ("approved", "rejected"):
            raise FlowError("INVALID_DECISION", str(body.get("decision")), stage="review")
        return jsonify(service.review_generated_asset(
            _locate("asset", asset_id), asset_id, body["decision"],
            body.get("usable_ranges") or [], body.get("checks") or {},
            reviewer=body.get("reviewer")))

    # ── 时间线与交付 ────────────────────────────────────
    @bp.post("/projects/<project_id>/timelines")
    @_fail
    def create_timeline(project_id):
        body = _body()
        if not body.get("plan_revision_id"):
            raise FlowError("PLAN_REQUIRED", "缺少 plan_revision_id", stage="timeline")
        return jsonify(service.assemble_timeline(
            _project_dir(project_id), body["plan_revision_id"],
            body.get("selections"))), 201

    @bp.post("/timelines/<timeline_id>/render")
    @_fail
    def render_timeline(timeline_id):
        directory = _locate("timeline", timeline_id)
        return _async(directory, "render",
                      lambda job_id: service.render_timeline(directory, timeline_id, job_id))

    @bp.post("/timelines/<timeline_id>/quality-checks")
    @_fail
    def quality_checks(timeline_id):
        directory = _locate("timeline", timeline_id)
        return _async(directory, "qc",
                      lambda job_id: service.run_quality_checks(directory, timeline_id,
                                                                job_id=job_id))

    @bp.post("/timelines/<timeline_id>/reviews")
    @_fail
    def final_review(timeline_id):
        body = _body()
        if body.get("decision") not in ("approved", "rejected"):
            raise FlowError("INVALID_DECISION", str(body.get("decision")), stage="review")
        if not body.get("reviewer"):
            raise FlowError("REVIEWER_REQUIRED", "缺少 reviewer", stage="review")
        return jsonify(service.review_final(
            _locate("timeline", timeline_id), timeline_id, body["decision"],
            reviewer=body["reviewer"], notes=body.get("notes", ""),
            qc_id=body.get("qc_id")))

    @bp.get("/timelines/<timeline_id>/permissions")
    @_fail
    def permissions(timeline_id):
        return jsonify(service.permissions(_locate("timeline", timeline_id), timeline_id))

    @bp.post("/timelines/<timeline_id>/deliveries")
    @_fail
    def deliveries(timeline_id):
        return jsonify(service.export_delivery(_locate("timeline", timeline_id),
                                               timeline_id)), 201

    # ── 作业 ──────────────────────────────────────────
    @bp.get("/jobs/<job_id>")
    @_fail
    def job(job_id):
        return jsonify(service.job_status(_locate("job", job_id), job_id))

    @bp.post("/jobs/<job_id>/cancel")
    @_fail
    def cancel_job(job_id):
        return jsonify(service.cancel_job(_locate("job", job_id), job_id))

    return bp
