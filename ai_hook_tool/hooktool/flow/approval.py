"""方案版本、审批绑定与失效传播。

审批记录绑定 plan_revision_id + semantic_hash；任何影响剧情的编辑都会产生新版本，
旧审批只作为历史保留，不能再驱动制作或交付。
"""
from __future__ import annotations

import copy

from ..source_identity import json_hash
from . import settings
from .errors import FlowError
from .validator import validate_plan

SEMANTIC_FIELDS = ("mechanism", "perspective", "relationship_summary", "opening",
                   "setup", "escalation", "turn", "ending", "answers", "shots")
SHOT_SEMANTIC_FIELDS = ("shot_id", "role", "event_ids", "evidence_ids",
                        "source_start_ms", "source_end_ms", "planned_ms",
                        "dialogue", "character_ids")

REJECTION_CODES = ("OPENING_WEAK", "RELATION_UNCLEAR", "NO_ESCALATION",
                   "SPOILER_TOO_EARLY", "PACING_SLOW", "INSUFFICIENT_REACTION",
                   "FACT_ERROR", "OTHER")


def semantic_hash(payload: dict) -> str:
    """只哈希影响剧情与制作的内容，技术参数（机位、字幕样式）不在此列。"""
    semantic = {field: payload.get(field) for field in SEMANTIC_FIELDS}
    semantic["shots"] = [{field: shot.get(field) for field in SHOT_SEMANTIC_FIELDS}
                         for shot in (payload.get("shots") or [])]
    return json_hash(semantic)


def _normalise(payload: dict) -> dict:
    payload["planned_duration_ms"] = sum(int(s.get("planned_ms") or 0)
                                         for s in payload.get("shots") or [])
    return payload


def _shot_from_event(event: dict, role: str, *, duration_ms: int,
                     handles: int = 300) -> dict:
    start = max(0, event["source_spans"][0]["start_ms"] - handles)
    end = min(duration_ms, event["source_spans"][0]["end_ms"] + handles)
    return {
        "shot_id": f"shot_{role}_{json_hash(event['event_id'] + role)[:8]}",
        "role": role,
        "event_ids": [event["event_id"]],
        "evidence_ids": list(event["evidence_ids"]),
        "source_start_ms": start,
        "source_end_ms": end,
        "planned_ms": max(1, min(end - start, int(settings.policy()["max_shot_sec"] * 1000))),
        "dialogue": event["dialogue_text"][:160],
        "visual_hint": "沿用原片动作与反应；人物身份以已确认参考为准",
        "character_ids": list(event["character_ids"]),
        "verification_status": event["verification_status"],
    }


def _apply_edits(payload: dict, edits: list[dict], story: dict,
                 duration_ms: int) -> dict:
    events = {e["event_id"]: e for e in story["events"]}
    shots = [copy.deepcopy(s) for s in payload["shots"]]
    index = {s["shot_id"]: i for i, s in enumerate(shots)}

    for edit in edits:
        op = edit.get("op")
        if op == "set_title":
            payload["title"] = str(edit["value"])[:80]
        elif op == "set_mechanism":
            payload["mechanism"] = edit["mechanism"]
            payload["mechanism_name"] = edit.get("mechanism_name") or payload.get("mechanism_name")
        elif op == "set_answers":
            answers = dict(payload.get("answers") or {})
            answers.update({k: str(v) for k, v in (edit.get("answers") or {}).items()})
            payload["answers"] = answers
        elif op == "set_perspective":
            payload["perspective"] = {"char_id": edit["char_id"],
                                      "name": edit.get("name") or "主角（待确认）",
                                      "verification_status": "confirmed_by_editor"}
        elif op == "set_camera":
            shot = shots[index[edit["shot_id"]]]
            shot["camera"] = str(edit["value"])
        elif op == "delete_shot":
            if edit["shot_id"] not in index:
                raise FlowError("EDIT_TARGET_NOT_FOUND", edit["shot_id"], stage="plan")
            del shots[index[edit["shot_id"]]]
            index = {s["shot_id"]: i for i, s in enumerate(shots)}
        elif op == "replace_shot":
            if edit["shot_id"] not in index:
                raise FlowError("EDIT_TARGET_NOT_FOUND", edit["shot_id"], stage="plan")
            shot = shots[index[edit["shot_id"]]]
            for key, value in (edit.get("fields") or {}).items():
                if key not in SHOT_SEMANTIC_FIELDS + ("visual_hint", "camera"):
                    raise FlowError("EDIT_FIELD_NOT_ALLOWED", key, stage="plan")
                shot[key] = value
        elif op == "insert_shot":
            event_id = edit["event_id"]
            if event_id not in events:
                raise FlowError("EDIT_EVENT_NOT_FOUND", event_id, stage="plan")
            shot = _shot_from_event(events[event_id], edit.get("role") or "setup",
                                    duration_ms=duration_ms)
            if edit.get("after"):
                if edit["after"] not in index:
                    raise FlowError("EDIT_TARGET_NOT_FOUND", edit["after"], stage="plan")
                shots.insert(index[edit["after"]] + 1, shot)
            else:
                shots.append(shot)
            index = {s["shot_id"]: i for i, s in enumerate(shots)}
        elif op == "reorder":
            order = list(edit.get("order") or [])
            if set(order) != {s["shot_id"] for s in shots}:
                raise FlowError("EDIT_REORDER_INCOMPLETE", "顺序必须覆盖全部镜头", stage="plan")
            shots = [next(s for s in shots if s["shot_id"] == sid) for sid in order]
        elif op == "set_ending":
            if not shots:
                raise FlowError("EDIT_NO_SHOTS", "没有镜头可替换结尾", stage="plan")
            event_id = edit["event_id"]
            if event_id not in events:
                raise FlowError("EDIT_EVENT_NOT_FOUND", event_id, stage="plan")
            shots[-1] = _shot_from_event(events[event_id], "ending", duration_ms=duration_ms)
        elif op == "set_opening":
            event_id = edit["event_id"]
            if event_id not in events:
                raise FlowError("EDIT_EVENT_NOT_FOUND", event_id, stage="plan")
            shots.insert(0, _shot_from_event(events[event_id], "opening",
                                             duration_ms=duration_ms))
        else:
            raise FlowError("EDIT_OP_UNKNOWN", str(op), stage="plan")

    if not shots:
        raise FlowError("EDIT_EMPTY_PLAN", "编辑后没有镜头", stage="plan")
    payload["shots"] = shots
    if shots:
        payload["opening"] = shots[0]["event_ids"][0]
        payload["ending"] = shots[-1]["event_ids"][0]
    return _normalise(payload)


def create_plan(store, proposal_id: str, story: dict, evidence_snapshot: dict,
                *, policy: dict | None = None) -> dict:
    proposal = store.proposal(proposal_id)["payload"]
    pol = policy or settings.policy()
    validation = validate_plan(proposal, story, evidence_snapshot, policy=pol)
    payload = copy.deepcopy(proposal)
    payload.update(revision_no=1, parent_id=None, edits=[],
                   validation=validation, plan_status="proposed",
                   blocked=not validation["ok"])
    _normalise(payload)
    plan = store.create_plan(proposal_id, payload,
                             semantic_hash=semantic_hash(payload))
    store.set_stage("PROPOSED")
    return {**plan, "validation": validation, "blocked": not validation["ok"]}


def revise_plan(store, plan_revision_id: str, edits: list[dict], *,
                expected_revision: int, story: dict, evidence_snapshot: dict,
                policy: dict | None = None) -> dict:
    current = store.plan(plan_revision_id)
    if current["revision_no"] != expected_revision:
        raise FlowError("REVISION_CONFLICT",
                        f"期望版本 {expected_revision}，当前 {current['revision_no']}",
                        stage="plan")
    payload = copy.deepcopy(current["payload"])
    payload.pop("validation", None)
    payload = _apply_edits(payload, edits, story,
                          int(evidence_snapshot["duration_ms"]))
    validation = validate_plan(payload, story, evidence_snapshot,
                              policy=policy or settings.policy())
    payload.update(revision_no=current["revision_no"] + 1,
                   parent_id=plan_revision_id,
                   edits=list(payload.get("edits") or []) + edits,
                   validation=validation, plan_status="revised",
                   blocked=not validation["ok"])
    new_plan = store.create_plan(current["proposal_id"], payload,
                                 semantic_hash=semantic_hash(payload),
                                 parent_id=plan_revision_id)
    store.invalidate_plan_reviews(plan_revision_id, "superseded_by_edit")
    return {**new_plan, "validation": validation, "blocked": not validation["ok"]}


def review_plan(store, plan_revision_id: str, decision: str, *, reviewer: str,
                notes: str = "", approved_scope: str = "full",
                facts_reviewed: bool = False, reason_code: str | None = None,
                story: dict, evidence_snapshot: dict,
                policy: dict | None = None) -> dict:
    plan = store.plan(plan_revision_id)
    latest = store.latest_plan(plan["proposal_id"])
    if latest["plan_revision_id"] != plan_revision_id:
        raise FlowError("PLAN_NOT_CURRENT",
                        "只能审批最新版本；旧版本仅供追溯", stage="review")
    if decision == "approved":
        validation = validate_plan(plan["payload"], story, evidence_snapshot,
                                   policy=policy or settings.policy())
        if not validation["ok"]:
            raise FlowError("PLAN_INVALID", "方案存在阻断项，不能批准", stage="review",
                            blocking_issues=validation["blocking_issues"])
    else:
        if reason_code not in REJECTION_CODES:
            raise FlowError("INVALID_REJECTION_CODE", str(reason_code), stage="review")
    review = store.add_plan_review(
        plan_revision_id, semantic_hash=plan["semantic_hash"], decision=decision,
        reviewer=reviewer, notes=(f"[{reason_code}] {notes}" if reason_code else notes),
        approved_scope=approved_scope, facts_reviewed=facts_reviewed)
    store.set_stage("PLAN_APPROVED" if decision == "approved" else "PROPOSED",
                    reason=notes)
    return review


def assert_production_allowed(store, plan_revision_id: str, story: dict,
                              evidence_snapshot: dict, *,
                              policy: dict | None = None) -> dict:
    store.assert_current_source()
    plan = store.plan(plan_revision_id)
    latest = store.latest_plan(plan["proposal_id"])
    if latest["plan_revision_id"] != plan_revision_id:
        raise FlowError("PLAN_NOT_CURRENT", "方案不是最新版本", stage="plan")
    validation = validate_plan(plan["payload"], story, evidence_snapshot,
                               policy=policy or settings.policy())
    if not validation["ok"]:
        raise FlowError("PLAN_INVALID", "方案存在阻断项", stage="plan",
                        blocking_issues=validation["blocking_issues"])
    review = store.valid_plan_review(plan)
    if review is None:
        raise FlowError("PLAN_NOT_APPROVED", "方案未批准或审批已失效", stage="approval",
                        blocking_issues=["未找到与当前版本匹配的有效审批"])
    return {"plan": plan, "review": review, "validation": validation}


def assert_delivery_allowed(store, plan_revision_id: str, story: dict,
                           evidence_snapshot: dict, *,
                           policy: dict | None = None) -> dict:
    result = assert_production_allowed(store, plan_revision_id, story,
                                       evidence_snapshot, policy=policy)
    if not result["review"].get("facts_reviewed"):
        raise FlowError("FACTS_NOT_REVIEWED",
                        "关键剧情事实尚未人工核对", stage="approval",
                        blocking_issues=["审批时未勾选关键事实已核对"],
                        )
    return result


def invalidate(store, scope: str, reason: str, target: str | None = None) -> dict:
    """按依赖图作废；只标记失效，不删除历史版本。"""
    affected = {"plans": 0, "asset_reviews": 0, "final_reviews": 0, "stage": None}
    if scope == "source":
        for plan in store.plans():
            affected["plans"] += store.invalidate_plan_reviews(
                plan["plan_revision_id"], reason)
        affected["asset_reviews"] = store.invalidate_asset_reviews(reason)
        affected["final_reviews"] = store.invalidate_final_reviews(reason)
        store.set_stage("NEEDS_INPUT", reason=reason)
        affected["stage"] = "NEEDS_INPUT"
    elif scope == "evidence":
        for plan in store.plans():
            if plan["payload"].get("evidence_revision_id") == target:
                affected["plans"] += store.invalidate_plan_reviews(
                    plan["plan_revision_id"], reason)
    elif scope == "plan":
        if not target:
            raise FlowError("INVALID_SCOPE", "plan 失效需要 target", stage="invalidate")
        affected["plans"] = store.invalidate_plan_reviews(target, reason)
    elif scope == "asset":
        if not target:
            raise FlowError("INVALID_SCOPE", "asset 失效需要 target", stage="invalidate")
        affected["asset_reviews"] = store.invalidate_asset_review(target, reason)
    elif scope == "render":
        affected["final_reviews"] = store.invalidate_final_reviews(reason)
    else:
        raise FlowError("INVALID_SCOPE", scope, stage="invalidate")
    return affected
