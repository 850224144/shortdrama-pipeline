# -*- coding: utf-8 -*-
"""冲突/悬念词爆点台词提取（复刻工坊 _extract_quotes 纯函数）。"""
import re

from . import config as cfg

_TS_PREFIX = re.compile(r"^\[[\d.\-]+\]\s*(.+)$")


def _clean_line(line: str) -> str:
    line = line.strip()
    m = _TS_PREFIX.match(line)
    if m:
        line = m.group(1).strip()
    return line


def extract_quotes(lines: list[str], count: int = 10) -> tuple[list[str], list[str]]:
    """提取排序后的原剧爆点台词；tuple 接口保留兼容，排名见 rank_quotes()."""
    ranked = rank_quotes(lines, count=count)
    return ([item["short_title"] for item in ranked],
            [item["text"] for item in ranked])


def rank_quotes(lines: list[str], count: int = 10) -> list[dict]:
    """按高烈度动作/金句、冲突词权重与时间顺序排序候选。

    单行 ASR 片段用于保留时间戳和后续字幕对齐；4 字钩子也纳入候选。
    """
    kw = cfg.keywords()
    conflict_words = tuple(kw["conflict_words"])
    suspense_words = tuple(kw["suspense_words"])
    high_phrases = tuple(kw.get("high_intensity_phrases", ()))
    low_phrases = tuple(kw.get("low_signal_phrases", ()))
    punct = kw["punctuations"]

    items: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(lines):
        raw = raw.strip()
        match = _TS_PREFIX.match(raw)
        line = _clean_line(raw)
        if not line or not (2 <= len(line) <= 120):
            continue
        key = "".join(line.split())
        if key in seen:
            continue
        seen.add(key)

        high_hits = [phrase for phrase in high_phrases if phrase in line]
        conflict_hits = [word for word in conflict_words if word in line]
        suspense_hits = [word for word in suspense_words if word in line]
        low_hits = [phrase for phrase in low_phrases if phrase in line]
        if not (high_hits or conflict_hits or suspense_hits):
            continue

        score = (min(12.0, len(high_hits) * 6.0)
                 + min(8.0, len(conflict_hits) * 1.7)
                 + min(5.0, len(suspense_hits) * 1.5)
                 - len(low_hits) * 5.0)
        if match:
            start, end = float(match.group(1)), float(match.group(2))
            if end <= kw.get("hook", {}).get("opening_max_sec", 3.0):
                score += 3.0
        else:
            start = end = None
        short = line
        for p in punct:
            short = short.replace(p, "")
        items.append({"text": line, "short_title": short[:16], "start": start,
                      "end": end, "score": round(score, 3), "source": "asr",
                      "source_index": index, "high_hits": high_hits,
                      "conflict_hits": conflict_hits, "suspense_hits": suspense_hits,
                      "low_signal_hits": low_hits})

    items.sort(key=lambda item: (-item["score"],
                                 item["start"] if item["start"] is not None else float("inf"),
                                 item["source_index"]))
    return items[:count]
