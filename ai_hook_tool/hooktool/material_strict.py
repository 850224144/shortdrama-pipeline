"""Strict, filename-only discovery for project mode; legacy scanning is unchanged."""
from __future__ import annotations

import os
from pathlib import Path
import re

from .source_identity import contained_path


def episode_number(filename: str) -> int | None:
    stem = Path(filename).stem
    for pattern in (r"第\s*(\d{1,3})\s*集",
                    r"(?:^|[\s_.-])EP?\s*(\d{1,3})(?=$|[\s_.-])",
                    r"^\s*(\d{1,3})(?=$|[\s_.-])"):
        match = re.search(pattern, stem, re.IGNORECASE)
        if match:
            number = int(match.group(1))
            return number if number > 0 else None
    return None


def discover(source_dir: str) -> dict:
    root = Path(source_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("SOURCE_NOT_DIRECTORY")
    videos, subtitles, auxiliary = [], [], []
    for base, dirs, files in os.walk(root, followlinks=False):
        # Junctions may resolve outside root on Windows. Do not traverse them.
        dirs[:] = sorted(d for d in dirs if not Path(base, d).is_symlink()
                         and Path(base, d).resolve().is_relative_to(root))
        for name in sorted(files):
            path = Path(base, name)
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            suffix = path.suffix.lower()
            row = {"path": str(path.resolve()), "episode_hint": episode_number(name)}
            if suffix == ".mp4":
                videos.append(row)
            elif suffix in (".srt", ".vtt") or (
                    suffix == ".txt" and re.search(r"transcript|字幕|转写", name, re.I)):
                subtitles.append(row)
            elif suffix in (".txt", ".png", ".jpg", ".jpeg", ".webp"):
                auxiliary.append({"path": str(path.resolve()), "status": "not_analyzed"})
    return {"source_dir": str(root), "videos": videos, "subtitles": subtitles,
            "auxiliary": auxiliary}


def select_sources(discovery: dict, *, episode=None, confirm_first_episode=False,
                   transcript=None, confirm_transcript=False, no_transcript=False) -> dict:
    if confirm_first_episode and not episode:
        raise ValueError("--confirm-first-episode 必须同时指定 --episode")
    if confirm_transcript and not transcript:
        raise ValueError("--confirm-transcript 必须同时指定 --transcript")
    if no_transcript and transcript:
        raise ValueError("--transcript 与 --no-transcript 互斥")
    issues = []
    result = {"episode": None, "transcript": None,
              "episode_selection": None, "transcript_selection": None,
              "issues": issues}
    root = discovery["source_dir"]
    if episode:
        if not confirm_first_episode:
            raise ValueError("显式选择视频须添加 --confirm-first-episode")
        selected = contained_path(root, episode)
        if selected.suffix.lower() != ".mp4":
            raise ValueError("UNSUPPORTED_VIDEO: 首集须为 MP4")
        result.update(episode=str(selected), episode_selection="user_confirmed",
                      episode_hint=episode_number(selected.name))
    else:
        first = [v for v in discovery["videos"] if v["episode_hint"] == 1]
        # Multiple directories containing videos may represent multiple dramas.
        parents = {str(Path(v["path"]).parent) for v in discovery["videos"]}
        if len(first) == 1 and len(parents) == 1:
            result.update(episode=first[0]["path"], episode_selection="unique_e01",
                          episode_hint=1)
        else:
            issues.append({"code": "EPISODE_CONFIRMATION_REQUIRED", "severity": "blocking",
                           "message": "缺少唯一首集、存在重复首集或多目录素材；请明确选择并确认首集"})
    if no_transcript:
        result["transcript_selection"] = "user_disabled"
    elif transcript:
        if not confirm_transcript:
            raise ValueError("显式选择字幕须添加 --confirm-transcript")
        selected = contained_path(root, transcript)
        if selected.suffix.lower() not in (".txt", ".srt", ".vtt"):
            raise ValueError("UNSUPPORTED_TRANSCRIPT")
        hint = episode_number(selected.name)
        # A known non-E01 subtitle is not silently relabelled as source evidence.
        if hint is not None and hint != 1:
            raise ValueError("TRANSCRIPT_EPISODE_MISMATCH: 字幕明确标记为非首集")
        result.update(transcript=str(selected), transcript_selection="user_confirmed")
    elif result["episode"]:
        candidates = [s for s in discovery["subtitles"] if s["episode_hint"] == 1
                      and Path(s["path"]).parent == Path(result["episode"]).parent]
        if len(candidates) == 1:
            result.update(transcript=candidates[0]["path"], transcript_selection="unique_e01")
        elif discovery["subtitles"]:
            issues.append({"code": "TRANSCRIPT_CONFIRMATION_REQUIRED", "severity": "blocking",
                           "message": "字幕归属不唯一或未标首集；请选择并确认，或显式 --no-transcript"})
        else:
            result["transcript_selection"] = "absent"
    return result
