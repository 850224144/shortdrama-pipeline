# -*- coding: utf-8 -*-
import os
import subprocess

import pytest

from hooktool import assemble, config as cfg, probe

FFMPEG = cfg.ffmpeg_bin()
pytestmark = pytest.mark.skipif(
    not (FFMPEG and os.path.exists(FFMPEG)), reason="ffmpeg 不可用，跳过拼接集成测试"
)


def _make_sample(out_path: str, color: str):
    cmd = [FFMPEG, "-y", "-f", "lavfi", "-i",
           f"color=c={color}:s=1080x1920:d=3:r=30",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path]
    subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_assemble_concats_two_clips_to_portrait(tmp_path):
    a = str(tmp_path / "a.mp4")
    b = str(tmp_path / "b.mp4")
    _make_sample(a, "red")
    _make_sample(b, "blue")

    clips = [{"path": a, "has_audio": False}, {"path": b, "has_audio": False}]
    out = str(tmp_path / "final.mp4")
    res = assemble.run_assemble(clips, out)

    assert res["error"] is None, res["error"]
    assert os.path.exists(out)
    info = probe.probe_media(out)
    assert info["width"] == 1080
    assert info["height"] == 1920
    assert abs(info["duration"] - 6.0) <= 0.6
    assert res["badge_ok"] is True


def test_assemble_no_clips_error(tmp_path):
    res = assemble.run_assemble([], str(tmp_path / "x.mp4"))
    assert res["error"]
