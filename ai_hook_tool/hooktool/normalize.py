# -*- coding: utf-8 -*-
"""名称归一、模糊去重、集号解析、短标题规范（复刻工坊纯函数，无外部依赖）。"""
import difflib
import re


def normalize_name(name: str) -> str:
    """标准化剧名：只保留中文/字母/数字。"""
    return re.sub(r"[^一-龥a-zA-Z0-9]", "", str(name or ""))


def is_fuzzy_duplicate(name1: str, name2: str, ratio: float = 0.85) -> bool:
    """模糊去重：SequenceMatcher ratio >= 阈值。"""
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    return difflib.SequenceMatcher(None, n1, n2).ratio() >= ratio


def parse_episode_number(filename: str):
    """解析文件名集数，兼容 '1.mp4' 与 '第1集.mp4'；无法解析返回 None。"""
    m = re.search(r"第(\d+)集", filename)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\.mp4", filename, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def sanitize_short_title(title: str, max_len: int = 16, min_len: int = 4) -> str:
    """去标点/空白 → 截断到 max_len；不足 min_len 返回空串。"""
    s = re.sub(
        r"[\s　，、。！？；：“”‘’—–…（）【】《》\[\]\(\)\{\}<>、,.;:!?\'\"|·~\-_]",
        "", str(title or ""),
    )
    s = s.strip()
    if not s:
        return ""
    return s[:max_len] if len(s) >= min_len else ""
