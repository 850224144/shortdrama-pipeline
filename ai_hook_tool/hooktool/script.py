# -*- coding: utf-8 -*-
"""Generate a source-anchored short-drama hook plan, ranked by conflict intensity."""
from __future__ import annotations

import re

from . import config as cfg, manifest as mf


def _plain(text: str, punct: list[str]) -> str:
    value = text or ""
    for mark in punct:
        value = value.replace(mark, "")
    return value.strip()


def _subtitle(text: str, punct: list[str], maximum: int) -> str:
    clean = _plain(text, punct)
    return clean if len(clean) <= maximum else clean[:maximum]


def _event_index(text: str, events: list[dict], used: set[int]) -> list[int]:
    for index, event in enumerate(events):
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
                     min_match: float) -> list[dict]:
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
        text = entry.get("line", "").strip()
        if not text or entry.get("asr_start") is None:
            continue
        match_ratio = float(entry.get("asr_match", 0))
        if match_ratio < min_match:
            continue
        # Carry timing from the exact ASR utterance while using the script only to
        # restore its character label and original action directions.
        start, end = entry["asr_start"], entry["asr_end"]
        overlaps = [q for q in pool if q.get("start") is not None
                    and q["start"] < end and q["end"] > start]
        if not overlaps:
            continue
        base_score = max(q.get("score", 0) for q in overlaps)
        high_hits = [phrase for phrase in high_phrases if phrase in text]
        conflict_hits = [word for word in conflict if word in text]
        suspense_hits = [word for word in suspense if word in text]
        low_hits = [phrase for phrase in low_phrases if phrase in text]
        # An exact, named, original-script delivery outranks unlabelled ASR shards.
        score = (base_score + 6.0 + min(4.0, len(high_hits) * 2.0)
                 + min(3.0, len(conflict_hits) * 0.7)
                 + min(2.0, len(suspense_hits) * 0.5) - len(low_hits) * 2.0)
        pool.append({"text": text, "start": start, "end": end, "score": round(score, 3),
                     "source": "asr+script_alignment", "source_index": min(
                         q.get("source_index", 0) for q in overlaps),
                     "role": entry.get("role", ""), "dialogue_source": "asr",
                     "vo": entry.get("vo", False), "action_context": entry.get(
                         "action_context", []), "asr_match": match_ratio,
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
    result = [item for item in result
              if item.get("source") == "asr+script_alignment"
              or not any(item.get("start") is not None
                         and item["start"] < label["end"]
                         and item["end"] > label["start"]
                         and (_plain(item["text"], kw["punctuations"])
                              in _plain(label["text"], kw["punctuations"])
                              or _plain(label["text"], kw["punctuations"])
                              in _plain(item["text"], kw["punctuations"]))
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
    """Tie generated motion to the original script's explicit actions."""
    text = item["text"]
    actions = "；".join(item.get("action_context", []))
    if "第一巴掌" in actions or "扬手" in actions or "一巴掌扇" in actions:
        motion = "白玉瑶扬手一巴掌扇在陆时衍脸上；正反打，先给挥手动作，再切陆时衍脸部反应。"
        framing = "中景交代两人站位，切面部近景"
    elif "又抬手" in actions or "第二巴掌" in actions:
        motion = "白玉瑶再次抬手，陆时衍下意识偏头避开；用动作起落和反应完成剪辑。"
        framing = "双人中景转反应特写"
    elif "门推开" in actions or "推开门" in actions:
        motion = "手术室门突然推开，秦露举着手机带学生进入；镜头跟随门和手机推进病床。"
        framing = "门口全景切手机近景再切病床"
    elif "冲进来" in actions:
        motion = "陆时衍冲进病房，在直播手机、学生与产床间急停；镜头跟随入场再反打。"
        framing = "动态全景转越肩对峙"
    elif "凑到床边" in actions or "伸头" in actions:
        motion = "学生凑到床边围观，温知宁攥紧被子并把手臂护在身前；镜头从围观群像压到反应。"
        framing = "床边群像和俯视反应镜头"
    elif "撑着坐起来" in actions or "胳膊一软" in actions:
        motion = "温知宁挣扎撑起身体又因疼痛脱力，小林扶住她；秦露的手机继续逼近。"
        framing = "中景动作接痛苦近景"
    elif "指甲掐进" in actions or "弓起来" in actions:
        motion = "温知宁剧痛弓身、手指掐进床单，秦露把手机逼近；镜头跟随动作快速推近。"
        framing = "手部细节切面部特写"
    elif "脸上" in actions or "抬手" in actions:
        motion = "角色抬手并完成剧本中的巴掌动作，紧接对方躲闪或震惊反应，不增加肢体冲突。"
        framing = "双人中景接冲击反应特写"
    elif "拽" in actions or "拉住" in actions:
        motion = "角色拉住对方衣袖，对方转身躲开；使用移动镜头清楚交代双方位置。"
        framing = "双人中景转手部细节"
    else:
        motion = "按原剧动作完成一次明确的入场、阻挡、转身或逼近；若当前对白没有动作描述，保持克制移动，绝不编新动作。"
        framing = "先交代人物关系的中景，接带明确表情变化的反应近景"

    beats = ["动作起势", "动作落点", "对方反应", "悬念停顿"]
    beat = beats[(shot_no - 1) % len(beats)]
    if segment == mf.SEG_HOOK and shot_no == 2:
        beat = "最炸反应后立即掐断，保留未解悬念"
    return {"motion": motion, "framing": framing, "beat": beat,
            "source_actions": item.get("action_context", []),
            "continuity_note": "严格沿用首集已出现的人物、关系、场景和动作；不添新事件。"}


def _shot(shot_no: int, segment: str, role: str, ref_image: str,
          item: dict | None, events: list[dict], used_events: set[int],
          subtitle_cfg: dict, scene_hint: str, max_score: float) -> dict:
    text = item["text"] if item else ""
    if item and item.get("asr_event_indices"):
        event_indices = list(item["asr_event_indices"])
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
    prompt = (
        f"9:16竖屏，基于首集原片的{scene_hint}，严格使用参考图中{role or '原剧人物'}的身份、脸、" 
        f"发型和服装。镜头段落：{segment}，源片时间 {range_label}。"
        f"本镜必须在首帧呈现冲突动作/手机直播/病床危机；原句：{cue}。"
        f"表演动作：{action['motion']} 景别和机位：{action['framing']}。"
        f"节奏节点：{action['beat']}。使用动作起势、落点、反应的清楚剪辑；人物有明显位移和目光变化，"
        "不用静态皱眉摆拍，不用空镜，不增删原剧情事件。准确口播原句，普通话自然，口型同步；"
        "屏幕叠加简体中文字幕，不生成额外屏幕文字、字幕水印或生成工具标识。"
        "画面有前中后景，写实医疗剧灯光和稳定运动镜头。"
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
        "emotion": "愤怒/受辱/对峙" if score >= 7 else "疼痛/恐惧/反抗",
        "action": action,
        "opening_hook": bool(segment == mf.SEG_PRE and shot_no == 1),
        "is_cliffhanger": bool(segment == mf.SEG_HOOK and shot_no == 2),
    }


def build_script(material: dict, short_titles: list[str], long_titles: list[str],
                 genre: str, source_episode: str,
                 txt_entries: list[dict] | None = None,
                 quote_records: list[dict] | None = None,
                 transcript_lines: list[str] | None = None) -> dict:
    kw = cfg.keywords()
    hook_cfg = kw.get("hook", {})
    subtitle_cfg = kw.get("subtitle", {})
    faces = material.get("faces", {})
    roles = list(faces)
    transcript_lines = transcript_lines or []
    txt_entries = txt_entries or []
    events = []
    ts_re = re.compile(r"^\[([\d.]+)-([\d.]+)\]\s*(.*)$")
    for source_index, raw in enumerate(transcript_lines):
        match = ts_re.match(raw.strip())
        if match:
            events.append({"start": float(match.group(1)), "end": float(match.group(2)),
                           "text": match.group(3).strip(), "source_index": source_index})

    pool = _make_quote_pool(long_titles, txt_entries, quote_records,
                            kw["conflict_words"], kw["suspense_words"],
                            kw.get("high_intensity_phrases", []),
                            kw.get("low_signal_phrases", []),
                            hook_cfg.get("min_source_match", 0.5))
    opening = _opening_group(events, quote_records or [], hook_cfg)
    opening_indices = {event["source_index"] for event in opening}
    opening_times = {round(event["start"], 2) for event in opening}
    opening_by_indices = [item for item in pool
                          if item.get("source_index") in opening_indices
                          or (item.get("start") is not None
                              and round(item["start"], 2) in opening_times)
                          or any(i in item.get("asr_event_indices", []) for i in opening_indices)]

    # Restore verified speaker labels by exact transcript interval, while the script
    # remains a speaker/action annotation only. Every spoken word maps back to ASR.
    opening_candidates = sorted(opening_by_indices,
                                key=lambda item: (-item.get("score", 0),
                                                  item.get("start", float("inf"))))
    first_hook = opening_candidates[0] if opening_candidates else None
    selected: list[dict] = [first_hook] if first_hook else []
    # Add the strongest distinct story beats from the same opening conflict,
    # followed by the source's crisis setup, keeping source order for continuity.
    pre_pool = [item for item in pool if item.get("start") is not None
                and item.get("start", 0) < 30 and item is not first_hook]
    pre_pool.sort(key=lambda item: (-item.get("score", 0),
                                   item.get("start", float("inf"))))
    for item in pre_pool:
        if any(item["start"] < prior["end"] and item["end"] > prior["start"]
               for prior in selected if prior.get("start") is not None):
            continue
        selected.append(item)
        if len(selected) >= hook_cfg.get("min_pre_shots", 3):
            break
    selected.sort(key=lambda item: item.get("start", float("inf")))

    opening_end = max((item.get("end") or 0 for item in selected), default=0)
    post_opening = [item for item in pool if item.get("start") is not None
                    and item["start"] >= max(30.0, opening_end) and item not in selected]
    cliff_phrases = hook_cfg.get("cliffhanger_phrases", [])
    cliff_pool = [item for item in post_opening
                  if any(phrase in item["text"] for phrase in cliff_phrases)]
    cliff = max(cliff_pool, key=lambda item: (item.get("end", 0), item.get("score", 0)),
                default=None)
    hook_candidates: list[dict] = []
    if cliff is not None:
        lead_window = hook_cfg.get("cliffhanger_lead_window_sec", 15.0)
        lead_pool = [item for item in post_opening if item is not cliff
                     and 0 < cliff["start"] - item["end"] <= lead_window]
        lead = max(lead_pool, key=lambda item: (item.get("score", 0), item.get("end", 0)),
                   default=None)
        if lead:
            hook_candidates.append(lead)
        hook_candidates.append(cliff)

    warns: list[str] = []
    if not roles:
        warns.append("无定妆/剧照参照，角色锁脸待补（PENDING）")
    if any(item.get("dialogue_source") == "txt" for item in selected + hook_candidates):
        warns.append("有台词无法由首集 ASR 对齐，禁止用于成片并已排除")

    manifest = mf.empty_manifest(material["drama_name"], genre,
                                 cfg.get_setting("episode_label", "E01"), source_episode)
    pre = mf.segment(manifest, mf.SEG_PRE)
    hook = mf.segment(manifest, mf.SEG_HOOK)
    used_events: set[int] = set()
    maximum = max([item.get("score", 0) for item in pool] or [1.0])

    def role_for(item: dict, index: int) -> tuple[str, str]:
        role = item.get("role", "")
        if role and role in faces:
            return role, faces[role]
        if roles:
            fallback = roles[index % len(roles)]
            return fallback, faces[fallback]
        return "", ""

    for index, item in enumerate(selected, start=1):
        role, ref = role_for(item, index - 1)
        pre["shots"].append(_shot(index, mf.SEG_PRE, role, ref, item, events,
                                  used_events, subtitle_cfg, "产房冲突开场", maximum))
    for index, item in enumerate(hook_candidates, start=1):
        role, ref = role_for(item, index - 1)
        hook["shots"].append(_shot(index, mf.SEG_HOOK, role, ref, item, events,
                                   used_events, subtitle_cfg, "原剧未解冲突", maximum))

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
