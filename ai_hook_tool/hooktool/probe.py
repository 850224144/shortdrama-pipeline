# -*- coding: utf-8 -*-
"""ffprobe 探测封装：时长、分辨率、音/视频流有无。"""
import json
import subprocess

from . import config as cfg


def probe_media(path: str) -> dict:
    """返回 {duration, width, height, has_video, has_audio, error}。

    探测失败不抛断，error 填原因，由上层按 PENDING/缺失处理。
    """
    ffprobe = cfg.ffprobe_bin()
    result = {"duration": None, "width": None, "height": None,
               "has_video": False, "has_audio": False, "error": None}
    if not ffprobe:
        result["error"] = "未配置 ffprobe 路径"
        return result
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            result["error"] = proc.stderr.strip()[:200]
            return result
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        for st in streams:
            if st.get("codec_type") == "video":
                result["has_video"] = True
                result["width"] = st.get("width")
                result["height"] = st.get("height")
            elif st.get("codec_type") == "audio":
                result["has_audio"] = True
        fmt = data.get("format", {})
        dur = fmt.get("duration")
        result["duration"] = float(dur) if dur is not None else None
    except Exception as exc:
        result["error"] = str(exc)[:200]
    return result
