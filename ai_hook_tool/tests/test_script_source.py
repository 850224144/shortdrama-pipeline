# -*- coding: utf-8 -*-
import os

from hooktool import script_source


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "第一集.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_parse_script_txt_extracts_dialogue_skips_action(tmp_path):
    text = (
        "1-1日内医院走廊\n"
        "人物：白玉瑶、陆时衍\n"
        "△白玉瑶扬手一巴掌。\n"
        "白玉瑶（咬着牙）：这一巴掌，打你老婆生孩子。\n"
        "温知宁VO（内心独白）：我婆婆是个娇软傻白甜。\n"
    )
    entries = script_source.parse_script_txt(_write(tmp_path, text))
    roles = [e["role"] for e in entries]
    assert "白玉瑶" in roles
    assert "温知宁" in roles
    by_role = {e["role"]: e for e in entries}
    assert by_role["温知宁"]["vo"] is True
    assert all(e["source"] == "txt" for e in entries)


def test_parse_script_txt_missing_file():
    assert script_source.parse_script_txt("不存在.txt") == []


def test_pick_txt_candidates_conflict_first():
    entries = [
        {"role": "甲", "line": "普通叙述一句。", "vo": False, "source": "txt"},
        {"role": "乙", "line": "凭什么你敢欺负我老婆！", "vo": False, "source": "txt"},
    ]
    conflict = ["凭什么", "欺负"]
    suspense = ["真相"]
    picked = script_source.pick_txt_candidates(entries, conflict, suspense, count=2)
    assert picked[0]["role"] == "乙"
