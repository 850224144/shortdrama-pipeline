"""候选召回：覆盖整个首集，分块处理并保留重叠上下文，再按全局信号合并。

只产出候选与理由，不决定最终方案；素材不足时如实返回覆盖缺口。
"""
from __future__ import annotations

from .. import config as legacy_cfg
from . import settings


def _words_in(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in text]


def score_event(event: dict, cfg: dict, conflict_words: list[str],
                suspense_words: list[str]) -> dict:
    text = event["dialogue_text"]
    mechanism_hits = {}
    signal_total = 0
    for mechanism in cfg["mechanisms"]:
        hits = _words_in(text, mechanism["signals"])
        if hits:
            mechanism_hits[mechanism["id"]] = hits
            signal_total += min(len(hits), 3) * 4
    conflict = _words_in(text, conflict_words)
    suspense = _words_in(text, suspense_words)
    new_info = len(event["new_information"])
    obstacle = 1 if event["obstacle"] else 0
    goal = 1 if event["goal"] else 0
    turn = 1 if _words_in(text, cfg["turn_markers"]) else 0
    reaction = 1 if _words_in(text, cfg["reaction_markers"]) else 0
    question = 1 if ("?" in text or "？" in text) else 0
    stakes = min(len(event["stakes"]), 2)
    score = (signal_total + len(conflict) * 3 + len(suspense) * 2
             + new_info * 9 + obstacle * 6 + goal * 4 + turn * 7
             + reaction * 4 + question * 3 + stakes * 3)
    reasons = []
    if mechanism_hits:
        reasons.append("机制信号：" + "、".join(
            f"{mid}({','.join(hits[:2])})" for mid, hits in list(mechanism_hits.items())[:2]))
    if new_info:
        reasons.append(f"新增信息 {new_info} 处")
    if obstacle:
        reasons.append("存在明确阻碍/拒绝")
    if turn:
        reasons.append("含转折标记")
    if question:
        reasons.append("含未答问题")
    role = "turn" if (new_info or turn) else (
        "obstacle" if obstacle else ("opening" if conflict else "setup"))
    return {"score": score, "mechanisms": list(mechanism_hits.keys()),
            "reasons": reasons or ["常规叙事推进"],
            "role": role, "question": bool(question), "reaction": bool(reaction),
            "conflict": bool(conflict), "new_information": new_info}


def _chunks(items: list[dict], size: int, overlap: int) -> list[list[dict]]:
    if size <= 0 or len(items) <= size:
        return [items]
    step = max(1, size - overlap)
    return [items[i:i + size] for i in range(0, len(items), step)
            if items[i:i + size]]


def recall(story: dict, *, policy: dict | None = None,
           config: dict | None = None) -> dict:
    cfg = config or settings.config()
    pol = policy or settings.policy()
    events = story["events"]
    if not events:
        return {"candidates": [], "coverage": {"event_total": 0, "covered": 0,
                                               "uncovered_event_ids": [],
                                               "chunks": 1},
                "issues": [{"code": "INSUFFICIENT_MATERIAL", "severity": "blocking",
                            "message": "首集没有可定位的事件"}]}
    kb = legacy_cfg.keywords()
    conflict_words = kb.get("conflict_words", [])
    suspense_words = kb.get("suspense_words", [])
    scores = {e["event_id"]: score_event(e, cfg, conflict_words, suspense_words)
              for e in events}
    by_id = {e["event_id"]: e for e in events}
    order = list(story["presentation_order"])

    chunk_rows = _chunks(order, int(pol["chunk_events"]), int(pol["chunk_overlap"]))
    picked: set[str] = set()
    for chunk in chunk_rows:
        ranked = sorted(chunk, key=lambda eid: (-scores[eid]["score"], eid))
        qualified = [eid for eid in ranked if scores[eid]["score"] >= pol["min_candidate_score"]]
        picked.update(qualified[:max(3, int(pol["proposal_limit"]) * 2)])
        if not qualified:
            picked.add(ranked[0])

    # 每个候选补齐前后各一个上下文事件，避免只截“这一巴掌”的碎片。
    expanded: set[str] = set()
    for event_id in picked:
        index = order.index(event_id)
        for offset in (-1, 0, 1):
            if 0 <= index + offset < len(order):
                expanded.add(order[index + offset])

    # 保证最小候选规模：阈值只用于排序优先级，不得让整集只剩两三个候选。
    need = min(int(pol["min_candidates"]), len(order))
    if len(expanded) < need:
        for event_id in sorted(order, key=lambda eid: (-scores[eid]["score"],
                                                       by_id[eid]["presentation_index"])):
            expanded.add(event_id)
            if len(expanded) >= need:
                break

    candidates = []
    for event_id in sorted(expanded, key=lambda eid: (-scores[eid]["score"],
                                                     by_id[eid]["presentation_index"])):
        event = by_id[event_id]
        index = order.index(event_id)
        context = [order[i] for i in (index - 1, index + 1) if 0 <= i < len(order)]
        candidates.append({
            "event_id": event_id,
            "score": scores[event_id]["score"],
            "role": scores[event_id]["role"],
            "mechanisms": scores[event_id]["mechanisms"],
            "reasons": scores[event_id]["reasons"],
            "context_event_ids": context,
            "start_ms": event["source_spans"][0]["start_ms"],
            "end_ms": event["source_spans"][0]["end_ms"],
            "question": scores[event_id]["question"],
            "reaction": scores[event_id]["reaction"],
            "conflict": scores[event_id]["conflict"],
            "new_information": scores[event_id]["new_information"],
        })

    covered = {c["event_id"] for c in candidates} | {
        cid for c in candidates for cid in c["context_event_ids"]}
    uncovered = [eid for eid in order if eid not in covered]
    issues = []
    if len(candidates) < int(pol["min_candidates"]):
        issues.append({"code": "FEW_CANDIDATES", "severity": "warning",
                       "message": f"全片仅召回 {len(candidates)} 个候选，低于建议下限"})
    if uncovered:
        issues.append({"code": "COVERAGE_GAP", "severity": "warning",
                       "message": f"{len(uncovered)} 个事件未进入候选或上下文",
                       "uncovered_event_ids": uncovered})
    return {"candidates": candidates,
            "coverage": {"event_total": len(order), "covered": len(covered),
                         "uncovered_event_ids": uncovered, "chunks": len(chunk_rows),
                         "chunk_size": int(pol["chunk_events"]),
                         "overlap": int(pol["chunk_overlap"])},
            "issues": issues}
