# -*- coding: utf-8 -*-
from hooktool import script


def _material():
    return {"drama_name": "测试剧", "faces": {"白玉瑶": "p.png"}}


def test_asr_takes_priority_over_txt():
    txt = [{"role": "白玉瑶", "line": "剧本候选台词。", "vo": False, "source": "txt"}]
    man = script.build_script(_material(), ["短标"], ["ASR冲突台词"], "婆媳", "ep", txt)
    pre = man["segments"][0]["shots"]
    assert pre[0]["dialogue"] == "ASR冲突台词"
    assert pre[0]["dialogue_source"] == "asr"


def test_txt_fills_when_no_asr():
    txt = [{"role": "白玉瑶", "line": "凭什么你敢欺负人！", "vo": False, "source": "txt"}]
    man = script.build_script(_material(), [], [], "婆媳", "ep", txt)
    pre = man["segments"][0]["shots"]
    assert pre[0]["dialogue"] == "凭什么你敢欺负人！"
    assert pre[0]["dialogue_source"] == "txt"
    assert any("txt" in w for w in man["warns"])


def test_dialogue_dedup():
    txt = [
        {"role": "白玉瑶", "line": "重复台词。", "vo": False, "source": "txt"},
        {"role": "白玉瑶", "line": "重复台词。", "vo": False, "source": "txt"},
    ]
    man = script.build_script(_material(), [], [], "婆媳", "ep", txt)
    lines = [s["dialogue"] for s in man["segments"][0]["shots"] if s["dialogue"]]
    assert len(lines) == len(set(lines))


def test_txt_role_aligns_to_face():
    mat = {"drama_name": "测试剧", "faces": {"白玉瑶": "p.png", "温知宁": "w.png"}}
    txt = [{"role": "温知宁", "line": "凭什么你敢欺负人！", "vo": False, "source": "txt"}]
    man = script.build_script(mat, [], [], "婆媳", "ep", txt)
    shot = man["segments"][0]["shots"][0]
    assert shot["role"] == "温知宁"
    assert shot["ref_image"] == "w.png"