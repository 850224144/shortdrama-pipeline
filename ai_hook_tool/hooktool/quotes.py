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
    """从台词行提取短标题(≤16字无标点)与长标题。

    返回 (short_titles, long_titles)；冲突/悬念词命中优先，无命中时取中段兜底。
    """
    kw = cfg.keywords()
    conflict_words = kw["conflict_words"]
    suspense_words = kw["suspense_words"]
    punct = kw["punctuations"]

    cleaned = [_clean_line(x) for x in lines]
    cleaned = [x for x in cleaned if x]

    short_titles: list[str] = []
    long_titles: list[str] = []

    for line in cleaned:
        if not (5 <= len(line) <= 80):
            continue
        has_conflict = any(w in line for w in conflict_words)
        has_suspense = any(w in line for w in suspense_words)
        if has_conflict or has_suspense:
            long_titles.append(line)
            short = line[:16]
            for p in punct:
                short = short.replace(p, "")
            short_titles.append(short)

    short_titles = list(dict.fromkeys(short_titles))[:count]
    long_titles = list(dict.fromkeys(long_titles))[:count]

    if not short_titles and not long_titles and cleaned:
        start = len(cleaned) // 2
        pool = cleaned[start:] or cleaned
        for line in pool[:count]:
            long_titles.append(line)
            short = line[:16]
            for p in punct:
                short = short.replace(p, "")
            short_titles.append(short)

    return short_titles, long_titles
