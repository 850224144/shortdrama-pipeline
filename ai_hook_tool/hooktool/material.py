# -*- coding: utf-8 -*-
"""素材目录扫描：剧集、定妆/剧照、封面、文本、已有转写。

剧情事实以首集成片为准；txt 仅作题材辅助（定稿 §4.3）。
"""
import os
import re

from . import config as cfg
from .normalize import parse_episode_number

COVER_KEYWORDS = ("-sph", "cover", "封面", "poster")
ROLE_SUFFIX = re.compile(r"[-_](反派|主角|女主|男主|配角|女二|男二)$", re.IGNORECASE)


def _iter_files(root: str):
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            yield os.path.join(dirpath, name)


def _is_cover(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in COVER_KEYWORDS)


def _role_name(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = ROLE_SUFFIX.sub("", stem)
    return stem.strip()


def scan_material(src_dir: str) -> dict:
    """扫描素材目录，返回素材清单与缺失/告警标记。

    返回 dict:
      drama_name, episodes[(num,path)], first_episode(num,path)|None,
      faces{role:path}, cover|None, texts[path], transcript|None,
      missing[], warnings[]
    """
    settings = cfg.settings()
    img_exts = tuple(settings["image_formats"])
    vid_exts = tuple(settings["video_formats"])

    by_ep = {}
    faces: dict[str, str] = {}
    cover = None
    texts: list[str] = []
    transcript = None
    warnings: list[str] = []
    missing: list[str] = []

    for f in _iter_files(src_dir):
        name = os.path.basename(f)
        low = name.lower()
        ext = os.path.splitext(name)[1].lower()

        if ext in vid_exts:
            if name.endswith(".downloading"):
                continue
            ep = parse_episode_number(name)
            if ep is not None:
                cur = by_ep.get(ep)
                if cur is None or f.count(os.sep) < cur[1].count(os.sep):
                    by_ep[ep] = (ep, f)
            continue

        if ext in img_exts:
            if _is_cover(name):
                cover = f
            else:
                role = _role_name(f)
                if role:
                    faces[role] = f
            continue

        if ext == ".txt":
            if re.search(r"transcript|转写|字幕", low):
                transcript = f
            else:
                texts.append(f)
            continue

        if ext in (".srt", ".vtt"):
            transcript = f

    episodes = [by_ep[k] for k in sorted(by_ep)]
    first_episode = episodes[0] if episodes else None

    drama_name = os.path.basename(os.path.normpath(src_dir))

    if not episodes:
        missing.append("无可用 mp4 剧集文件")
    if not faces:
        warnings.append("未发现定妆/剧照参照图，锁脸参照待补")
    if not texts and not transcript:
        warnings.append("无剧本/简介文本，题材判定可能为 PENDING")

    return {
        "drama_name": drama_name,
        "episodes": episodes,
        "first_episode": first_episode,
        "faces": faces,
        "cover": cover,
        "texts": sorted(texts),
        "transcript": transcript,
        "missing": missing,
        "warnings": warnings,
    }


def read_texts(paths: list[str]) -> str:
    """拼接文本文件内容（仅题材辅助）。"""
    chunks = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                chunks.append(f.read())
        except OSError:
            continue
    return "\n".join(chunks)
