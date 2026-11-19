# -*- coding: utf-8 -*-
"""Optional evidence-constrained LLM suggestions for hook planning."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable


_ROLES = {"opening", "escalation", "cliffhanger", "exclude"}
_DETAIL_OPTIONS = {
    "framing": {"全景", "中景", "近景", "特写", "全景转中景", "中景转近景",
                "近景转特写", "中景转反应近景", "双人中景转反应特写"},
    "camera_movement": {"固定", "缓慢推进", "快速推进", "跟随移动", "横向移动",
                        "轻微手持", "摇镜", "拉远"},
    "composition": {"中心构图", "三分构图", "前景压迫式构图", "双人对峙构图",
                    "纵深构图", "字幕下方留白"},
    "expression": {"紧张", "震惊", "愤怒", "恐惧", "悲伤", "冷静", "犹豫",
                   "困惑", "喜悦", "克制"},
    "lighting": {"自然光", "冷色柔光", "暖色柔光", "侧光", "逆光", "顶光",
                 "低照度", "高反差"},
    "sound_and_pacing": {"保留原对白", "突出环境声", "对白后短暂停顿", "快速剪辑",
                          "节奏逐渐加快", "结尾悬念停顿", "音乐渐弱"},
}
_DETAIL_FIELDS = tuple(_DETAIL_OPTIONS)


def candidate_id(item: dict) -> str:
    if item.get("source_index") is not None:
        return f"asr:{item['source_index']}"
    event_ids = item.get("asr_event_indices", [])
    if event_ids:
        return "asr:" + str(event_ids[0])
    source = f"{item.get('start')}|{item.get('end')}|{item.get('text', '')}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _json_value(content: str):
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        content = fence.group(1).strip()
    return json.loads(content)


def _short_text(value, maximum: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:maximum] if value else None


def _validated_rows(content: str, valid_ids: set[str], fields: tuple[str, ...]) -> list[dict]:
    data = _json_value(content)
    if not isinstance(data, list):
        raise ValueError("response must be a JSON array")
    rows = []
    seen = set()
    for row in data:
        if not isinstance(row, dict):
            continue
        item_id = row.get("candidate_id")
        if item_id not in valid_ids or item_id in seen:
            continue
        clean = {"candidate_id": item_id}
        if "role_in_hook" in fields:
            role = row.get("role_in_hook")
            why = _short_text(row.get("why_it_hooks"))
            if role not in _ROLES or not why:
                continue
            clean.update(role_in_hook=role, why_it_hooks=why)
        else:
            for field in fields:
                value = row.get(field)
                if field in _DETAIL_OPTIONS and value not in _DETAIL_OPTIONS[field]:
                    continue
                value = _short_text(value, 160)
                if value:
                    clean[field] = value
        if len(clean) > 1:
            rows.append(clean)
            seen.add(item_id)
    return rows


def plan_candidates(candidates: list[dict], genre: str, synopsis: str,
                    chat_json: Callable[[str], str | None]) -> tuple[dict[str, dict], str]:
    eligible = candidates[:20]
    if not eligible:
        return {}, "no_candidates"
    by_id = {candidate_id(item): item for item in eligible}
    data = [{"candidate_id": key, "text": item["text"],
             "source": item.get("source", "asr"),
             "role": item.get("role", ""),
             "start": item.get("start"), "end": item.get("end")}
            for key, item in by_id.items()]
    prompt = f"""你是短剧钩子策划助手。只对给定的、已从 ASR 提取的候选台词做钩子功能分析。
题材标签仅作背景，不代表必须出现特定人物关系或事件。简介内容仅作长线背景，不是当前集台词或镜头证据。

题材：{genre or '未指定'}
简介背景（不可引用为当前集实录）：{synopsis or '无'}
ASR 候选 JSON：
{json.dumps(data, ensure_ascii=False)}

输出 JSON 数组，每项只能引用输入里的 candidate_id，并包含：
- candidate_id
- role_in_hook：opening / escalation / cliffhanger / exclude 之一
- why_it_hooks：不超过 120 字，说明该原句为何适合该位置

约束：不得改写/编造台词、角色、动作、场景或时间；不得输出时间码；素材不足时可返回空数组，不要凑数。只输出 JSON。"""
    try:
        content = chat_json(prompt)
    except Exception:
        return {}, "unavailable"
    if not content:
        return {}, "unavailable"
    try:
        rows = _validated_rows(content, set(by_id), ("role_in_hook", "why_it_hooks"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}, "invalid_output"
    if not rows:
        return {}, "no_suggestions"
    return {row["candidate_id"]: row for row in rows}, "used"


def suggest_camera(shots: list[dict], chat_json: Callable[[str], str | None]) -> tuple[dict[str, dict], str]:
    eligible = [shot for shot in shots if shot.get("candidate_id")]
    if not eligible:
        return {}, "no_shots"
    by_id = {shot["candidate_id"]: shot for shot in eligible}
    data = [{"candidate_id": key, "segment": shot.get("segment")}
            for key, shot in by_id.items()]
    options = json.dumps({key: sorted(values) for key, values in _DETAIL_OPTIONS.items()},
                         ensure_ascii=False)
    prompt = f"""你是短剧生成视频的分镜拍法助手。为以下已核实的原剧镜头提供简短的拍法建议。
输入 JSON：
{json.dumps(data, ensure_ascii=False)}

拍法字段允许值：{options}
输出 JSON 数组，每项包含 candidate_id 和零个或多个拍法字段。每个字段值必须逐字选择其允许值之一。
不得改写对白、补动作/人物/道具/情节，不得输出时间码。全片为 9:16 竖屏。
只输出 JSON；若没有合适值可省略该字段。"""
    try:
        content = chat_json(prompt)
    except Exception:
        return {}, "unavailable"
    if not content:
        return {}, "unavailable"
    try:
        rows = _validated_rows(content, set(by_id), _DETAIL_FIELDS)
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}, "invalid_output"
    return {row["candidate_id"]: row for row in rows}, "used"
