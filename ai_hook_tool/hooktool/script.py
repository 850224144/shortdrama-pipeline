# -*- coding: utf-8 -*-
"""Generate a source-anchored short-drama hook plan, ranked by conflict intensity."""
from __future__ import annotations

import re

from . import config as cfg, manifest as mf
from .hook_planner import candidate_id


def _plain(text: str, punct: list[str]) -> str:
    value = text or ""
    for mark in punct:
        value = value.replace(mark, "")
    return value.strip()


def _subtitle(text: str, punct: list[str], maximum: int) -> str:
    clean = _plain(text, punct)
    return clean if len(clean) <= maximum else clean[:maximum]


def _event_index(text: str, events: list[dict], used: set[int]) -> list[int]:
    for event in events:
        index = event["source_index"]
        if index in used:
            continue
        if event["text"] == text or text in event["text"] or event["text"] in text:
            return [index]
    return []


def _opening_group(events: list[dict], quotes: list[dict], hook: dict) -> list[dict]:
    """First three source seconds: build a contiguous real-ASR hook, never filler."""
    timed = [q for q in quotes if q.get("start") is not None and q.get("end") is not None
             and q["start"] < hook["opening_max_sec"]]
    if not timed:
        return []
    first = min(timed, key=lambda q: (q["start"], -q["score"]))
    start = next((i for i, event in enumerate(events)
                  if abs(event["start"] - first["start"]) < 0.05), None)
    if start is None:
        return [first]
    group = []
    end_at = first["start"] + hook["opening_max_sec"]
    for event in events[start:]:
        if event["start"] >= end_at:
            break
        group.append(event)
    return group


def _make_quote_pool(long_titles: list[str], txt_entries: list[dict],
                     quote_records: list[dict] | None, conflict: list[str],
                     suspense: list[str], high_phrases: list[str], low_phrases: list[str],
                     min_match: float, source_role_bonus: float = 6.0) -> list[dict]:
    pool: list[dict] = []
    by_text = {item["text"]: dict(item) for item in (quote_records or [])}
    for index, text in enumerate(long_titles):
        record = by_text.get(text, {"text": text, "start": None, "end": None,
                                    "score": 0.0, "source": "asr", "source_index": index})
        record["text"] = text
        record["role"] = ""
        record["dialogue_source"] = "asr"
        record["vo"] = False
        record["action_context"] = []
        pool.append(record)

    for entry in txt_entries:
        script_line = entry.get("line", "").strip()
        spoken_text = entry.get("asr_text", "").strip()
        start, end = entry.get("asr_start"), entry.get("asr_end")
        match_ratio = float(entry.get("asr_match", 0))
        if not script_line or not spoken_text or start is None or end is None or match_ratio < min_match:
            continue
        overlaps = [q for q in pool if q.get("start") is not None
                    and q["start"] < end and q["end"] > start]
        if not overlaps:
            continue
        base_score = max(q.get("score", 0) for q in overlaps)
        high_hits = [phrase for phrase in high_phrases if phrase in spoken_text]
        conflict_hits = [word for word in conflict if word in spoken_text]
        suspense_hits = [word for word in suspense if word in spoken_text]
        low_hits = [phrase for phrase in low_phrases if phrase in spoken_text]
        score = (base_score + source_role_bonus + min(4.0, len(high_hits) * 2.0)
                 + min(3.0, len(conflict_hits) * 0.7)
                 + min(2.0, len(suspense_hits) * 0.5) - len(low_hits) * 2.0)
        pool.append({"text": spoken_text, "start": start, "end": end, "score": round(score, 3),
                     "source": "asr+script_alignment", "source_index": min(
                         q.get("source_index", 0) for q in overlaps),
                     "role": entry.get("role", ""), "dialogue_source": "asr",
                     "script_text": script_line,
                     "vo": entry.get("vo", False), "action_context": entry.get(
                         "action_context", []), "scene": entry.get("scene", ""),
                     "asr_match": match_ratio,
                     "asr_event_indices": entry.get("asr_event_indices", []),
                     "high_hits": high_hits, "conflict_hits": conflict_hits,
                     "suspense_hits": suspense_hits, "low_signal_hits": low_hits})

    # Deduplicate transcript shards replaced by a source-script line, as well as
    # duplicate lines while keeping the strongest occurrence.
    strongest: dict[tuple, dict] = {}
    for item in pool:
        key = (item["text"], item.get("start"))
        if key not in strongest or item["score"] > strongest[key]["score"]:
            strongest[key] = item
    result = list(strongest.values())
    aligned = [item for item in result if item.get("source") == "asr+script_alignment"]
    punctuations = cfg.keywords()["punctuations"]
    result = [item for item in result
              if item.get("source") == "asr+script_alignment"
              or not any(item.get("start") is not None
                         and item["start"] < label["end"]
                         and item["end"] > label["start"]
                         and (_plain(item["text"], punctuations)
                              in _plain(label["text"], punctuations)
                              or _plain(label["text"], punctuations)
                              in _plain(item["text"], punctuations))
                         for label in aligned)]
    result.sort(key=lambda item: (-item["score"],
                                  item["start"] if item["start"] is not None else float("inf"),
                                  item.get("source_index", 0)))
    return result


def _score_marker(item: dict, maximum: float) -> tuple[str, int]:
    start = item.get("start")
    if start is None:
        return "", 0
    return (f"{start:.2f}-{item['end']:.2f}",
            int(round(max(0, min(100, item["score"] / max(maximum, 1.0) * 100)))))


def _action_for(item: dict, shot_no: int, segment: str) -> dict:
    actions = item.get("action_context", [])
    action_text = "；".join(actions)
    framing = "人物关系中景接反应近景"
    if any(term in action_text for term in ("扬手一巴掌", "一巴掌扇", "打了一巴掌")):
        motion, framing = action_text, "双人中景接反应特写"
    elif any(term in action_text for term in ("门推开", "推开门")):
        motion, framing = action_text, "场景中景接人物近景"
    elif any(term in action_text for term in ("冲进来", "闯入")):
        motion, framing = action_text, "动态全景接越肩镜头"
    elif action_text:
        motion = action_text
    else:
        motion = "不增加动作；用原场景中的站位、视线和反应呈现冲突。"

    if item.get("planning_hint", {}).get("role_in_hook") == "cliffhanger" or item.get("is_cliffhanger"):
        beat = "在未解信息处掐断"
    else:
        beat = ("冲突起势", "对抗升级", "人物反应")[(shot_no - 1) % 3]
    return {"motion": motion, "framing": framing, "beat": beat,
            "source_actions": actions,
            "continuity_note": "只呈现来源动作；不新增人物、动作或剧情事件。"}


def _append_camera_suggestion(prompt: str, suggestion: dict) -> str:
    labels = {
        "framing": "补充景别", "camera_movement": "运镜建议",
        "composition": "构图建议", "expression": "表情建议",
        "lighting": "光线建议", "sound_and_pacing": "声音节奏建议",
    }
    additions = [f"{label}：{suggestion[field]}。"
                 for field, label in labels.items() if suggestion.get(field)]
    return prompt + "".join(additions)


def _shot(shot_no: int, segment: str, role: str, ref_image: str,
          item: dict | None, events: list[dict], used_events: set[int],
          subtitle_cfg: dict, scene_hint: str, max_score: float) -> dict:
    text = item["text"] if item else ""
    if item and item.get("asr_event_indices"):
        event_indices = list(item["asr_event_indices"])
    elif item and item.get("source_index") is not None:
        event_indices = [item["source_index"]]
    else:
        event_indices = _event_index(text, events, used_events) if text else []
    for index in event_indices:
        used_events.add(index)
    start = item.get("start") if item else None
    end = item.get("end") if item else None
    action = _action_for(item or {"text": "", "action_context": []}, shot_no, segment)
    cue = text or "无台词"
    score = item.get("score", 0.0) if item else 0.0
    range_label = f"{start:.2f}-{end:.2f}s" if start is not None else "时间待确认"
    scene = item.get("scene", "") if item else ""
    camera = item.get("camera_suggestion", {}) if item else {}
    camera_text = "".join(
        f"{label}：{camera[field]}。" for field, label in (
            ("framing", "补充景别"), ("camera_movement", "运镜建议"),
            ("composition", "构图建议"), ("expression", "表情建议"),
            ("lighting", "光线建议"), ("sound_and_pacing", "声音节奏建议"))
        if camera.get(field))
    prompt = (
        f"9:16竖屏，基于原剧场景{scene or scene_hint}，严格使用参考图中{role or '原剧人物'}的身份、脸、"
        f"发型和服装。镜头段落：{segment}，源片时间 {range_label}。原句：{cue}。"
        f"表演动作：{action['motion']} 景别和机位：{action['framing']}。"
        f"节奏节点：{action['beat']}。使用有明确视线和反应的剪辑，不增删原剧情事件。{camera_text}"
        "准确口播原句，普通话自然，口型同步；字幕采用简体中文并留出安全区域，"
        "不生成额外屏幕文字、字幕水印或生成工具标识。灯光服从原场景，不臆造空间布置。"
    )
    return {
        "shot": shot_no, "segment": segment, "role": role, "ref_image": ref_image,
        "prompt": prompt, "dialogue": text, "dialogue_source": item.get(
            "dialogue_source", "") if item else "",
        "vo": item.get("vo", False) if item else False,
        "subtitle": _subtitle(text, cfg.keywords()["punctuations"],
                               subtitle_cfg.get("max_chars_per_line", 14)),
        "subtitle_mode": "burned_overlay_or_voiceover",
        "source_time": {"start": start, "end": end, "asr_events": event_indices},
        "conflict_score": score, "conflict_percentile": _score_marker(
            item, max_score)[1] if item else 0,
        "emotion": item.get("emotion", "冲突") if item else "待确认",
        "scene": item.get("scene", "") if item else "",
        "candidate_id": candidate_id(item) if item else "",
        "planning_hint": item.get("planning_hint", {}) if item else {},
        "action": action,
        "opening_hook": bool(segment == mf.SEG_PRE and shot_no == 1),
        "is_cliffhanger": bool(segment == mf.SEG_HOOK and item and item.get("is_cliffhanger")),
    }


def build_script(material: dict, short_titles: list[str], long_titles: list[str],
                 genre: str, source_episode: str,
                 txt_entries: list[dict] | None = None,
                 quote_records: list[dict] | None = None,
                 transcript_lines: list[str] | None = None,
                 planning_hints: dict[str, dict] | None = None,
                 planning_status: dict | None = None,
                 include_planner_candidates: bool = False) -> dict:
    kw = cfg.keywords()
    hook_cfg = kw.get("hook", {})
    subtitle_cfg = kw.get("subtitle", {})
    faces = material.get("faces", {})
    roles = list(faces)
    transcript_lines = transcript_lines or []
    txt_entries = txt_entries or []
    planning_hints = planning_hints or {}
    planning_status = planning_status or {}
    events = []
    ts_re = re.compile(r"^\[([\d.]+)-([\d.]+)\]\s*(.*)$")
    for source_index, raw in enumerate(transcript_lines):
        match = ts_re.match(raw.strip())
        if match:
            events.append({"start": float(match.group(1)), "end": float(match.group(2)),
                           "text": match.group(3).strip(), "source_index": source_index,
                           "event_index": len(events)})

    genre_kw = cfg.keywords_for_genre(genre)
    pool = _make_quote_pool(long_titles, txt_entries, quote_records,
                            genre_kw["conflict_words"], genre_kw["suspense_words"],
                            genre_kw.get("high_intensity_phrases", []),
                            genre_kw.get("low_signal_phrases", []),
                            hook_cfg.get("min_source_match", 0.5),
                            hook_cfg.get("source_script_role_bonus", 6.0))
    known_indices = {item.get("source_index") for item in pool}
    for candidate in (quote_records or []):
        if not include_planner_candidates:
            break
        hint = planning_hints.get(candidate_id(candidate), {})
        if (candidate.get("source_index") in known_indices
                or candidate.get("start") is None or candidate.get("end") is None
                or hint.get("role_in_hook") not in {"opening", "escalation", "cliffhanger"}):
            continue
        item = dict(candidate)
        item.update(role="", dialogue_source="asr", vo=False, action_context=[],
                    scene="", candidate_id=candidate_id(candidate), planning_hint=hint)
        pool.append(item)
        known_indices.add(item.get("source_index"))
    opening = _opening_group(events, quote_records or [], hook_cfg)
    opening_indices = {event["source_index"] for event in opening}
    opening_times = {round(event["start"], 2) for event in opening}
    for item in pool:
        item["candidate_id"] = candidate_id(item)
        item["planning_hint"] = planning_hints.get(item["candidate_id"], {})
        if item["planning_hint"].get("role_in_hook") == "exclude":
            item["score"] = -1.0
    opening_by_indices = [item for item in pool
                          if item.get("source_index") in opening_indices
                          or (item.get("start") is not None
                              and round(item["start"], 2) in opening_times)
                          or any(i in item.get("asr_event_indices", []) for i in opening_indices)]

    # Restore verified speaker labels by exact transcript interval, while the script
    # remains a speaker/action annotation only. Every spoken word maps back to ASR.
    if planning_hints:
        opening_by_indices = [item for item in opening_by_indices
                              if item.get("planning_hint", {}).get("role_in_hook") == "opening"
                              or (item.get("score", 0) > 0
                                  and item.get("planning_hint", {}).get("role_in_hook") != "exclude")]

    opening_candidates = sorted(opening_by_indices,
                                key=lambda item: (item.get("planning_hint", {}).get("role_in_hook") != "opening",
                                                  -item.get("score", 0),
                                                  item.get("start", float("inf"))))
    first_hook = opening_candidates[0] if opening_candidates else None
    selected: list[dict] = [first_hook] if first_hook else []
    planned_openings = [item for item in pool
                        if item.get("planning_hint", {}).get("role_in_hook") == "opening"
                        and item not in selected and item.get("start") is not None
                        and item.get("end") is not None
                        and item["start"] < hook_cfg.get("opening_max_sec", 3.0)]
    if planned_openings:
        first_hook = min(planned_openings,
                         key=lambda item: item.get("start", float("inf")))
        selected = [first_hook]
    # Add the strongest distinct story beats from the same opening conflict,
    # followed by the source's crisis setup, keeping source order for continuity.
    pre_pool = [item for item in pool if item.get("start") is not None
                and item.get("start", 0) < 30 and item is not first_hook
                and item.get("planning_hint", {}).get("role_in_hook") != "exclude"
                and (not planning_hints or item.get("score", 0) > 0
                     or item.get("planning_hint", {}).get("role_in_hook") == "escalation")]
    pre_pool.sort(key=lambda item: (item.get("planning_hint", {}).get("role_in_hook") != "escalation",
                                    -item.get("score", 0),
                                    item.get("start", float("inf"))))
    planned_escalations = [item for item in pool
                           if item.get("planning_hint", {}).get("role_in_hook") == "escalation"
                           and item not in selected and item.get("start") is not None
                           and item["start"] < 30]
    pre_pool = planned_escalations + pre_pool
    for item in pre_pool:
        if any(item["start"] < prior["end"] and item["end"] > prior["start"]
               for prior in selected if prior.get("start") is not None):
            continue
        if item not in selected:
            selected.append(item)
        if len(selected) >= hook_cfg.get("min_pre_shots", 3):
            break
    selected.sort(key=lambda item: item.get("start", float("inf")))

    opening_end = max((item.get("end") or 0 for item in selected), default=0)
    post_opening = [item for item in pool if item.get("start") is not None
                    and item["start"] >= max(30.0, opening_end) and item not in selected]
    cliff_phrases = genre_kw.get("hook", {}).get("cliffhanger_phrases", [])
    cliff_pool = [item for item in post_opening
                  if item.get("planning_hint", {}).get("role_in_hook") == "cliffhanger"
                  or any(phrase in item["text"] for phrase in cliff_phrases)]
    cliff = max(cliff_pool, key=lambda item: (item.get("end", 0), item.get("score", 0)),
                default=None)
    hook_candidates: list[dict] = []
    if cliff is None:
        planned_cliffs = [item for item in pool
                          if item.get("planning_hint", {}).get("role_in_hook") == "cliffhanger"
                          and item not in selected and item.get("start") is not None
                          and item.get("start", 0) >= 30]
        cliff = max(planned_cliffs, key=lambda item: item.get("end", 0), default=None)
    if cliff is not None:
        lead_window = hook_cfg.get("cliffhanger_lead_window_sec", 15.0)
        lead_pool = [item for item in post_opening if item is not cliff
                     and 0 <= cliff["start"] - item["end"] <= lead_window]
        lead = max(lead_pool, key=lambda item: (item.get("score", 0), item.get("end", 0)),
                   default=None)
        if lead:
            hook_candidates.append(lead)
        cliff["is_cliffhanger"] = True
        hook_candidates.append(cliff)
    elif post_opening:
        hook_candidates = sorted(post_opening,
                                 key=lambda item: (item.get("score", 0),
                                                   item.get("end", 0)), reverse=True)[:2]
        hook_candidates.sort(key=lambda item: item.get("start", float("inf")))

    warns: list[str] = []
    if not roles:
        warns.append("无定妆/剧照参照，角色锁脸待补（PENDING）")
    if any(item.get("dialogue_source") == "txt" for item in selected + hook_candidates):
        warns.append("有台词无法由首集 ASR 对齐，禁止用于成片并已排除")
    chosen = selected + hook_candidates
    if any(not item.get("role") or item["role"] not in faces for item in chosen):
        warns.append("部分说话角色无对应参照图，相关镜头身份/造型待人工确认（PENDING）")
    if any("闪回" in "；".join(item.get("action_context", [])) for item in chosen):
        warns.append("钩子包含剧本闪回动作，前后场景承接需人工复核（PENDING）")
    scene_order = [item.get("scene") for item in chosen if item.get("scene")]
    if any(left != right for left, right in zip(scene_order, scene_order[1:])):
        warns.append("钩子包含跨场景镜头，时空/剪辑承接需人工复核（PENDING）")

    manifest = mf.empty_manifest(material["drama_name"], genre,
                                 cfg.get_setting("episode_label", "E01"), source_episode)
    pre = mf.segment(manifest, mf.SEG_PRE)
    hook = mf.segment(manifest, mf.SEG_HOOK)
    used_events: set[int] = set()
    maximum = max([item.get("score", 0) for item in pool] or [1.0])

    def role_for(item: dict) -> tuple[str, str]:
        role = item.get("role", "")
        if role and role in faces:
            return role, faces[role]
        return role, ""

    for index, item in enumerate(selected, start=1):
        role, ref = role_for(item)
        pre["shots"].append(_shot(index, mf.SEG_PRE, role, ref, item, events,
                                  used_events, subtitle_cfg, "原剧冲突", maximum))
    for index, item in enumerate(hook_candidates, start=1):
        role, ref = role_for(item)
        hook["shots"].append(_shot(index, mf.SEG_HOOK, role, ref, item, events,
                                   used_events, subtitle_cfg, "原剧悬念", maximum))

    minimum_pre = hook_cfg.get("min_pre_shots", 3)
    minimum_hook = hook_cfg.get("min_hook_shots", 2)
    max_open = hook_cfg.get("opening_max_sec", 3.0)
    opening_ok = bool(pre["shots"] and pre["shots"][0]["source_time"]["start"] is not None
                      and pre["shots"][0]["source_time"]["start"] < max_open)
    if not opening_ok:
        manifest["status"] = "blocked"
        manifest["hard_block"] = [f"首镜缺少首{max_open:g}秒内可溯源的冲突/悬念台词"]
    if len(pre["shots"]) < minimum_pre:
        manifest["hard_block"] = manifest.get("hard_block", []) + [
            f"前核钩子镜头不足 {minimum_pre} 个，禁止占位补齐"]
    if len(hook["shots"]) < minimum_hook:
        manifest["hard_block"] = manifest.get("hard_block", []) + [
            f"后钩子镜头不足 {minimum_hook} 个，禁止用前核片段伪装补齐"]
    if manifest.get("hard_block"):
        warns.extend(manifest["hard_block"])
        manifest["status"] = "blocked"
    else:
        manifest["status"] = "scripted"

    manifest["refs"] = faces
    manifest["transcript"] = {"event_count": len(events),
                               "ranked_candidate_count": len(pool),
                               "top_candidates": pool[:12]}
    manifest["planning"] = {
        "method": "rules_only",
        "llm_status": "disabled",
        "visual_analysis": "not_performed",
        "evidence_status": "verified" if events else "pending",
        "provider": None,
        "logical_calls": 0,
        "fallbacks": 0,
        "synopsis_only_candidates": [],
    }
    for shot in pre["shots"] + hook["shots"]:
        shot["evidence"] = {
            "dialogue": "asr_aligned" if shot["dialogue_source"] == "asr" else "pending",
            "action": "script_action_aligned" if shot["action"]["source_actions"] else "not_available",
            "scene": "script_scene" if shot.get("scene") else "not_available",
            "character": "script_role_mapping" if shot["role"] else "pending",
        }
        shot["camera_suggestion"] = {}
    manifest["planning"].update(planning_status)
    manifest["hook_qc"] = {
        "opening_max_sec": max_open,
        "opening_first_shot_start": pre["shots"][0]["source_time"]["start"]
        if pre["shots"] else None,
        "opening_within_limit": opening_ok,
        "pre_shot_count": len(pre["shots"]),
        "hook_shot_count": len(hook["shots"]),
        "ranked_by_conflict_intensity": True,
        "source_dialogue_only": True,
        "subtitle_lines_present": all(s["subtitle"] for s in pre["shots"] + hook["shots"]),
        "hard_block": manifest.get("hard_block", []),
    }
    manifest["warns"] = list(dict.fromkeys(warns))
    return manifest
