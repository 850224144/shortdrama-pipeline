"""确定性校验：引用、时间码、前提、时长与差异化。

只校验能被机械核实的部分；语义问题留给审批与人工复核，不用模型自评冒充证据。
"""
from __future__ import annotations

from . import settings

DETECTOR_VERSION = "validator-1"


def _check(check_id: str, status: str, reason: str, *, refs: list | None = None,
           severity: str = "blocking", review_required: bool = False) -> dict:
    return {"check_id": check_id, "status": status, "severity": severity,
            "reason": reason, "evidence_refs": refs or [],
            "detector_version": DETECTOR_VERSION,
            "review_required": review_required}


def _overlap_ms(left: dict, right: dict) -> int:
    return max(0, min(left["source_end_ms"], right["source_end_ms"])
               - max(left["source_start_ms"], right["source_start_ms"]))


def validate_plan(plan: dict, story: dict, evidence_snapshot: dict,
                  *, policy: dict | None = None) -> dict:
    pol = policy or settings.policy()
    duration_ms = int(evidence_snapshot["duration_ms"])
    utterances = {u["utterance_id"] for u in evidence_snapshot["evidence"]["utterances"]}
    events = {e["event_id"]: e for e in story["events"]}
    checks: list[dict] = []
    shots = plan.get("shots") or []

    if not shots:
        checks.append(_check("PLAN_SHOTS", "FAIL", "方案没有镜头", severity="blocking"))
        return _result(checks, shots)

    shot_ids = [s.get("shot_id") for s in shots]
    if len(set(shot_ids)) != len(shot_ids) or any(not s for s in shot_ids):
        checks.append(_check("SHOT_ID_UNIQUE", "FAIL", "镜头 ID 缺失或重复",
                             refs=shot_ids))
    for shot in shots:
        shot_id = shot.get("shot_id")
        for event_id in shot.get("event_ids") or []:
            if event_id not in events:
                checks.append(_check("EVENT_REFERENCE", "FAIL",
                                     f"{shot_id} 引用不存在的事件 {event_id}",
                                     refs=[event_id]))
        missing = [uid for uid in shot.get("evidence_ids") or [] if uid not in utterances]
        if missing:
            checks.append(_check("EVIDENCE_REFERENCE", "FAIL",
                                 f"{shot_id} 引用不存在的证据 {missing}",
                                 refs=missing))
        start, end = shot.get("source_start_ms"), shot.get("source_end_ms")
        if (type(start) is not int or type(end) is not int or start < 0
                or end <= start or end > duration_ms):
            checks.append(_check("SOURCE_RANGE", "FAIL",
                                 f"{shot_id} 源片区间非法或越界",
                                  refs=[shot_id]))
            continue
        planned = shot.get("planned_ms")
        if type(planned) is not int or planned <= 0 or planned > (end - start):
            checks.append(_check("SHOT_DURATION", "FAIL",
                                 f"{shot_id} 计划时长 {planned} 不在可用区间内",
                                 refs=[shot_id]))

    # 前提依赖：不能只给“这一巴掌”而不给原因。
    positioned = {s["shot_id"]: index for index, s in enumerate(shots)}
    shown_events = {eid for s in shots for eid in (s.get("event_ids") or [])}
    for index, shot in enumerate(shots):
        for event_id in shot.get("event_ids") or []:
            for prerequisite in events.get(event_id, {}).get("prerequisite_event_ids", []):
                if prerequisite not in shown_events:
                    checks.append(_check("PREREQUISITE_MISSING", "FAIL",
                                         f"{shot['shot_id']} 的事件缺少前提 {prerequisite}",
                                         refs=[event_id, prerequisite]))
                else:
                    previous = next((s for s in shots if prerequisite in (s.get("event_ids") or [])),
                                    None)
                    if previous and positioned.get(previous["shot_id"], 99) > index:
                        checks.append(_check("PREREQUISITE_ORDER", "PENDING",
                                             "前提镜头排在结果之后；倒叙必须显式标注",
                                             refs=[prerequisite, event_id],
                                             severity="warning", review_required=True))

    # 同源片段重复使用：第一版限制，必须显式理由与审核才允许。
    for i, left in enumerate(shots):
        for right in shots[i + 1:]:
            overlap = _overlap_ms(left, right)
            shortest = min(left["source_end_ms"] - left["source_start_ms"],
                           right["source_end_ms"] - right["source_start_ms"])
            if shortest > 0 and overlap / shortest > 0.5:
                checks.append(_check("DUPLICATE_SOURCE_USE", "FAIL",
                                     f"{left['shot_id']} 与 {right['shot_id']} 重复使用同一源片区间",
                                     refs=[left["shot_id"], right["shot_id"]]))

    total = sum(int(s.get("planned_ms") or 0) for s in shots)
    minimum, maximum = (int(x * 1000) for x in pol["delivery_duration_range_sec"])
    if total < minimum and not pol.get("allow_shorter_than_min"):
        checks.append(_check("DURATION_BELOW_POLICY", "FAIL",
                             f"计划时长 {total / 1000:.1f}s 低于项目下限 {minimum / 1000:.1f}s；"
                             "应回策划补必要内容，不靠循环或慢放"))
    elif total > maximum:
        checks.append(_check("DURATION_ABOVE_POLICY", "WARN",
                             f"计划时长 {total / 1000:.1f}s 超过交付上限 {maximum / 1000:.1f}s",
                             severity="warning", review_required=True))
    if len(shots) > pol["max_shots"]:
        checks.append(_check("TOO_MANY_SHOTS", "WARN",
                             f"镜头数 {len(shots)} 超过建议上限 {pol['max_shots']}",
                             severity="warning"))

    ending = shots[-1]
    ending_events = [events[eid] for eid in (ending.get("event_ids") or []) if eid in events]
    if ending.get("role") not in ("ending", "turn"):
        checks.append(_check("ENDING_ROLE", "WARN",
                             f"最后一镜角色是 {ending.get('role')}，结尾动机可能不成立",
                             severity="warning", review_required=True))
    if any(e.get("resolution_marker") for e in ending_events):
        checks.append(_check("SPOILER_TOO_EARLY", "FAIL",
                             "结尾使用了收束/大结局语句，可能提前泄底", refs=[ending["shot_id"]]))
    if len(shots) < 2:
        checks.append(_check("NO_BUILD_UP", "FAIL", "不足两镜，无法建立递进与观看动机"))

    answers = plan.get("answers") or {}
    missing_answers = [k for k in ("what_first", "who_wants", "obstacle",
                                   "new_info", "ending_question")
                       if not str(answers.get(k) or "").strip()]
    if missing_answers:
        checks.append(_check("FIVE_QUESTIONS", "FAIL",
                             f"五个必答问题缺失：{missing_answers}"))
    perspective = (plan.get("perspective") or {}).get("char_id")
    known_characters = {c["char_id"] for c in story["characters"]}
    if perspective and perspective not in known_characters:
        checks.append(_check("PERSPECTIVE_CHARACTER", "FAIL",
                             f"主视角角色 {perspective} 不在人物表中"))

    pending = [s["shot_id"] for s in shots
               if any(events.get(eid, {}).get("verification_status") != "verified"
               for eid in (s.get("event_ids") or []))]
    if pending:
        checks.append(_check("FACTS_NEED_REVIEW", "PENDING",
                             f"{len(pending)} 个镜头依赖未人工核实的剧情事实",
                             refs=pending, review_required=True))
    return _result(checks, shots, total=total, pending=pending)


def validate_differentiation(proposals: list[dict]) -> dict:
    checks = []
    if not proposals:
        checks.append(_check("PROPOSAL_COUNT", "FAIL", "没有有效方案"))
        return _result(checks, [])
    signatures = {(p["mechanism"], p["opening"], p["turn"], p["ending"])
                  for p in proposals}
    if len(proposals) > 1 and len(signatures) < len(proposals):
        checks.append(_check("PLAN_NOT_DIFFERENTIATED", "FAIL",
                             "存在机制与关键切点完全相同的方案"))
    if len(proposals) > 1:
        mechanisms = {p["mechanism"] for p in proposals}
        if len(mechanisms) < 2:
            checks.append(_check("SINGLE_MECHANISM", "WARN",
                                 "多个方案共用同一机制，差异可能不足",
                                 severity="warning", review_required=True))
    for proposal in proposals:
        if proposal.get("insufficient_material"):
            checks.append(_check("INSUFFICIENT_MATERIAL", "WARN",
                                 f"{proposal['proposal_id']} 有效时长不足",
                                 severity="warning", review_required=True))
    return _result(checks, proposals)


def _result(checks: list[dict], subjects, **extra) -> dict:
    blocking = [c for c in checks if c["status"] == "FAIL"]
    pending = [c for c in checks if c["status"] == "PENDING"]
    warnings = [c for c in checks if c["status"] == "WARN"]
    return {"ok": not blocking, "checks": checks, "blocking": blocking,
            "pending": pending, "warnings": warnings,
            "blocking_issues": [c["reason"] for c in blocking], **extra}
