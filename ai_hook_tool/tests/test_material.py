# -*- coding: utf-8 -*-
import os

from hooktool.material import scan_material, read_texts


def _touch(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"0")


def test_scan_classifies_episodes_faces_cover_texts(tmp_path):
    src = tmp_path / "某部剧"
    _touch(str(src / "2.mp4"))
    _touch(str(src / "1.mp4"))
    _touch(str(src / "白玉瑶.png"))
    _touch(str(src / "陆时衍.jpg"))
    _touch(str(src / "某部剧-sph.jpg"))
    _touch(str(src / "简介详情.txt"))

    mat = scan_material(str(src))
    assert [ep for ep, _ in mat["episodes"]] == [1, 2]
    assert mat["first_episode"][0] == 1
    assert set(mat["faces"].keys()) == {"白玉瑶", "陆时衍"}
    assert mat["cover"].endswith("-sph.jpg")
    assert len(mat["texts"]) == 1


def test_scan_warns_when_no_face(tmp_path):
    src = tmp_path / "无脸剧"
    _touch(str(src / "1.mp4"))
    mat = scan_material(str(src))
    assert any("定妆" in w for w in mat["warnings"])
    assert mat["faces"] == {}


def test_scan_missing_when_no_episode(tmp_path):
    src = tmp_path / "空目录"
    src.mkdir()
    mat = scan_material(str(src))
    assert any("mp4" in m for m in mat["missing"])


def test_read_texts_concat(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("婆媳矛盾", encoding="utf-8")
    assert "婆媳" in read_texts([str(p)])


# --- 字幕/转写必须绑定首集（2026-11-17 修：此前任何 .srt/.vtt 都无条件覆盖） ---

def test_multi_episode_subtitles_bind_first_episode(tmp_path):
    src = tmp_path / "多集字幕剧"
    _touch(str(src / "1.mp4"))
    _touch(str(src / "8.mp4"))
    (src / "1.srt").write_text("首集字幕", encoding="utf-8")
    (src / "8.srt").write_text("第八集字幕", encoding="utf-8")

    mat = scan_material(str(src))
    assert mat["first_episode"][0] == 1
    assert mat["transcript"].endswith("1.srt"), "必须取首集字幕，不能取后扫到的第八集"
    assert not any("忽略" in w for w in mat["warnings"])


def test_subtitle_with_chinese_episode_number_binds_first(tmp_path):
    src = tmp_path / "第几集命名剧"
    _touch(str(src / "第1集.mp4"))
    _touch(str(src / "第3集.mp4"))
    (src / "第3集.srt").write_text("第三集字幕", encoding="utf-8")
    (src / "第1集.srt").write_text("首集字幕", encoding="utf-8")

    mat = scan_material(str(src))
    assert mat["transcript"].endswith("第1集.srt")


def test_non_first_subtitle_is_ignored_not_used(tmp_path):
    src = tmp_path / "只有第二集字幕"
    _touch(str(src / "1.mp4"))
    (src / "第2集.srt").write_text("第二集字幕", encoding="utf-8")

    mat = scan_material(str(src))
    assert mat["transcript"] is None, "集号与首集不符的字幕不得当作首集剧情事实"
    assert any("忽略" in w and "E1" in w for w in mat["warnings"])


def test_unique_unlabeled_subtitle_used_with_warning(tmp_path):
    src = tmp_path / "唯一无集号字幕"
    _touch(str(src / "1.mp4"))
    (src / "字幕.srt").write_text("无集号字幕", encoding="utf-8")

    mat = scan_material(str(src))
    assert mat["transcript"].endswith("字幕.srt")
    assert any("未标集号" in w for w in mat["warnings"])


def test_multi_unlabeled_subtitles_ignored(tmp_path):
    src = tmp_path / "多份无集号字幕"
    _touch(str(src / "1.mp4"))
    (src / "字幕A.srt").write_text("A", encoding="utf-8")
    (src / "字幕B.srt").write_text("B", encoding="utf-8")

    mat = scan_material(str(src))
    assert mat["transcript"] is None
    w = [x for x in mat["warnings"] if "忽略" in x]
    assert w, "多份无集号字幕必须给忽略告警"
    assert "字幕A.srt" in w[0] and "字幕B.srt" in w[0]


def test_txt_transcript_binds_first_episode(tmp_path):
    src = tmp_path / "文本转写剧"
    _touch(str(src / "1.mp4"))
    _touch(str(src / "2.mp4"))
    (src / "第2集转写.txt").write_text("第二集", encoding="utf-8")
    (src / "第1集转写.txt").write_text("第一集", encoding="utf-8")

    mat = scan_material(str(src))
    assert mat["transcript"].endswith("第1集转写.txt")
