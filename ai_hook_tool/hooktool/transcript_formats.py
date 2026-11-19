"""Pure subtitle parsers producing evidence, not verified story facts."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re

from .source_identity import json_hash

PARSER_VERSION = "1"


def _milliseconds(value: str) -> int:
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) not in (1, 2, 3):
        raise ValueError("INVALID_TIMESTAMP")
    try:
        values = [Decimal(p) for p in parts]
        if any(not v.is_finite() or v < 0 for v in values):
            raise ValueError("INVALID_TIMESTAMP")
        if len(values) > 1 and (values[-1] >= 60 or
                                (len(values) == 3 and values[-2] >= 60)):
            raise ValueError("INVALID_TIMESTAMP")
        seconds = sum(v * (60 ** i) for i, v in enumerate(reversed(values)))
        return int((seconds * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError("INVALID_TIMESTAMP") from exc


def validate_events(events: list, duration_ms: int, source_asset_id: str) -> list[dict]:
    if not isinstance(events, list) or not events:
        raise ValueError("EMPTY_TRANSCRIPT: 无有效带时间码对白")
    issues, ids = [], set()
    previous_start, previous_end = -1, -1
    for row in events:
        start, end = row.get("start_ms"), row.get("end_ms")
        if (type(start) is not int or type(end) is not int or
                start < 0 or end <= start or end > duration_ms):
            raise ValueError("INVALID_TIME_RANGE: 时间码不合法或超出首集时长")
        if row.get("source_asset_id") != source_asset_id:
            raise ValueError("EVIDENCE_SOURCE_MISMATCH")
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError("EMPTY_CUE")
        if not row.get("utterance_id") or row["utterance_id"] in ids:
            raise ValueError("DUPLICATE_EVIDENCE_ID")
        ids.add(row["utterance_id"])
        if start < previous_start:
            raise ValueError("UNORDERED_CUES: 字幕时间乱序，需人工检查")
        if start < previous_end:
            issues.append({"code": "OVERLAPPING_CUES", "severity": "warning",
                           "utterance_id": row["utterance_id"],
                           "message": "对白时间重叠，保留原顺序，未自动合并说话人"})
        previous_start, previous_end = start, max(previous_end, end)
    return issues


def parse_transcript(text: str, extension: str, source_asset_id: str,
                     transcript_asset_id: str, duration_ms: int,
                     provenance: str = "subtitle") -> dict:
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("INVALID_DURATION")
    if "�" in text or "\x00" in text:
        raise ValueError("INVALID_TEXT_ENCODING")
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    rows = []
    if extension.lower() == ".txt":
        for index, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            match = re.fullmatch(r"\s*\[([^\]]+?)-([^\]]+?)\]\s*(.+)", line)
            if not match:
                raise ValueError(f"UNTIMED_TRANSCRIPT: 第{index + 1}行缺少可解析时间码")
            rows.append((index, None, _milliseconds(match[1]), _milliseconds(match[2]), match[3]))
    elif extension.lower() in (".srt", ".vtt"):
        blocks = re.split(r"\n[ \t]*\n", text.strip())
        for index, block in enumerate(blocks):
            lines = block.strip().splitlines()
            if not lines:
                continue
            if extension.lower() == ".vtt" and (
                    lines[0].startswith("WEBVTT") or
                    re.match(r"^(NOTE(?:\s|$)|STYLE$|REGION$)", lines[0])):
                continue
            timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
            if timing not in (0, 1):
                raise ValueError(f"INVALID_CUE: 第{index + 1}块缺少合法时间码")
            match = re.fullmatch(r"\s*(\S+)\s+-->\s+(\S+)(?:\s+.*)?", lines[timing])
            if not match:
                raise ValueError("INVALID_CUE_TIMESTAMP")
            caption = "\n".join(lines[timing + 1:]).strip()
            rows.append((index, lines[0] if timing == 1 else None,
                         _milliseconds(match[1]), _milliseconds(match[2]), caption))
    else:
        raise ValueError("UNSUPPORTED_TRANSCRIPT_FORMAT")
    events = []
    for index, cue_id, start, end, caption in rows:
        ident = json_hash([source_asset_id, transcript_asset_id, index, start, end, caption])
        events.append({"utterance_id": "utt_" + ident[:24], "source_index": index,
                       "cue_id": cue_id, "start_ms": start, "end_ms": end,
                       "text": caption, "source_asset_id": source_asset_id,
                       "transcript_asset_id": transcript_asset_id,
                       "provenance": provenance, "verification_status": "extracted",
                       "confidence": None})
    # No implicit tolerance or clipping: out-of-range cues require correction.
    issues = validate_events(events, duration_ms, source_asset_id)
    return {"schema_version": 1, "parser_version": PARSER_VERSION,
            "utterances": events, "issues": issues}
