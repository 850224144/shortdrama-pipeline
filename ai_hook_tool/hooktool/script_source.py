# -*- coding: utf-8 -*-
"""首集 txt 剧本台词候选解析。

口径（定稿 §4.3）：txt 不作剧情事实依据，仅作台词候选；剧情相同度判定仍以成片 ASR/画面为准。
动作行（△）、场景行、人物表行跳过；角色（情绪/标注）：台词 行解析为候选。
"""
import os
import re

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

    for raw in lines:
        s = raw.strip()
        if not s or s.startswith(_SKIP_PREFIX):
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
        })
    return entries


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

    ranked = sorted(entries, key=score, reverse=True)
    return [e for e in ranked if score(e) > 0][:count] or ranked[:count]
