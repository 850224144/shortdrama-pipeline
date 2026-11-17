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

# 字幕/转写文件名的集号线索（parse_episode_number 只认「第N集」「N.mp4」，
# 字幕常见命名是 E01 / EP01 / 01.srt，这里补齐；三条都要求紧邻分隔或文件名首尾，
# 避免把 1080p、2024 这类数字误判成集号）
SUB_EP_PATTERNS = (
    re.compile(r"第\s*(\d{1,3})\s*集"),
    re.compile(r"(?i)\bE(?:P)?\s*0*(\d{1,3})\b"),
    re.compile(r"(?i)^\s*0*(\d{1,3})\s*(?:[._\-]|$)"),
)


def _subtitle_episode(filename: str):
    """从字幕/转写文件名解析集号；无可靠线索返回 None。"""
    ep = parse_episode_number(filename)
    if ep is not None:
        return ep
    stem = os.path.splitext(filename)[0]
    for pat in SUB_EP_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            continue
    return None


def _select_transcript(subs, first_episode):
    """按首集绑定字幕/转写文件，返回 (path|None, warnings)。

    subs 为 [(ep|None, path)]，按扫描顺序。判定顺序：
      1. 集号等于首集集号 → 采用（多份取路径排序第一，并告警）；
      2. 无首集可比（素材里没有 mp4）→ 仅当只有一份时才采用并告警；
      3. 只有一份且未标集号 → 无冲突证据，采用并告警；
      4. 其余（集号与首集不符 / 多份且无从判定）→ 一律不采用并告警。
    绝不在「无法证明是首集」时静默把某份字幕当剧情事实（定稿 §4.3：剧情事实
    以首集成片为准）。
    """
    if not subs:
        return None, []
    names = [os.path.basename(p) for _, p in subs]

    if first_episode is None:
        if len(subs) == 1:
            return subs[0][1], [f"未识别到首集，按唯一字幕/转写采用：{names[0]}"]
        return None, ["未识别到首集且存在多份字幕/转写，均不采用：" + "、".join(names)]

    first_num = first_episode[0]
    matched = sorted(p for ep, p in subs if ep == first_num)
    if matched:
        if len(matched) > 1:
            picked = os.path.basename(matched[0])
            return matched[0], [f"首集字幕/转写有多份，已取 {picked}："
                                + "、".join(os.path.basename(p) for p in matched)]
        return matched[0], []

    if len(subs) == 1 and subs[0][0] is None:
        return subs[0][1], [f"字幕/转写未标集号，作为唯一文件采用：{names[0]}"]

    return None, [f"字幕/转写集号与首集 E{first_num} 不符或无法判定，已忽略："
                  + "、".join(names)]


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
    subs: list[tuple] = []
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
                subs.append((_subtitle_episode(name), f))
            else:
                texts.append(f)
            continue

        if ext in (".srt", ".vtt"):
            subs.append((_subtitle_episode(name), f))

    episodes = [by_ep[k] for k in sorted(by_ep)]
    first_episode = episodes[0] if episodes else None

    # 字幕/转写必须绑定首集（此前任何 .srt/.vtt 都无条件覆盖 transcript，
    # 多集字幕时可能取到非首集，情节事实就错源）
    transcript, sub_warnings = _select_transcript(subs, first_episode)
    warnings.extend(sub_warnings)

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
