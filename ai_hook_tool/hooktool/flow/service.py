"""服务门面：CLI 与 Web 共用同一套流程，不各自实现。

每个长任务登记 job，便于重启对账；不会在 import 时自动启动分析或生成。
"""
from __future__ import annotations

import copy
from pathlib import Path

from ..prepare import prepare_project
from ..material_strict import discover, select_sources
from ..source_identity import json_hash
from . import (approval, delivery, generation, planner, preview, quality,
               settings, story as story_mod, timeline as timeline_mod)
from .errors import FlowError, as_flow_error
from .store import FlowStore

DEFAULT_POLICY = {}


def _job(store: FlowStore, stage: str, func, job_id: str | None = None):
    job = {"job_id": job_id} if job_id else store.start_job(stage)
    try:
        result = func(job["job_id"])
    except Exception as exc:
        error = as_flow_error(exc, stage=stage)
        store.finish_job(job["job_id"], "failed", error.to_dict(), error.code)
        raise error
    summary = {key: value for key, value in result.items()
               if isinstance(value, (str, int, float, bool, type(None)))}
    store.finish_job(job["job_id"], "succeeded", summary)
    return {**result, "job": {"job_id": job["job_id"], "status": "succeeded",
                              "stage": stage}}


def _merged_policy(store: FlowStore, overrides: dict | None = None) -> dict:
    base = settings.policy()
    base.update(copy.deepcopy(store.policy()))
    if overrides:
        base.update(copy.deepcopy(overrides))
    return base


def _evidence_for_plan(store: FlowStore, plan: dict) -> dict:
    evidence_id = plan["payload"].get("evidence_revision_id")
    if not evidence_id:
        raise FlowError("EVIDENCE_NOT_FOUND", "方案未记录证据修订", stage="plan")
    return store.evidence_by_id(evidence_id)


def _story_for_plan(store: FlowStore, plan: dict) -> dict:
    evidence = _evidence_for_plan(store, plan)
    story = store.story_for_evidence(evidence["evidence_revision_id"])
    if story is None:
        story = story_mod.build_story(evidence["snapshot"])
        store.save_story(evidence["run_id"], evidence["evidence_revision_id"], story)
    return story


# ── 项目与首集 ────────────────────────────────────────────
def create_project(source_dir: str, *, episode=None, confirm_first_episode=False,
                   transcript=None, confirm_transcript=False, no_transcript=False,
                   policy: dict | None = None, projects_root=None) -> dict:
    discovery = discover(source_dir)
    selection = select_sources(discovery, episode=episode,
                              confirm_first_episode=confirm_first_episode,
                              transcript=transcript,
                              confirm_transcript=confirm_transcript,
                              no_transcript=no_transcript)
    store = FlowStore.create(source_dir, discovery, selection,
                             projects_root=projects_root)
    merged = settings.policy()
    merged.update(copy.deepcopy(policy or {}))
    store.set_policy(merged)
    store.set_stage("INGESTED" if not selection["issues"] else "NEEDS_INPUT")
    return {"project_dir": str(store.root), "project": store.project(),
            "selection": selection,
            "issues": selection["issues"],
            "next": "confirm_episode" if selection["issues"] else "analyze"}


def confirm_episode(project_dir: str, *, expected_revision: int, episode=None,
                    transcript=None, confirm_transcript=False,
                    no_transcript=False, policy: dict | None = None) -> dict:
    store = FlowStore(project_dir)
    project = store.project()
    discovery = discover(project["source_dir"])
    selection = select_sources(discovery, episode=episode,
                              confirm_first_episode=bool(episode),
                              transcript=transcript,
                              confirm_transcript=confirm_transcript,
                              no_transcript=no_transcript)
    updated = store.confirm(discovery, selection, expected_revision)
    if policy:
        store.set_policy({**(store.policy() or {}), **copy.deepcopy(policy)})
    # 首集或字幕变更后，旧证据与审批不可继续复用。
    approval.invalidate(store, "source", "首集/字幕输入版本已更新")
    store.set_stage("INGESTED" if not selection["issues"] else "NEEDS_INPUT")
    return {"project": store.project(), "selection": selection,
            "issues": selection["issues"], "project_revision": updated["revision"]}


def update_policy(project_dir: str, patch: dict) -> dict:
    store = FlowStore(project_dir)
    merged = {**(store.policy() or {}), **copy.deepcopy(patch or {})}
    store.set_policy(merged)
    return {"policy": merged}


def project_state(project_dir: str) -> dict:
    store = FlowStore(project_dir)
    project = store.project()
    result = {"project": project, "policy": store.policy(),
              "runs": store.runs(), "jobs": store.jobs()[:10],
              "proposals": [{"proposal_id": p["proposal_id"],
                             "status": p["payload"].get("status"),
                             "score": p["payload"].get("total_score")}
                            for p in _safe(store.proposals)],
              "plans": [{"plan_revision_id": p["plan_revision_id"],
                         "proposal_id": p["proposal_id"],
                         "revision_no": p["revision_no"],
                         "blocked": bool(p["payload"].get("blocked")),
                         "approval": (store.valid_plan_review(p) or {}).get("decision")}
                        for p in _safe(store.plans)],
              "timelines": _safe_timelines(store),
              "invalidations": store.invalidations()[:10]}
    return result


def _safe(func):
    try:
        return func()
    except FlowError:
        return []


def _safe_timelines(store: FlowStore) -> list[dict]:
    try:
        row = store.latest_timeline()
        return [{"timeline_revision_id": row["timeline_revision_id"],
                 "plan_revision_id": row["plan_revision_id"],
                 "revision_no": row["revision_no"],
                 "total_ms": row["payload"].get("total_ms")}]
    except FlowError:
        return []


# ── 分析 ──────────────────────────────────────────────────
def analyze_episode(project_dir: str, *, allow_asr: bool = False,
                    policy: dict | None = None, job_id: str | None = None) -> dict:
    store = FlowStore(project_dir)
    project = store.project()
    selection = project.get("selection") or {}
    if selection.get("issues") or not selection.get("episode"):
        raise FlowError("NEEDS_INPUT", "请先确认首集与字幕归属", stage="analyze")
    pol = _merged_policy(store, policy)
    store.set_policy(pol)

    def work(_job_id):
        prepared = prepare_project(store, allow_asr=allow_asr)
        if prepared["status"] != "succeeded":
            problem = prepared.get("error") or {}
            raise FlowError(problem.get("code", "PREPARE_FAILED"),
                            problem.get("message", "证据准备失败"), stage="analyze")
        evidence = store.register_evidence(prepared["run_id"])
        model = story_mod.build_story(evidence["snapshot"], config=settings.config())
        store.save_story(prepared["run_id"], evidence["evidence_revision_id"], model)
        if not model["events"]:
            store.set_stage("INSUFFICIENT_MATERIAL", reason="首集没有可定位事件")
            raise FlowError("INSUFFICIENT_MATERIAL", "首集没有可用事件", stage="analyze")
        store.set_stage("ANALYZED")
        return {"evidence_revision_id": evidence["evidence_revision_id"],
                "run_id": prepared["run_id"],
                "event_count": len(model["events"]),
                "character_count": len(model["characters"]),
                "coverage": model["coverage"],
                "issues": model["issues"],
                "evidence_path": prepared["evidence_path"]}

    return _job(store, "analyze", work, job_id)


def evidence(project_dir: str, *, page: int = 1, size: int = 50) -> dict:
    store = FlowStore(project_dir)
    row = store.latest_evidence()
    utterances = row["snapshot"]["evidence"]["utterances"]
    start = max(0, (page - 1) * size)
    return {"evidence_revision_id": row["evidence_revision_id"],
            "duration_ms": row["duration_ms"],
            "total": len(utterances), "page": page, "size": size,
            "items": utterances[start:start + size]}


def story(project_dir: str) -> dict:
    store = FlowStore(project_dir)
    latest = store.story()
    return {"story": latest,
            "events": latest["events"],
            "characters": latest["characters"],
            "pending": [e["event_id"] for e in latest["events"]
                        if e["verification_status"] != "verified"]}


# ── 方案 ──────────────────────────────────────────────────
def propose_hooks(project_dir: str, *, evidence_revision_id: str | None = None,
                  limit: int | None = None, job_id: str | None = None) -> dict:
    store = FlowStore(project_dir)
    evidence = (store.evidence_by_id(evidence_revision_id) if evidence_revision_id
                else store.latest_evidence())
    model = store.story_for_evidence(evidence["evidence_revision_id"])
    if model is None:
        model = story_mod.build_story(evidence["snapshot"], config=settings.config())
        store.save_story(evidence["run_id"], evidence["evidence_revision_id"], model)
    pol = _merged_policy(store)

    def work(_job_id):
        result = planner.build_proposals(model, evidence["evidence_revision_id"],
                                        policy=pol, config=settings.config(),
                                        limit=limit)
        if not result["proposals"]:
            store.set_stage("INSUFFICIENT_MATERIAL",
                            reason=result.get("reason") or "无有效方案")
            return result
        store.save_proposals(evidence["run_id"], evidence["evidence_revision_id"],
                             result["proposals"])
        plans = []
        for proposal in result["proposals"]:
            semantic = approval.semantic_hash(proposal)
            existing = store.find_plan(proposal["proposal_id"], semantic)
            if existing:
                plans.append({**existing, "reused": True})
                continue
            plan = approval.create_plan(store, proposal["proposal_id"], model,
                                        evidence["snapshot"], policy=pol)
            plans.append({**plan, "reused": False})
        store.set_stage("PROPOSED")
        return {**result, "plans": plans}

    return _job(store, "propose", work, job_id)


def add_import_root(project_dir: str, directory: str) -> dict:
    store = FlowStore(project_dir)
    return store.add_import_root(directory)


def list_proposals(project_dir: str) -> dict:
    store = FlowStore(project_dir)
    items = []
    for row in store.proposals():
        payload = row["payload"]
        try:
            plan = store.latest_plan(row["proposal_id"])
            plan_info = {"plan_revision_id": plan["plan_revision_id"],
                         "revision_no": plan["revision_no"],
                         "blocked": bool(plan["payload"].get("blocked")),
                         "approval": (store.valid_plan_review(plan) or {}).get("decision")}
        except FlowError:
            plan_info = None
        items.append({"proposal_id": row["proposal_id"], "payload": payload,
                      "latest_plan": plan_info})
    return {"proposals": items, "count": len(items),
            "effective_count": sum(1 for i in items
                                   if not i["payload"].get("insufficient_material"))}


# ── 版本编辑与审批 ───────────────────────────────────────
def revise_plan(project_dir: str, plan_revision_id: str, edits: list[dict], *,
                expected_revision: int) -> dict:
    store = FlowStore(project_dir)
    plan = store.plan(plan_revision_id)
    evidence = _evidence_for_plan(store, plan)
    model = _story_for_plan(store, plan)
    result = approval.revise_plan(store, plan_revision_id, edits,
                                  expected_revision=expected_revision,
                                  story=model, evidence_snapshot=evidence["snapshot"],
                                  policy=_merged_policy(store))
    return result


def review_plan(project_dir: str, plan_revision_id: str, decision: str, *,
                reviewer: str, notes: str = "", approved_scope: str = "full",
                facts_reviewed: bool = False, reason_code: str | None = None) -> dict:
    store = FlowStore(project_dir)
    plan = store.plan(plan_revision_id)
    evidence = _evidence_for_plan(store, plan)
    model = _story_for_plan(store, plan)
    review = approval.review_plan(store, plan_revision_id, decision,
                                  reviewer=reviewer, notes=notes,
                                  approved_scope=approved_scope,
                                  facts_reviewed=facts_reviewed,
                                  reason_code=reason_code, story=model,
                                  evidence_snapshot=evidence["snapshot"],
                                  policy=_merged_policy(store))
    return {"review": review, "plan": store.plan(plan_revision_id)}


def plan_detail(project_dir: str, plan_revision_id: str) -> dict:
    store = FlowStore(project_dir)
    plan = store.plan(plan_revision_id)
    return {"plan": plan, "reviews": store.plan_reviews(plan_revision_id)}


# ── 预演 ──────────────────────────────────────────────────
def render_preview(project_dir: str, plan_revision_id: str,
                   job_id: str | None = None) -> dict:
    store = FlowStore(project_dir)
    plan = store.plan(plan_revision_id)
    evidence = _evidence_for_plan(store, plan)

    def work(_job_id):
        result = preview.render_preview(store, plan_revision_id,
                                        evidence["snapshot"],
                                        policy=_merged_policy(store))
        return {"preview": result}

    return _job(store, "preview", work, job_id)


# ── 制作 ──────────────────────────────────────────────────
def create_generation_tasks(project_dir: str, plan_revision_id: str) -> dict:
    store = FlowStore(project_dir)
    plan = store.plan(plan_revision_id)
    evidence = _evidence_for_plan(store, plan)
    model = _story_for_plan(store, plan)
    return generation.create_tasks(store, plan_revision_id, model,
                                   evidence["snapshot"], policy=_merged_policy(store))


def import_generated_asset(project_dir: str, task_id: str, file_path: str,
                           metadata: dict | None = None) -> dict:
    store = FlowStore(project_dir)
    asset = generation.import_asset(store, task_id, file_path, metadata)
    return {"asset": asset}


def review_generated_asset(project_dir: str, asset_id: str, decision: str,
                           usable_ranges: list[dict], checks: dict,
                           reviewer: str | None = None) -> dict:
    store = FlowStore(project_dir)
    result = generation.review_asset(store, asset_id, decision, usable_ranges,
                                     checks, reviewer=reviewer)
    store.set_stage("ASSETS_REVIEWED")
    return {"review": result}


# ── 时间线与质检 ─────────────────────────────────────────
def assemble_timeline(project_dir: str, plan_revision_id: str,
                      selections: dict | None = None) -> dict:
    store = FlowStore(project_dir)
    plan = store.plan(plan_revision_id)
    evidence = _evidence_for_plan(store, plan)
    model = _story_for_plan(store, plan)
    result = timeline_mod.build_timeline(store, plan_revision_id, model,
                                         evidence["snapshot"], selections=selections,
                                         policy=_merged_policy(store))
    return result


def render_timeline(project_dir: str, timeline_revision_id: str,
                    job_id: str | None = None) -> dict:
    store = FlowStore(project_dir)

    def work(_job_id):
        info = timeline_mod.render(store, timeline_revision_id,
                                   policy=_merged_policy(store))
        return {"render": info}

    return _job(store, "render", work, job_id)


def run_quality_checks(project_dir: str, timeline_revision_id: str,
                       render_info: dict | None = None,
                       job_id: str | None = None) -> dict:
    store = FlowStore(project_dir)

    def work(_job_id):
        report = quality.run(store, timeline_revision_id,
                             policy=_merged_policy(store),
                             render_info=render_info)
        return {"qc": report}

    return _job(store, "qc", work, job_id)


def review_final(project_dir: str, timeline_revision_id: str, decision: str, *,
                 reviewer: str, notes: str = "", qc_id: str | None = None) -> dict:
    store = FlowStore(project_dir)
    latest = store.latest_qc(timeline_revision_id)
    if latest is None:
        raise FlowError("QC_MISSING", "尚未质检，不能最终验收", stage="review")
    if qc_id and latest["qc_id"] != qc_id:
        raise FlowError("QC_STALE", "验收必须绑定最新质检报告", stage="review")
    report = latest["report"]
    if decision == "approved" and not report.get("delivery_ready"):
        raise FlowError("QC_BLOCKED", "质检存在阻断项，不能批准交付", stage="review",
                        blocking_issues=report.get("blocking_issues") or [])
    final = store.add_final_review(timeline_revision_id, qc_id=latest["qc_id"],
                                   decision=decision, reviewer=reviewer, notes=notes)
    store.set_stage("FINAL_REVIEW", reason=notes)
    return {"final_review": final, "qc_id": latest["qc_id"],
            "permissions": quality.permissions(store, {**report,
                                                       "qc_id": latest["qc_id"]})}


# ── 交付 ──────────────────────────────────────────────────
def export_delivery(project_dir: str, timeline_revision_id: str) -> dict:
    store = FlowStore(project_dir)
    result = delivery.export(store, timeline_revision_id,
                             policy=_merged_policy(store))
    return {"delivery": result}


def permissions(project_dir: str, timeline_revision_id: str) -> dict:
    store = FlowStore(project_dir)
    latest = store.latest_qc(timeline_revision_id)
    if latest is None:
        raise FlowError("QC_MISSING", "尚未质检", stage="qc")
    qc = {**latest["report"], "qc_id": latest["qc_id"]}
    return quality.permissions(store, qc)


def job_status(project_dir: str, job_id: str) -> dict:
    store = FlowStore(project_dir)
    return store.job(job_id)


def list_jobs(project_dir: str) -> dict:
    store = FlowStore(project_dir)
    return {"jobs": store.jobs()}


def cancel_job(project_dir: str, job_id: str) -> dict:
    store = FlowStore(project_dir)
    job = store.job(job_id)
    if job["status"] != "running":
        raise FlowError("JOB_NOT_RUNNING", job_id, stage="jobs")
    store.finish_job(job_id, "cancelled", {"cancellation": "cooperative"})
    return {"job_id": job_id, "status": "cancelled", "cooperative": True,
            "note": "已请求取消；外部平台任务不受本地控制"}
