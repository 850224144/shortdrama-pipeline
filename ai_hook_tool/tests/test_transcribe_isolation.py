# -*- coding: utf-8 -*-
"""首集转写缓存隔离回归（2026-11-17 修素材串戏）。

覆盖两条用户报告的场景：
1. 连续换两部剧：上一部剧的转写缓存不得被下一部剧命中；
2. 缓存的归属校验（无归属头 / 换剧名 / 换版本指纹）任一不符即不采用。
"""
import os

from hooktool import transcribe


def _write_video(path: str, payload: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(payload)
    return path


def _seed_cache(drama: str, ep: int, video: str, lines: list[str],
                drama_in_header: str | None = None) -> str:
    """按新格式预置缓存（带归属头），模拟「这部剧已经转写过一次」。"""
    fp = transcribe._source_fingerprint(video)
    cache = transcribe.cache_path_for(drama, ep, video, fingerprint=fp)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    header = transcribe._header_line(drama_in_header or drama, ep, video, fp)
    with open(cache, "w", encoding="utf-8") as f:
        f.write(header + "\n" + "\n".join(lines))
    return cache


def _no_whisper(monkeypatch, tmp_path):
    """让重转路径必然 PENDING，测试只关心「有没有错误复用别剧缓存」。"""
    monkeypatch.setattr(transcribe, "WHISPER_MODEL_PATH",
                        str(tmp_path / "no-such-model"))


def test_cache_hit_same_drama(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe.paths, "DIR_SCRIPT", str(tmp_path / "script"))
    video = _write_video(str(tmp_path / "剧A" / "1.mp4"), b"A" * 4096)
    _seed_cache("剧A", 1, video, ["[0.00-1.00] 剧A首集对白"])

    lines, reason = transcribe.transcribe_first_episode(
        video, drama_name="剧A", ep_num=1)
    assert reason is None
    assert lines == ["[0.00-1.00] 剧A首集对白"]


def test_switch_drama_does_not_reuse_previous_transcript(tmp_path, monkeypatch):
    """连续换两部剧：B 剧绝不能读到 A 剧的对白。"""
    monkeypatch.setattr(transcribe.paths, "DIR_SCRIPT", str(tmp_path / "script"))
    _no_whisper(monkeypatch, tmp_path)
    a_video = _write_video(str(tmp_path / "剧A" / "1.mp4"), b"A" * 4096)
    b_video = _write_video(str(tmp_path / "剧B" / "1.mp4"), b"B" * 4096)
    _seed_cache("剧A", 1, a_video, ["[0.00-1.00] 剧A首集对白"])

    lines_a, reason_a = transcribe.transcribe_first_episode(
        a_video, drama_name="剧A", ep_num=1)
    assert lines_a == ["[0.00-1.00] 剧A首集对白"] and reason_a is None

    lines_b, reason_b = transcribe.transcribe_first_episode(
        b_video, drama_name="剧B", ep_num=1)
    assert lines_b == [], "B 剧不得命中 A 剧的缓存"
    assert reason_b, "B 剧应走重转并返回 PENDING 原因，而不是给出别剧对白"
    assert transcribe.cache_path_for("剧B", 1, b_video) != \
        transcribe.cache_path_for("剧A", 1, a_video)


def test_legacy_cache_without_header_ignored(tmp_path, monkeypatch):
    """旧的无归属 transcript_E01.txt（header 缺失）不得被任何剧采用。"""
    monkeypatch.setattr(transcribe.paths, "DIR_SCRIPT", str(tmp_path / "script"))
    _no_whisper(monkeypatch, tmp_path)
    video = _write_video(str(tmp_path / "剧C" / "1.mp4"), b"C" * 4096)
    cache = transcribe.cache_path_for("剧C", 1, video)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        f.write("[0.00-1.00] 来路不明的对白")

    lines, reason = transcribe.transcribe_first_episode(
        video, drama_name="剧C", ep_num=1)
    assert lines == []
    assert reason


def test_header_drama_mismatch_ignored(tmp_path, monkeypatch):
    """缓存文件名对得上、但归属头写了别的剧 → 不采用。"""
    monkeypatch.setattr(transcribe.paths, "DIR_SCRIPT", str(tmp_path / "script"))
    _no_whisper(monkeypatch, tmp_path)
    video = _write_video(str(tmp_path / "剧D" / "1.mp4"), b"D" * 4096)
    _seed_cache("剧D", 1, video, ["[0.00-1.00] 别剧对白"], drama_in_header="剧E")

    lines, reason = transcribe.transcribe_first_episode(
        video, drama_name="剧D", ep_num=1)
    assert lines == []
    assert reason


def test_fingerprint_change_invalidates_cache(tmp_path, monkeypatch):
    """同一部剧换了版本（文件内容变化）→ 旧缓存失效，必须重转。"""
    monkeypatch.setattr(transcribe.paths, "DIR_SCRIPT", str(tmp_path / "script"))
    _no_whisper(monkeypatch, tmp_path)
    video = _write_video(str(tmp_path / "剧F" / "1.mp4"), b"old-version")
    _seed_cache("剧F", 1, video, ["[0.00-1.00] 旧版本对白"])

    # 重下/重切：同路径同大小但内容变了
    with open(video, "wb") as f:
        f.write(b"new-version")
    lines, reason = transcribe.transcribe_first_episode(
        video, drama_name="剧F", ep_num=1)
    assert lines == []
    assert reason


def test_load_or_transcribe_passes_drama_identity(tmp_path, monkeypatch):
    """load_or_transcribe 必须把剧名与集号传给转写，否则隔离失效。"""
    monkeypatch.setattr(transcribe.paths, "DIR_SCRIPT", str(tmp_path / "script"))
    _no_whisper(monkeypatch, tmp_path)
    video = _write_video(str(tmp_path / "剧G" / "1.mp4"), b"G" * 4096)
    _seed_cache("剧G", 1, video, ["[0.00-1.00] 剧G首集对白"])

    material = {"drama_name": "剧G", "first_episode": (1, video), "transcript": None}
    lines, reason = transcribe.load_or_transcribe(material)
    assert lines == ["[0.00-1.00] 剧G首集对白"]
    assert reason is None


def test_missing_first_episode_returns_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe.paths, "DIR_SCRIPT", str(tmp_path / "script"))
    lines, reason = transcribe.transcribe_first_episode(
        str(tmp_path / "不存在" / "1.mp4"))
    assert lines == []
    assert "不存在" in reason
