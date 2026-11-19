"""通用钩子策划：候选 → 多方案 → 差异化 → 自评。

题材只影响偏好与表达，不强制某种关系或事件；没有证据的剧情不补。
"""
from __future__ import annotations

import math

from ..source_identity import json_hash
from . import settings
from .recall import recall


SHOT_ROLES = ("opening", "setup", "escalation", "turn", "ending")


def _short(value: str, length: int = 14) -> str:
    return json_hash(value)[:length]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _event_index(story: dict) -> dict:
    return {e["event_id"]: e for e in story["events"]}


def _pick(candidates: list[dict], used: set[str], *, roles: tuple = (),
          mechanism_id: str | None = None, after: int | None = None,
          prefer_question: bool = False) -> dict | None:
    pool = [c for c in candidates if c["event_id"] not in used]
    if roles:
        pool = [c for c in pool if c["role"] in roles]
    if mechanism_id:
        matching = [c for c in pool if mechanism_id in c["mechanisms"]]
        if matching:
            pool = matching
    if after is not None:
        pool = [c for c in pool if c["start_ms"] >= after] or pool
    if prefer_question:
        questioning = [c for c in pool if c.get("question")]
        if questioning:
            pool = questioning
    if not pool:
        return None
    pool.sort(key=lambda c: (-c["score"], c["start_ms"]))
    return pool[0]


def _event_pick(event: dict) -> dict:
    """把未进入候选池的事件包装成最小可用结构，避免前提信息被召回截断。"""
    return {"event_id": event["event_id"], "score": 0, "role": "setup",
            "mechanisms": [], "start_ms": event["source_spans"][0]["start_ms"],
            "end_ms": event["source_spans"][0]["end_ms"], "question": False,
            "reaction": 0, "new_information": len(event.get("new_information") or []),
            "conflict": False, "context_event_ids": [],
            "reasons": ["前提补全（未进入候选池）"]}


def _shot(event: dict, role: str, source_limit_ms: int, handles: int = 300) -> dict:
    span_start = event["source_spans"][0]["start_ms"]
    span_end = event["source_spans"][0]["end_ms"]
    start = max(0, span_start - handles)
    end = min(int(source_limit_ms), span_end + handles)
    return {
        "shot_id": f"shot_{role}_{_short(event['event_id'] + role, 8)}",
        "role": role,
        "event_ids": [event["event_id"]],
        "evidence_ids": list(event["evidence_ids"]),
        "source_start_ms": start,
        "source_end_ms": max(start + 1, end),
        "planned_ms": max(1, end - start),
        "dialogue": event["dialogue_text"][:160],
        "visual_hint": "沿用原片动作与反应；人物身份以已确认参考为准",
        "character_ids": list(event["character_ids"]),
        "verification_status": event["verification_status"],
    }


def _fit_durations(shots: list[dict], target_ms: int, minimum_ms: int,
                   maximum_ms: int, *, min_shot: int, max_shot: int) -> int:
    """在可用区间内伸缩；不循环、不慢放、不空镜拖长。"""
    for shot in shots:
        available = max(1, shot["source_end_ms"] - shot["source_start_ms"])
        shot["planned_ms"] = int(_clamp(available, min(min_shot, available),
                                        min(max_shot, available)))
    total = sum(s["planned_ms"] for s in shots)
    if total > maximum_ms and shots:
        scale = target_ms / total
        for shot in shots:
            available = max(1, shot["source_end_ms"] - shot["source_start_ms"])
            shot["planned_ms"] = int(_clamp(math.floor(shot["planned_ms"] * scale),
                                            1, min(max_shot, available)))
        total = sum(s["planned_ms"] for s in shots)
    return total


def _score(pick: dict, scores: dict, total_ms: int, minimum_ms: int,
           maximum_ms: int, policy: dict) -> dict:
    opening = pick.get("opening")
    escalation = pick.get("escalation")
    turn = pick.get("turn")
    ending = pick.get("ending")
    setup = pick.get("setup")
    events = {eid: c for eid, c in scores.items()}

    opening_score = 25
    if opening:
        opening_score = min(100, 35 + opening["score"])
        if opening.get("conflict"):
            opening_score += 10
        if len((events[opening["event_id"]].get("context_event_ids") or [])) < 1:
            opening_score -= 5
    clarity = 95
    if not setup:
        clarity -= 15
    clarity = int(_clamp(clarity, 30, 100))
    if escalation:
        delta = events[escalation["event_id"]]["score"] - (events[opening["event_id"]]["score"]
                                                           if opening else 0)
        escalation_score = int(_clamp(55 + delta * 2, 30, 100))
    else:
        escalation_score = 25
    turn_score = 25
    if turn:
        turn_score = 90 if events[turn["event_id"]]["new_information"] else 60
        if opening and turn["event_id"] == opening["event_id"]:
            turn_score -= 25
    ending_score = 30
    if ending:
        ending_score = 85 if ending.get("question") else 65
        if events[ending["event_id"]].get("resolution_marker"):
            ending_score = 25
        if len({ending["event_id"], turn["event_id"] if turn else None}) < 2:
            ending_score -= 15
    feasibility = 100
    if not (minimum_ms <= total_ms <= maximum_ms):
        feasibility -= 30
    if len(pick.get("shots") or []) > policy["max_shots"]:
        feasibility -= 15
    feasibility = int(_clamp(feasibility, 20, 100))

    weights = policy["scoring_weights"]
    parts = {"opening": opening_score, "clarity": clarity,
             "escalation": escalation_score, "turn": turn_score,
             "ending": ending_score, "feasibility": feasibility}
    total = round(sum(parts[k] * weights[k] for k in weights) / sum(weights.values()), 1)
    return {**parts, "total": total}


def _answers(events: dict, pick: dict, perspective: dict) -> dict:
    opening_event = events[pick["opening"]["event_id"]] if pick.get("opening") else None
    turn_event = events[pick["turn"]["event_id"]] if pick.get("turn") else None
    ending_event = events[pick["ending"]["event_id"]] if pick.get("ending") else None
    name = (perspective or {}).get("name") or "主角（待确认）"
    who = name
    if opening_event and events:
        others = [c for c in opening_event["character_ids"] if c != perspective.get("char_id")]
        if others:
            who = f"{name}面对{len(others)}名在场角色"
    obstacle_text = ""
    if opening_event and opening_event["obstacle"]:
        obstacle_text = "对方在对话中明确拒绝或阻止"
    elif pick.get("escalation"):
        obstacle_text = events[pick["escalation"]["event_id"]]["action"]
    return {
        "what_first": (f"先看到{opening_event['action']}：{opening_event['dialogue_text'][:60]}"
                       if opening_event else "无有效开场"),
        "who_wants": f"{who}想改变当前处境（依据事件目标与施压语句）",
        "obstacle": obstacle_text or "阻碍在首集证据中不明确，需人工核对",
        "new_info": (f"中段揭示：{turn_event['action']}（{turn_event['dialogue_text'][:40]}）"
                     if turn_event else "没有新的信息变化"),
        "ending_question": _ending_question(ending_event, perspective),
    }


def _ending_question(ending_event: dict | None, perspective: dict) -> str:
    if not ending_event:
        return "无结尾动机：素材不足"
    text = ending_event["dialogue_text"]
    question = next((seg for seg in text.replace("?", "？").split("？") if seg.strip()), "")
    if "？" in text and question:
        return f"结局停在：{question.strip()[:50]}？"
    stakes = "、".join(ending_event["stakes"]) or "当前处境"
    name = (perspective or {}).get("name") or "主角（待确认）"
    return f"{name}会如何应对{stakes}，{ending_event['action']}之后还会发生什么？"


def _perspective(story: dict, pick: dict) -> dict:
    used = [pick[role]["event_id"] for role in SHOT_ROLES if pick.get(role)]
    characters = {c["char_id"]: c for c in story["characters"]}
    counts: dict[str, int] = {}
    for event_id in used:
        event = next(e for e in story["events"] if e["event_id"] == event_id)
        for char_id in event["character_ids"]:
            counts[char_id] = counts.get(char_id, 0) + 1
    if not counts:
        return {"char_id": None, "name": "主角（待确认）"}
    char_id = sorted(counts, key=lambda c: (-counts[c], c))[0]
    char = characters.get(char_id, {})
    return {"char_id": char_id,
            "name": char.get("provisional_name") or char.get("display_name") or "主角（待确认）",
            "verification_status": "needs_review"}


def _mechanism_rank(candidates: list[dict], mechanisms: list[dict]) -> list[dict]:
    ranked = []
    for mechanism in mechanisms:
        related = [c for c in candidates if mechanism["id"] in c["mechanisms"]]
        ranked.append({"mechanism": mechanism,
                       "score": sum(c["score"] for c in related) + len(related) * 3,
                       "related": related})
    ranked.sort(key=lambda row: (-row["score"], row["mechanism"]["id"]))
    return [row["mechanism"] for row in ranked]


def _compose(story: dict, candidates: list[dict], mechanism: dict,
             policy: dict, evidence_revision_id: str) -> dict | None:
    events = _event_index(story)
    used: set[str] = set()
    pick: dict[str, dict] = {}

    order = story["presentation_order"]
    opening = _pick(candidates, used, roles=("opening", "obstacle", "turn"),
                    mechanism_id=mechanism["id"])
    if opening is None:
        opening = _pick(candidates, used, roles=("opening", "obstacle", "turn"))
    if opening is None:
        opening = _pick(candidates, used, mechanism_id=mechanism["id"])
    if opening is None:
        opening = _pick(candidates, used)
    if opening is None:
        return None
    # 开场不能是最后一个事件：必须留出后续升级空间，否则钩子没有推进。
    if len(order) > 2 and order.index(opening["event_id"]) >= len(order) - 1:
        alternative = next((c for c in sorted(candidates, key=lambda c: (-c["score"],
                                                                       c["start_ms"]))
                            if order.index(c["event_id"]) < len(order) - 1), None)
        if alternative is not None:
            opening = alternative
    pick["opening"] = opening
    used.add(opening["event_id"])

    opening_event = events[opening["event_id"]]
    setup = None
    prerequisite = next((pid for pid in opening_event["prerequisite_event_ids"]
                         if pid not in used), None)
    if prerequisite:
        setup = next((c for c in candidates if c["event_id"] == prerequisite), None)
        if setup is None and prerequisite in events:
            setup = _event_pick(events[prerequisite])
        if setup is None:
            # 开场依赖前提却无法补齐，直接判为不可理解，不硬凑方案。
            return None
    else:
        index = order.index(opening["event_id"])
        if index > 0:
            candidate = next((c for c in candidates if c["event_id"] == order[index - 1]),
                             None)
            if candidate and candidate["event_id"] not in used:
                setup = candidate
    if setup:
        used.add(setup["event_id"])
        pick["setup"] = setup

    after_ms = opening["start_ms"]
    escalation = _pick(candidates, used, roles=("escalation", "obstacle", "turn"),
                       mechanism_id=mechanism["id"], after=after_ms)
    if escalation is None:
        escalation = _pick(candidates, used, mechanism_id=mechanism["id"], after=after_ms)
    if escalation is None:
        escalation = _pick(candidates, used)
    if escalation:
        pick["escalation"] = escalation
        used.add(escalation["event_id"])
        after_ms = max(after_ms, escalation["end_ms"])

    turn = _pick(candidates, used, roles=("turn", "reveal"),
                 mechanism_id=mechanism["id"], after=after_ms)
    if turn is None:
        turn = _pick(candidates, used, roles=("turn", "reveal"))
    if turn is None:
        turn = _pick(candidates, used, mechanism_id=mechanism["id"])
    if turn:
        pick["turn"] = turn
        used.add(turn["event_id"])
        after_ms = max(after_ms, turn["end_ms"])

    ending = _pick(candidates, used, after=after_ms, prefer_question=True)
    if ending is None:
        ending = _pick(candidates, used, prefer_question=True)
    if ending is None:
        ending = _pick(candidates, used)
    if ending:
        pick["ending"] = ending
        used.add(ending["event_id"])

    # 时长：先按可用区间估，再按目标节奏伸缩；不足不补假内容。
    planned: list[dict] = []
    source_limit = int(story.get("coverage", {}).get("duration_ms") or 0)

    def _prerequisite_chain(event_id: str, found: set[str]) -> None:
        for pid in events[event_id].get("prerequisite_event_ids") or []:
            if pid in found or pid not in events:
                continue
            found.add(pid)
            _prerequisite_chain(pid, found)

    opening_pre: set[str] = set()
    _prerequisite_chain(pick["opening"]["event_id"], opening_pre)
    intro_ids = [eid for eid in order if eid in opening_pre]
    if "setup" in pick and pick["setup"]["event_id"] not in intro_ids:
        intro_ids.append(pick["setup"]["event_id"])

    for pid in intro_ids:
        planned.append(_shot(events[pid], "setup", source_limit, handles=300))
    planned.append(_shot(events[pick["opening"]["event_id"]], "opening",
                         source_limit, handles=300))

    for role in ("escalation", "turn", "ending"):
        if role not in pick:
            continue
        chain: set[str] = set()
        _prerequisite_chain(pick[role]["event_id"], chain)
        for pid in chain:
            if pid not in {s["event_ids"][0] for s in planned}:
                planned.append(_shot(events[pid], "setup", source_limit, handles=300))
        planned.append(_shot(events[pick[role]["event_id"]], role,
                             source_limit, handles=300))

    # 开场镜头必须在第一镜：先给钩子，再补前因；倒叙由 presentation_note 显式标注。
    opening_shot = next(s for s in planned if s["role"] == "opening")
    planned.remove(opening_shot)
    planned.insert(0, opening_shot)

    minimum = int(policy["delivery_duration_range_sec"][0] * 1000)
    maximum = int(policy["delivery_duration_range_sec"][1] * 1000)
    target = int(policy["target_duration_sec"] * 1000)
    total = _fit_durations(planned, target, minimum, maximum,
                           min_shot=int(policy["min_shot_sec"] * 1000),
                           max_shot=int(policy["max_shot_sec"] * 1000))

    # 素材不足时尝试补一段未使用的升级事件，仍不足则如实标记。
    extra_candidates = [c for c in candidates if c["event_id"] not in used]
    while total < minimum and extra_candidates and len(planned) < policy["max_shots"]:
        extra = extra_candidates.pop(0)
        event = events[extra["event_id"]]
        planned.insert(-1 if len(planned) > 1 else len(planned),
                       _shot(event, "escalation", source_limit))
        total = _fit_durations(planned, target, minimum, maximum,
                               min_shot=int(policy["min_shot_sec"] * 1000),
                               max_shot=int(policy["max_shot_sec"] * 1000))

    # 去掉重复使用的镜头（同一事件只出现一次）。
    seen, deduped = set(), []
    for shot in planned:
        key = tuple(shot["event_ids"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(shot)
    planned = deduped
    total = sum(s["planned_ms"] for s in planned)

    scores = _score({**pick, "shots": planned},
                    {c["event_id"]: c for c in candidates}, total,
                    minimum, maximum, policy)
    perspective = _perspective(story, pick)
    answers = _answers(events, pick, perspective)
    roles_order = [s["role"] for s in planned]
    order_note = None
    if "setup" in pick and "opening" in pick:
        setup_index = story["presentation_order"].index(pick["setup"]["event_id"])
        opening_index = story["presentation_order"].index(pick["opening"]["event_id"])
        if setup_index > opening_index:
            order_note = {"type": "flash_forward", "presentation_intent":
                          "先呈现强冲突，再回到起点补齐前因；不得暗示因果反转"}
    risks = ["说话人身份为启发式，需人工核对"]
    if total < minimum:
        risks.append("有效素材时长不足交付下限，需回策划或调整规格")
    if pick.get("ending") and events[pick["ending"]["event_id"]].get("resolution_marker"):
        risks.append("结尾事件含收束标记，可能提前泄底")
    if not pick.get("turn"):
        risks.append("缺少中段新信息，递进可能不足")
    complexity = "low" if len(planned) <= 4 else ("medium" if len(planned) <= 6 else "high")

    proposal_id = "prop_" + _short("|".join(
        [evidence_revision_id, mechanism["id"]]
        + [s["event_ids"][0] for s in planned]
        + [str(total)]))
    proposal = {
        "proposal_id": proposal_id,
        "evidence_revision_id": evidence_revision_id,
        "mechanism": mechanism["id"],
        "mechanism_name": mechanism["name"],
        "title": f"{mechanism['name']}：{events[pick['opening']['event_id']]['action']}",
        "selling_point": f"{mechanism['name']} + {answers['what_first'][:40]}",
        "perspective": perspective,
        "relationship_summary": _relationship_summary(story, planned),
        "opening": pick["opening"]["event_id"],
        "setup": pick["setup"]["event_id"] if pick.get("setup") else None,
        "escalation": pick["escalation"]["event_id"] if pick.get("escalation") else None,
        "turn": pick["turn"]["event_id"] if pick.get("turn") else None,
        "ending": pick["ending"]["event_id"] if pick.get("ending") else None,
        "answers": answers,
        "shots": planned,
        "planned_duration_ms": total,
        "delivery_range_ms": [minimum, maximum],
        "complexity": complexity,
        "risks": risks,
        "rejected_reasons": [],
        "scores": scores,
        "total_score": scores["total"],
        "score_reasons": _score_reasons(pick, candidates, planned),
        "presentation_note": order_note,
        "order": roles_order,
        "source_spans": [[s["source_start_ms"], s["source_end_ms"]] for s in planned],
        "verification_status": "needs_review",
        "insufficient_material": total < minimum,
    }
    return proposal


def _relationship_summary(story: dict, planned: list[dict]) -> str:
    chars = sorted({cid for shot in planned for cid in shot["character_ids"]})
    names = []
    for char_id in chars:
        row = next((c for c in story["characters"] if c["char_id"] == char_id), None)
        names.append(row.get("provisional_name") if row else char_id)
    return " / ".join(names) + "：关系未确认，按首集同场证据串联"


def _score_reasons(pick: dict, candidates: list[dict], planned: list[dict]) -> list[dict]:
    by_id = {c["event_id"]: c for c in candidates}
    reasons = []
    for role in SHOT_ROLES:
        candidate = pick.get(role)
        if not candidate:
            continue
        reasons.append({"dimension": role,
                        "event_id": candidate["event_id"],
                        "reason": "；".join(candidate.get("reasons") or ["常规叙事推进"])[:120]})
    if not reasons:
        reasons.append({"dimension": "feasibility", "reason": "无候选"})
    return reasons


def _overlap(left: dict, right: dict) -> float:
    used_left = {s["event_ids"][0] for s in left["shots"]}
    used_right = {s["event_ids"][0] for s in right["shots"]}
    if not used_left and not used_right:
        return 1.0
    return len(used_left & used_right) / len(used_left | used_right)


def _differentiation(proposals: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for proposal in proposals:
        duplicate_of = None
        for existing in kept:
            same_mechanism = proposal["mechanism"] == existing["mechanism"]
            same_opening = proposal["opening"] == existing["opening"]
            same_turn = proposal["turn"] == existing["turn"] and proposal["turn"] is not None
            overlap = _overlap(proposal, existing)
            if same_mechanism or (overlap >= 0.6 and (same_opening or same_turn)):
                duplicate_of = existing
                break
        if duplicate_of:
            proposal["rejected_reasons"].append(
                f"与 {duplicate_of['proposal_id']} 结构重复（重合度 {_overlap(proposal, duplicate_of):.2f}）")
            continue
        kept.append(proposal)
    for proposal in kept:
        others = [p for p in kept if p is not proposal]
        proposal["differentiation"] = {
            "vs": [{"proposal_id": p["proposal_id"],
                    "overlap": round(_overlap(proposal, p), 3),
                    "mechanism_differs": proposal["mechanism"] != p["mechanism"],
                    "opening_differs": proposal["opening"] != p["opening"],
                    "ending_differs": proposal["ending"] != p["ending"]}
                   for p in others]
        }
    return kept


def build_proposals(story: dict, evidence_revision_id: str, *,
                    policy: dict | None = None, config: dict | None = None,
                    limit: int | None = None) -> dict:
    cfg = config or settings.config()
    pol = policy or settings.policy()
    limit = int(limit or pol["proposal_limit"])
    recalled = recall(story, policy=pol, config=cfg)
    candidates = recalled["candidates"]
    if not candidates:
        return {"status": "INSUFFICIENT_MATERIAL", "proposals": [],
                "reason": "首集没有可定位的候选事件", "recall": recalled}

    proposals = []
    for mechanism in _mechanism_rank(candidates, cfg["mechanisms"]):
        proposal = _compose(story, candidates, mechanism, pol, evidence_revision_id)
        if proposal is not None:
            proposals.append(proposal)
    proposals.sort(key=lambda p: (-p["total_score"], p["proposal_id"]))

    kept = _differentiation(proposals)[:limit]
    rejected = [{"proposal_id": p["proposal_id"],
                 "reasons": p["rejected_reasons"]} for p in proposals
                if p not in kept and p["rejected_reasons"]]
    status = "PROPOSED" if kept else "INSUFFICIENT_MATERIAL"
    shortfall = None
    if len(kept) < limit:
        shortfall = {"requested": limit, "effective": len(kept),
                     "reason": "可用结构不足，按实际有效数量输出，不凑数"}
    return {"status": status, "proposals": kept, "effective_count": len(kept),
            "shortfall": shortfall, "rejected": rejected,
            "recall": {"coverage": recalled["coverage"], "issues": recalled["issues"]},
            "policy": pol, "verification_status": "needs_review"}
