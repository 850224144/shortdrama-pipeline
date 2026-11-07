# -*- coding: utf-8 -*-
"""槽位齐备校验：收集 04_ai_generated 两段片段，校验存在性/时长/分辨率/音轨。

缺片段进硬阻断清单；时长偏差、分辨率不符、无音轨记软告警/PENDING，不中断流程。
"""
import os
import re

from . import config as cfg, manifest as mf, paths, probe

SEG_DIR = {mf.SEG_PRE: paths.DIR_AI_PRE, mf.SEG_HOOK: paths.DIR_AI_HOOK}
_SHOT_PREFIX = re.compile(r"(?:^|[_-])(\d+)\.mp4", re.IGNORECASE)


def _clips_in(dir_path: str) -> list[str]:
    if not os.path.isdir(dir_path):
        return []
    exts = tuple(cfg.settings()["video_formats"])
    files = [
        os.path.join(dir_path, n) for n in os.listdir(dir_path)
        if n.lower().endswith(exts) and not n.endswith(".downloading")
    ]
    return sorted(files)


def _shot_index(filename: str, fallback: int) -> int:
    m = _SHOT_PREFIX.search(os.path.basename(filename))
    return int(m.group(1)) if m else fallback


def collect(manifest: dict, seg_dirs: dict[str, str] | None = None,
            flat_dir: str | None = None) -> dict:
    settings = cfg.settings()
    seg_sec = settings["segment_sec"]
    res = settings["resolution"]
    duration_tol = 3.0
    seg_dirs = seg_dirs or SEG_DIR
    flat_dir = flat_dir if flat_dir is not None else paths.DIR_SEGMENTS

    # 槽位目录（前核/后钩子）+ 扁平 segments 目录；扁平目录片段归前核段。
    sources: list[tuple[str, str, bool]] = [
        (name, d, False) for name, d in seg_dirs.items()
    ]
    sources.append((mf.SEG_PRE, flat_dir, True))

    clips: list[dict] = []
    missing: list[str] = []
    warns: list[str] = []
    pending: list[str] = []
    seen: set[str] = set()
    hard_block: list[str] = []
    strict_slots = bool(manifest.get("hook_qc") or manifest.get("hard_block"))
    required = {
        mf.SEG_PRE: max(1, int(cfg.keywords().get("hook", {}).get("min_pre_shots", 3))),
        mf.SEG_HOOK: max(1, int(cfg.keywords().get("hook", {}).get("min_hook_shots", 2))),
    }

    for seg_name, seg_dir, is_flat in sources:
        files = _clips_in(seg_dir)

        if not files:
            if not is_flat:
                if strict_slots:
                    hard_block.append(f"{seg_name} 必需槽位缺失：后钩子/前核未齐，禁止拼接")
                else:
                    pending.append(f"{seg_name} 槽位无片段（可由扁平 segments 目录补齐）")
            continue

        if strict_slots and not is_flat and len(files) < required.get(seg_name, 1):
            hard_block.append(
                f"{seg_name} 片段只有 {len(files)} 个，少于必需的 {required.get(seg_name, 1)} 个")

        for idx, f in enumerate(files, start=1):
            if f in seen:
                continue
            seen.add(f)
            info = probe.probe_media(f)
            clip = {
                "segment": seg_name,
                "path": f,
                "duration": info["duration"],
                "width": info["width"],
                "height": info["height"],
                "has_audio": info["has_audio"],
                "shot": _shot_index(f, idx),
            }
            if info["error"]:
                warns.append(f"{os.path.basename(f)} 探测异常：{info['error']}")
            clips.append(clip)

            if info["duration"] is not None and abs(info["duration"] - seg_sec) > duration_tol:
                warns.append(
                    f"{seg_name} 片段时长 {info['duration']:.1f}s，偏离目标 {seg_sec}s")
            if info["width"] and info["height"] and (
                info["width"] != res["width"] or info["height"] != res["height"]
            ):
                warns.append(
                    f"{os.path.basename(f)} 分辨率 {info['width']}x{info['height']}，"
                    f"非 {res['width']}x{res['height']}，拼接时统一缩放")
            if not info["has_audio"]:
                pending.append(f"{os.path.basename(f)} 无音轨，音轨兜底选型待实测")

    if not clips:
        missing.append("所有生成槽位均无片段（前核/后钩子/segments）")
    if strict_slots and not any(c["segment"] == mf.SEG_HOOK for c in clips):
        reason = "后钩子槽位没有实际片段；禁止用前核素材拼出≥30秒假钩子"
        if reason not in hard_block:
            hard_block.append(reason)
    if strict_slots and len({c["path"] for c in clips
                             if c["segment"] == mf.SEG_HOOK}) < required[mf.SEG_HOOK]:
        reason = f"后钩子素材不足 {required[mf.SEG_HOOK]} 个独立片段"
        if reason not in hard_block:
            hard_block.append(reason)

    manifest["clips"] = clips
    manifest["hard_block"] = list(dict.fromkeys(
        list(manifest.get("hard_block", [])) + hard_block))
    manifest["hook_qc"] = manifest.get("hook_qc", {})
    manifest["hook_qc"]["collect_hard_block"] = list(dict.fromkeys(hard_block))
    manifest["hook_qc"]["pre_clip_count"] = sum(
        c["segment"] == mf.SEG_PRE for c in clips)
    manifest["hook_qc"]["hook_clip_count"] = sum(
        c["segment"] == mf.SEG_HOOK for c in clips)
    manifest["status"] = "collected" if clips else manifest["status"]
    return {"clips": clips, "missing": missing, "warns": warns, "pending": pending,
            "hard_block": list(dict.fromkeys(hard_block))}
