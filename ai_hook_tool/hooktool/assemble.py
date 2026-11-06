# -*- coding: utf-8 -*-
"""FFmpeg 本地拼接：两段片段 → 竖屏 1080x1920 成片，压 AI 标注角标。

干净 concat 链，不含抗查重/账号水印（定稿 D11）。角标压失败走软告警，降级出片。
"""
import os
import subprocess
import tempfile

from . import config as cfg, paths, probe

def compose_output_name(drama_name: str, episode_label: str, timestamp: str) -> str:
    safe = "".join(c for c in drama_name if c not in '\\/:*?"<>|')
    return f"{safe}_{episode_label}_{timestamp}.mp4"


def _video_scale_filter(index: int, res: dict) -> str:
    return (
        f"[{index}:v]scale={res['width']}:{res['height']}"
        f":force_original_aspect_ratio=decrease,"
        f"pad={res['width']}:{res['height']}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1[v{index}]"
    )


def _build_complex(clips: list[dict], res: dict, badge: dict | None) -> tuple[str, list[str], bool]:
    n = len(clips)
    parts = [_video_scale_filter(i, res) for i in range(n)]
    v_concat = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{v_concat}concat=n={n}:v=1:a=0[vc]")

    has_audio = all(c.get("has_audio") for c in clips) and n > 0
    video_label = "vc"

    if badge and badge.get("font") and os.path.exists(badge["font"]):
        badge_text = badge.get("text", "")
        badge_fd, text_file = tempfile.mkstemp(suffix=".txt", prefix="hook_badge_")
        with os.fdopen(badge_fd, "w", encoding="utf-8") as f:
            f.write(badge_text)
        font = badge["font"].replace("\\", "/").replace(":", "\\:")
        text_file_ff = text_file.replace("\\", "/").replace(":", "\\:")
        parts.append(
            f"[vc]drawtext=fontfile='{font}':textfile='{text_file_ff}':"
            f"fontsize={badge.get('fontsize', 36)}:fontcolor={badge.get('color', '&H00FFFFFF')}:"
            f"borderw={badge.get('outline', 1)}:bordercolor={badge.get('outline_color', '&H00000000')}:"
            f"x=(w-text_w)/2:y=h-{badge.get('margin', 60)}[vout]"
        )
        video_label = "vout"
    else:
        parts.append("[vc]copy[vout]")
        video_label = "vout"

    filter_complex = ";".join(parts)
    maps = ["-map", f"[{video_label}]"]
    if has_audio:
        a_concat = "".join(f"[{i}:a]" for i in range(n))
        filter_complex += f";{a_concat}concat=n={n}:v=0:a=1[aout]"
        maps += ["-map", "[aout]"]
    return filter_complex, maps, has_audio


def _run_ffmpeg(ffmpeg: str, cmd: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return False, proc.stderr.strip()[-300:]
    return True, ""


def run_assemble(clips: list[dict], output_path: str, badge_text: str | None = None) -> dict:
    settings = cfg.settings()
    res = settings["resolution"]
    ffmpeg = cfg.ffmpeg_bin()
    warns: list[str] = []
    result = {"output": output_path, "duration": None, "width": None, "height": None,
              "has_audio": False, "badge_ok": False, "warns": warns, "error": None}

    if not clips:
        result["error"] = "无片段可拼接"
        return result
    if not ffmpeg or not os.path.exists(ffmpeg):
        result["error"] = f"ffmpeg 不可用：{ffmpeg}"
        return result

    badge = dict(settings.get("badge", {}))
    badge["text"] = badge_text or settings.get("badge_text", "")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def ffmpeg_cmd(with_badge: bool) -> list[str]:
        b = badge if with_badge else None
        filter_complex, maps, has_audio = _build_complex(clips, res, b)
        cmd = [ffmpeg, "-y"]
        for c in clips:
            cmd += ["-i", c["path"]]
        cmd += ["-filter_complex", filter_complex] + maps
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-r", str(settings.get("fps", 30)), "-pix_fmt", "yuv420p"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", output_path]
        return cmd

    ok, err = _run_ffmpeg(ffmpeg, ffmpeg_cmd(True))
    if not ok:
        warns.append(f"AI 标注角标压失败，降级为无角标出片：{err[:120]}")
        ok, err = _run_ffmpeg(ffmpeg, ffmpeg_cmd(False))
        result["badge_ok"] = False
    else:
        result["badge_ok"] = True

    if not ok:
        result["error"] = err
        return result

    info = probe.probe_media(output_path)
    result["duration"] = info["duration"]
    result["width"] = info["width"]
    result["height"] = info["height"]
    result["has_audio"] = info["has_audio"]
    if info["error"]:
        warns.append(f"成片核验异常：{info['error']}")
    return result
