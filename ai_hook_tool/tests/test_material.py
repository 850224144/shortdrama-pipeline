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
