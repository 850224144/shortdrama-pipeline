# -*- coding: utf-8 -*-
"""首集 txt 剧本台词候选解析。

口径（定稿 §4.3）：txt 不作剧情事实依据，仅作台词候选；剧情相同度判定仍以成片 ASR/画面为准。
动作行（△）、场景行、人物表行跳过；角色（情绪/标注）：台词 行解析为候选。
"""
import os
import re
import difflib

# 角色名 + 可选括号情绪/标注 + 可选 VO + 冒号 + 台词
_LINE = re.compile(
    r"^(?P<role>[^\s△（(：:]+?)(?P<vo>VO)?(?P<paren>（[^）]*）)?[：:](?P<line>.+)$"
)
_SKIP_PREFIX = ("△", "人物", "场景")


def parse_script_txt(path: str) -> list[dict]:
    """解析剧本 txt，返回 [{role, line, vo, source}]；文件缺失返回 []。"""
    entries: list[dict] = []
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError:
        return []

    # txt 仍不作为剧情事实来源；仅用于匹配首集 ASR 台词、补回角色归属。
    # 没有对应口播的剧本句不进入生成候选。
    pending_actions: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith("△"):
            pending_actions.append(s.lstrip("△ ").strip())
            continue
        if s.startswith(("人物", "场景")):
            continue
        # 场景编号行，如 "1-1日内..."
        if re.match(r"^\d+-\d+", s):
            continue
        m = _LINE.match(s)
        if not m:
            continue
        role = m.group("role").strip().removesuffix("VO").strip()
        line = m.group("line").strip()
        if not role or not line:
            continue
        entries.append({
            "role": role,
            "line": line,
            "vo": bool(m.group("vo")),
            "source": "txt",
            "action_context": pending_actions,
        })
        pending_actions = []
    return entries


_TS_TAG = re.compile(r"^\[[\d.\-]+\]\s*")


def _normalize_for_match(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace() and ch not in "，。？！、：；“”（）")


_TIMED_LINE = re.compile(r"^\[([\d.]+)-([\d.]+)\]\s*(.*)$")


def _timed_events(transcript_lines: list[str]) -> list[dict]:
    events = []
    for index, raw in enumerate(transcript_lines):
        match = _TIMED_LINE.match(raw.strip())
        if not match:
            continue
        events.append({"index": index, "start": float(match.group(1)),
                       "end": float(match.group(2)), "text": match.group(3).strip()})
    return events


def align_dialogues(transcript_lines: list[str], entries: list[dict]) -> list[dict]:
    """将剧本对白只作为 ASR 台词的说话人标签，并对齐到首集时间段。

    连续 ASR 短片段可合并匹配一个剧本句；不匹配的句子绝不进入计划。
    """
    events = _timed_events(transcript_lines)
    matches: list[dict] = []
    cursor = 0
    for entry_index, entry in enumerate(entries):
        target = _normalize_for_match(entry.get("line", ""))
        if len(target) < 2:
            continue
        best: tuple[float, int, int] | None = None
        scan_end = min(len(events), cursor + 48)
        for start_index in range(cursor, scan_end):
            parts: list[str] = []
            for end_index in range(start_index, min(len(events), start_index + 9)):
                event = events[end_index]
                if end_index > start_index and event["start"] - events[end_index - 1]["end"] > 1.5:
                    break
                parts.append(_normalize_for_match(event["text"]))
                elapsed = event["end"] - events[start_index]["start"]
                if elapsed > 10.0:
                    break
                observed = "".join(parts)
                similarity = difflib.SequenceMatcher(
                    None, target, observed, autojunk=False).ratio()
                if observed and (target in observed or observed in target):
                    similarity = max(similarity, min(0.98, 0.72 +
                                       min(len(observed), len(target)) /
                                       max(len(target), 1) * 0.26))
                if best is None or similarity > best[0]:
                    best = (similarity, start_index, end_index)
        if best is None or best[0] < 0.40:
            continue
        similarity, first, last = best
        matches.append({"entry_index": entry_index, "asr_match": round(similarity, 3),
                        "asr_start": events[first]["start"],
                        "asr_end": events[last]["end"],
                        "asr_event_indices": [e["index"] for e in events[first:last + 1]]})
        cursor = last + 1
    return matches


def pick_txt_candidates(entries: list[dict], conflict_words: list[str],
                         suspense_words: list[str], count: int = 6) -> list[dict]:
    """按冲突/悬念词命中排序取候选，返回 [{role, line, vo, source}]。"""
    def score(e: dict) -> int:
        s = 0
        if any(w in e["line"] for w in conflict_words):
            s += 2
        if any(w in e["line"] for w in suspense_words):
            s += 1
        return s

    ranked = sorted(entries, key=lambda e: (score(e), e.get("asr_match", 0)), reverse=True)
    return [e for e in ranked if score(e) > 0][:count] or ranked[:count]
