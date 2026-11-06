# -*- coding: utf-8 -*-
"""题材关键词判定：只做记录归类，不做账号匹配（定稿 D17）。"""
from . import config as cfg


def detect_genre(text: str) -> dict:
    """返回 {genre, hits, pending}。无命中记 pending=True，不得默认 PASS。"""
    genre_cfg = cfg.genres()
    table = genre_cfg.get("genres", {})
    text = text or ""
    scores: dict[str, int] = {}
    hits: dict[str, list[str]] = {}

    for genre, words in table.items():
        matched = [w for w in words if w and w in text]
        if matched:
            scores[genre] = len(matched)
            hits[genre] = matched

    if not scores:
        return {"genre": "", "hits": {}, "pending": True}

    genre = max(scores, key=lambda g: scores[g])
    return {"genre": genre, "hits": hits, "pending": False}
