# -*- coding: utf-8 -*-
import os

from hooktool import collect, manifest as mf


def _touch(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"0")


def _dirs(tmp_path):
    pre = str(tmp_path / "pre")
    hook = str(tmp_path / "hook")
    flat = str(tmp_path / "flat")
    return {mf.SEG_PRE: pre, mf.SEG_HOOK: hook}, pre, hook, flat


def test_collect_segment_level_not_shot_count(tmp_path):
    dirs, pre, hook, flat = _dirs(tmp_path)
    _touch(os.path.join(pre, "pre_1.mp4"))
    _touch(os.path.join(hook, "hook_1.mp4"))

    man = mf.empty_manifest("测试剧", "婆媳", "E01", "")
    mf.segment(man, mf.SEG_PRE)["shots"] = [{"shot": i} for i in range(1, 4)]
    mf.segment(man, mf.SEG_HOOK)["shots"] = [{"shot": 1}, {"shot": 2}]

    res = collect.collect(man, dirs, flat)
    assert res["missing"] == []
    assert len(res["clips"]) == 2


def test_collect_flat_dir_collected_into_pre(tmp_path):
    dirs, pre, hook, flat = _dirs(tmp_path)
    _touch(os.path.join(flat, "seg1.mp4"))
    _touch(os.path.join(flat, "seg2.mp4"))

    man = mf.empty_manifest("测试剧", "婆媳", "E01", "")
    res = collect.collect(man, dirs, flat)
    assert res["missing"] == []
    assert len(res["clips"]) == 2
    assert all(c["segment"] == mf.SEG_PRE for c in res["clips"])


def test_collect_single_slot_empty_not_blocked(tmp_path):
    dirs, pre, hook, flat = _dirs(tmp_path)
    _touch(os.path.join(pre, "pre_1.mp4"))

    man = mf.empty_manifest("测试剧", "婆媳", "E01", "")
    res = collect.collect(man, dirs, flat)
    assert res["missing"] == []
    assert any("后钩子15s" in p for p in res["pending"])


def test_collect_all_empty_blocked(tmp_path):
    dirs, pre, hook, flat = _dirs(tmp_path)
    man = mf.empty_manifest("测试剧", "婆媳", "E01", "")
    res = collect.collect(man, dirs, flat)
    assert len(res["missing"]) == 1
