"""剧情理解：人物、事件、前提关系与双时间线。

这一层只产出“可定位的分析结果”，全部标记 needs_review；说话人、画面人物和
旁白分别记录，不互相替代；没有证据的字段留空而不是编造。
"""
from __future__ import annotations

import re

from ..source_identity import json_hash
from . import settings


STORY_VERSION = "1"


def _short(value: str, length: int = 12) -> str:
    return json_hash(value)[:length]


def _has(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def _first_evidence(texts: dict, words: list[str]) -> str | None:
    for uid, text in texts.items():
        if _has(text, words):
            return uid
    return None


def _turn_split(utterances: list[dict]) -> list[dict]:
    """按停顿切说话轮次；这是启发式，不据此认定姓名或真实身份。"""
    turns: list[dict] = []
    current = None
    speaker_index = 0
    for utterance in utterances:
        gap = 0 if current is None else utterance["start_ms"] - current["end_ms"]
        new_turn = current is None or gap >= 700 or (
            current["text"][-1:] in "。！？!?" and gap >= 300)
        if new_turn:
            if current is not None:
                speaker_index = 1 - speaker_index
            label = "spk_a" if speaker_index == 0 else "spk_b"
            current = {"turn_id": "turn_" + _short(
                f"{utterance['utterance_id']}|{label}"), "speaker": label,
                "start_ms": utterance["start_ms"], "end_ms": utterance["end_ms"],
                "utterance_ids": [], "text": ""}
            turns.append(current)
        current["utterance_ids"].append(utterance["utterance_id"])
        current["end_ms"] = max(current["end_ms"], utterance["end_ms"])
        current["text"] = (current["text"] + " " + utterance["text"]).strip()
    return turns


def _event_boundary(previous: dict, current: dict, count: int) -> bool:
    gap = current["start_ms"] - previous["end_ms"]
    if gap >= 1500:
        return True
    if count >= 10:
        return True
    if current["text"].lstrip().startswith(("（", "(", "【")) and count >= 2:
        return True
    return False


def _group_events(turns: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    for turn in turns:
        if current and _event_boundary(current[-1], turn, len(current)):
            groups.append(current)
            current = []
        current.append(turn)
    if current:
        groups.append(current)
    return groups


def _summarise(group: list[dict], markers: dict, utterance_text: dict) -> dict:
    texts = {uid: utterance_text.get(uid, "")
             for turn in group for uid in turn["utterance_ids"]}
    combined = " ".join(turn["text"] for turn in group)
    speakers = sorted({turn["speaker"] for turn in group})
    action = "对话推进"
    if (_has(combined, markers["goal_markers"])
            and _has(combined, markers["obstacle_markers"])):
        action = "提出请求后被拒绝"
    elif _has(combined, markers["new_information_markers"]):
        action = "信息被揭开"
    elif _has(combined, markers["turn_markers"]):
        action = "局面发生转折"
    elif _has(combined, markers["reaction_markers"]):
        action = "情绪反应升级"
    partners = "与".join(speakers) if len(speakers) > 1 else (speakers[0] if speakers else "")
    return {
        "action": f"{partners}{action}" if partners else action,
        "goal": _first_evidence(texts, markers["goal_markers"]),
        "obstacle": _first_evidence(texts, markers["obstacle_markers"]),
        "new_information": [uid for uid, text in texts.items()
                            if _has(text, markers["new_information_markers"])],
        "stakes": [word for word in markers["stakes_markers"] if word in combined],
    }


def build_story(evidence_snapshot: dict, *, config: dict | None = None) -> dict:
    cfg = config or settings.config()
    utterances = list(evidence_snapshot["evidence"]["utterances"])
    utterance_text = {u["utterance_id"]: u["text"] for u in utterances}
    duration_ms = int(evidence_snapshot["duration_ms"])
    source_asset_id = evidence_snapshot["assets"][0]["asset_id"]
    markers = {key: cfg[key] for key in (
        "new_information_markers", "obstacle_markers", "goal_markers",
        "stakes_markers", "causal_markers", "turn_markers", "flashback_markers",
        "reaction_markers", "resolution_markers", "emotion_words")}

    turns = _turn_split(utterances)
    groups = _group_events(turns)

    characters: dict[str, dict] = {}
    for turn in turns:
        label = turn["speaker"]
        char_id = f"char_{label}"
        row = characters.setdefault(char_id, {
            "char_id": char_id, "display_name": None,
            "provisional_name": "说话人A（待确认）" if label == "spk_a" else "说话人B（待确认）",
            "aliases": [], "speaker_labels": [], "visual_tracks": [],
            "relationships": [], "appearance": {"status": "unobserved"},
            "reference_images": [], "voice_role": "unknown",
            "line_count": 0, "unconfirmed_fields": ["display_name", "visual_tracks"],
            "verification_status": "needs_review",
        })
        if label not in row["speaker_labels"]:
            row["speaker_labels"].append(label)
        row["line_count"] += len(turn["utterance_ids"])

    events: list[dict] = []
    for index, group in enumerate(groups):
        start = group[0]["start_ms"]
        end = group[-1]["end_ms"]
        event_id = "evt_" + _short(f"{source_asset_id}|{index}|{start}|{end}")
        summary = _summarise(group, markers, utterance_text)
        texts = {uid: utterance_text[uid]
                 for turn in group for uid in turn["utterance_ids"]}
        subtitle_text = " ".join(texts.values())
        evidence_ids = [group[0]["utterance_ids"][0]]
        for marker_key in ("new_information_markers", "obstacle_markers",
                           "goal_markers", "turn_markers"):
            uid = _first_evidence(texts, markers[marker_key])
            if uid and uid not in evidence_ids:
                evidence_ids.append(uid)
        last_uid = group[-1]["utterance_ids"][-1]
        if last_uid not in evidence_ids:
            evidence_ids.append(last_uid)
        prerequisite = None
        if _has(subtitle_text, markers["causal_markers"]) and events:
            prerequisite = events[-1]["event_id"]
        events.append({
            "event_id": event_id,
            "source_asset_id": source_asset_id,
            "source_spans": [{"start_ms": start, "end_ms": end}],
            "participants": sorted({turn["speaker"] for turn in group}),
            "character_ids": sorted({f"char_{turn['speaker']}" for turn in group}),
            "action": summary["action"],
            "goal": summary["goal"],
            "obstacle": summary["obstacle"],
            "stakes": summary["stakes"],
            "new_information": summary["new_information"],
            "prerequisite_event_ids": [prerequisite] if prerequisite else [],
            "presentation_index": index,
            "dialogue_text": subtitle_text,
            "evidence_ids": evidence_ids,
            "verification_status": "needs_review",
            "inference": "heuristic_not_fact",
            "flashback_candidate": _has(subtitle_text, markers["flashback_markers"]),
            "resolution_marker": _has(subtitle_text, markers["resolution_markers"]),
        })

    # 双时间线：呈现顺序始终确定；剧情时序只保留可证实的局部约束。
    constraints = []
    for event in events:
        for prerequisite in event["prerequisite_event_ids"]:
            constraints.append({"before": prerequisite, "after": event["event_id"],
                                "reason": "causal_marker_heuristic",
                                "verification_status": "needs_review"})
    flashbacks = [e["event_id"] for e in events if e["flashback_candidate"]]
    story_time = {
        "status": "presentation_assumed" if not flashbacks else "partially_unknown",
        "constraints": constraints,
        "unknown_segments": flashbacks,
        "note": "未人工核实的时序不得当作既定事实用于倒叙开场",
    }

    relationships = []
    for i, left in enumerate(events):
        for right in events[i + 1:]:
            shared = set(left["character_ids"]) & set(right["character_ids"])
            if len(shared) >= 2 or (left["character_ids"] and
                                    set(left["character_ids"]) & set(right["character_ids"])):
                key = tuple(sorted(set(left["character_ids"]) | set(right["character_ids"])))
                existing = next((r for r in relationships if r["characters"] == list(key)), None)
                if existing:
                    existing["event_count"] += 1
                    if right["event_id"] not in existing["evidence_event_ids"]:
                        existing["evidence_event_ids"].append(right["event_id"])
                elif len(key) >= 2:
                    relationships.append({
                        "relationship_id": "rel_" + _short(str(key)),
                        "characters": list(key),
                        "kind": "unconfirmed_co_occurrence",
                        "evidence_event_ids": [left["event_id"], right["event_id"]],
                        "event_count": 2,
                        "verification_status": "needs_review",
                    })

    covered_ms = sum(e["source_spans"][0]["end_ms"] - e["source_spans"][0]["start_ms"]
                     for e in events)
    coverage = {"utterance_count": len(utterances), "event_count": len(events),
                "covered_ms": covered_ms, "duration_ms": duration_ms,
                "ratio": round(covered_ms / duration_ms, 4) if duration_ms else 0.0,
                "first_utterance_ms": utterances[0]["start_ms"] if utterances else None,
                "last_utterance_ms": utterances[-1]["end_ms"] if utterances else None}

    issues = list(evidence_snapshot["evidence"].get("issues") or [])
    issues.append({"code": "SPEAKER_HEURISTIC", "severity": "warning",
                   "message": "说话人标签为停顿启发式，未做声纹或人工确认"})
    if flashbacks:
        issues.append({"code": "TIMELINE_PARTIAL", "severity": "warning",
                       "message": "检测到疑似回忆/倒叙语句，剧情时序只保留局部约束"})

    return {
        "schema_version": 1, "story_version": STORY_VERSION,
        "source_asset_id": source_asset_id, "duration_ms": duration_ms,
        "characters": list(characters.values()),
        "events": events,
        "relationships": relationships,
        "presentation_order": [e["event_id"] for e in events],
        "story_time": story_time,
        "coverage": coverage,
        "issues": issues,
        "verification_status": "needs_review",
        "note": "自动提取不等于人工核实；关键事实须在方案页核对",
    }
