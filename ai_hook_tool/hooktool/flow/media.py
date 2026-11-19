"""FFmpeg 渲染适配层：只做机械执行，不做事实判断。

预览与成片共用同一套区间裁剪逻辑；是否可用由上层审批与质检决定。
失败必须显式返回，不能用静音轨道或空镜头掩盖缺失。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import config as cfg, probe


def _ms(value: int) -> str:
    return f"{value / 1000:.3f}"


def _escape_filter_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", r"\'")


def ffmpeg_available() -> bool:
    binary = cfg.ffmpeg_bin()
    return bool(binary) and os.path.exists(binary)


def _run(cmd: list[str], timeout: int = 600) -> tuple[bool, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip()[-500:]
    return True, ""


def render(items: list[dict], out_path: str | Path, *, resolution: dict,
           fps: int = 30, label_text: str | None = None,
           subtitles: list[dict] | None = None,
           audio_mode: str = "original", subtitle_style: dict | None = None,
           extra_inputs: list[str] | None = None) -> dict:
    """按时间线区间渲染。items 需含 path/source_in_ms/source_out_ms。

    audio_mode: original=使用片段原轨；none=不铺音轨（交付阶段必须显式失败）。
    """
    result = {"ok": False, "output": str(out_path), "error": None,
              "audio_embedded": False, "label_burned": False,
              "subtitles_rendered": 0, "duration": None, "width": None,
              "height": None, "input_count": 0}
    if not items:
        result["error"] = "EMPTY_TIMELINE"
        return result
    ffmpeg = cfg.ffmpeg_bin()
    if not ffmpeg_available():
        result["error"] = "FFMPEG_UNAVAILABLE"
        return result

    for item in items:
        start, end = item["source_in_ms"], item["source_out_ms"]
        if start < 0 or end <= start:
            result["error"] = "INVALID_SOURCE_RANGE"
            return result
        if not os.path.isfile(item["path"]):
            result["error"] = f"MISSING_MEDIA:{item['path']}"
            return result

    width, height = int(resolution["width"]), int(resolution["height"])
    tempdir = tempfile.mkdtemp(prefix="hookflow_")
    try:
        parts, labels = [], []
        for index, item in enumerate(items):
            start, end = _ms(item["source_in_ms"]), _ms(item["source_out_ms"])
            parts.append(
                f"[{index}:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
                f"fps={fps}[v{index}]")
            labels.append(f"[v{index}]")
        parts.append(f"{''.join(labels)}concat=n={len(items)}:v=1:a=0[vc]")
        current = "vc"

        style = subtitle_style or {}
        font = style.get("font") or (cfg.settings().get("subtitle") or {}).get("font") \
            or r"C:\Windows\Fonts\msyh.ttc"
        font_ok = os.path.exists(font)
        for index, event in enumerate(subtitles or []):
            text = str(event.get("text") or "").strip()
            if not text or not font_ok:
                continue
            text_file = os.path.join(tempdir, f"sub{index}.txt")
            Path(text_file).write_text(text.replace("\n", " "), encoding="utf-8")
            start, end = _ms(int(event["start_ms"])), _ms(int(event["end_ms"]))
            parts.append(
                f"[{current}]drawtext=fontfile='{_escape_filter_path(font)}':"
                f"textfile='{_escape_filter_path(text_file)}':"
                f"fontsize={int(style.get('fontsize', 64))}:"
                f"fontcolor={style.get('color', 'white')}:"
                f"borderw={int(style.get('outline', 4))}:bordercolor=black:"
                f"x=(w-text_w)/2:y=h-{int(style.get('margin', 250))}:"
                f"enable='between(t,{start},{end})'[sub{index}]")
            current = f"sub{index}"
            result["subtitles_rendered"] += 1

        if label_text and font_ok:
            label_file = os.path.join(tempdir, "label.txt")
            Path(label_file).write_text(label_text, encoding="utf-8")
            parts.append(
                f"[{current}]drawtext=fontfile='{_escape_filter_path(font)}':"
                f"textfile='{_escape_filter_path(label_file)}':"
                f"fontsize={int(style.get('label_fontsize', 40))}:fontcolor=white:"
                f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=80[vout]")
            current = "vout"
            result["label_burned"] = True
        else:
            parts.append(f"[{current}]copy[vout]")
            current = "vout"

        has_audio = audio_mode == "original" and all(
            item.get("has_audio") for item in items)
        total_ms = sum(item["source_out_ms"] - item["source_in_ms"] for item in items)
        if has_audio:
            a_parts, a_labels = [], []
            for index, item in enumerate(items):
                a_parts.append(
                    f"[{index}:a]atrim=start={_ms(item['source_in_ms'])}:"
                    f"end={_ms(item['source_out_ms'])},asetpts=PTS-STARTPTS,"
                    f"aresample=44100[a{index}]")
                a_labels.append(f"[a{index}]")
            parts.extend(a_parts)
            parts.append(f"{''.join(a_labels)}concat=n={len(items)}:v=0:a=1[aout]")
            result["audio_embedded"] = True

        cmd = [ffmpeg, "-y"]
        for item in items:
            cmd += ["-i", item["path"]]
        for extra in extra_inputs or []:
            cmd += ["-i", extra]
        cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
        if has_audio:
            cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-r", str(fps), "-pix_fmt", "yuv420p",
                "-t", f"{total_ms / 1000:.3f}", "-movflags", "+faststart",
                str(out_path)]

        os.makedirs(Path(out_path).parent, exist_ok=True)
        ok, error = _run(cmd)
        if not ok:
            result["error"] = "FFMPEG_FAILED: " + error
            return result
        info = probe.probe_media(str(out_path))
        if info.get("error") or not info.get("has_video"):
            result["error"] = "RENDER_PROBE_FAILED: " + str(info.get("error"))
            return result
        result.update(ok=True, duration=info["duration"], width=info["width"],
                      height=info["height"], input_count=len(items))
        return result
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


def detect_black(path: str, min_duration: float = 0.5) -> dict:
    """blackdetect 输出解析；不可用时返回 unavailable，不伪造通过。"""
    ffmpeg = cfg.ffmpeg_bin()
    if not ffmpeg_available():
        return {"available": False, "segments": [], "error": "FFMPEG_UNAVAILABLE"}
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path, "-vf",
         f"blackdetect=d={min_duration}:pix_th=0.10", "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    segments = []
    for line in (proc.stderr or "").splitlines():
        if "black_start:" in line:
            try:
                fields = dict(pair.split(":") for pair in
                              line.split("black_start:")[1].split())
                segments.append({"start": float(fields["black_start"]),
                                  "end": float(fields["black_end"])})
            except (KeyError, ValueError):
                return {"available": False, "segments": [], "error": "BLACKDETECT_PARSE"}
    return {"available": True, "segments": segments, "error": None}


def loudness(path: str) -> dict:
    ffmpeg = cfg.ffmpeg_bin()
    if not ffmpeg_available():
        return {"available": False, "integrated_lufs": None, "error": "FFMPEG_UNAVAILABLE"}
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path, "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    values = re.findall(r"I:\s*([-+]?\d+(?:\.\d+)?)\s*LUFS", proc.stderr or "")
    if values:
        try:
            return {"available": True, "integrated_lufs": float(values[-1]), "error": None}
        except ValueError:
            pass
    return {"available": False, "integrated_lufs": None, "error": "LOUDNESS_UNAVAILABLE"}
