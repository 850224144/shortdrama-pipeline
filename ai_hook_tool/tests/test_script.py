# -*- coding: utf-8 -*-
from hooktool import script


def _material():
    return {"drama_name": "测试剧", "faces": {"白玉瑶": "p.png"}}


def test_asr_takes_priority_over_unmatched_script_text():
    txt = [{"role": "白玉瑶", "line": "剧本候选台词。", "vo": False, "source": "txt"}]
    man = script.build_script(
        _material(), ["短标"], ["ASR冲突台词"], "婆媳", "ep", txt,
        transcript_lines=["[1.00-2.00] ASR冲突台词"],
        quote_records=[{"text": "ASR冲突台词", "start": 1.0, "end": 2.0,
                        "score": 2, "source": "asr", "source_index": 0}],
    )
    pre = man["segments"][0]["shots"]
    assert pre[0]["dialogue"] == "ASR冲突台词"
    assert pre[0]["dialogue_source"] == "asr"


def test_unmatched_script_text_is_not_used_as_source_dialogue():
    txt = [{"role": "白玉瑶", "line": "凭什么你敢欺负人！", "vo": False, "source": "txt"}]
    man = script.build_script(_material(), [], [], "婆媳", "ep", txt)
    assert not man["segments"][0]["shots"]
    assert man["status"] == "blocked"
    assert any("首镜" in reason for reason in man["hard_block"])


def test_asr_aligned_script_dialogue_keeps_source_timing():
    mat = {"drama_name": "测试剧", "faces": {"白玉瑶": "p.png"}}
    txt = [{"role": "白玉瑶", "line": "凭什么你敢欺负人！", "vo": False,
            "source": "txt", "scene": "1-1 走廊", "asr_start": 1.0, "asr_end": 2.0,
            "asr_match": 0.95, "asr_text": "凭什么你敢欺负人！",
            "asr_event_indices": [0]}]
    man = script.build_script(
        mat, [], ["凭什么你敢欺负人！"], "婆媳", "ep", txt,
        quote_records=[{"text": "凭什么你敢欺负人！", "start": 1.0, "end": 2.0,
                        "score": 2, "source": "asr", "source_index": 0}],
        transcript_lines=["[1.0-2.0] 凭什么你敢欺负人！"],
    )
    shot = man["segments"][0]["shots"][0]
    assert shot["dialogue_source"] == "asr"
    assert shot["source_time"] == {"start": 1.0, "end": 2.0, "asr_events": [0]}
    assert shot["role"] == "白玉瑶"


def test_dialogue_dedup():
    quote = {"text": "重复台词。", "start": 1.0, "end": 2.0,
             "score": 2, "source": "asr", "source_index": 0}
    txt = [{"role": "白玉瑶", "line": "重复台词。", "vo": False, "source": "txt",
            "asr_start": 1.0, "asr_end": 2.0, "asr_match": 0.95,
            "asr_text": "重复台词。", "asr_event_indices": [0]}]
    man = script.build_script(
        _material(), [], ["重复台词。"], "婆媳", "ep", txt,
        quote_records=[quote], transcript_lines=["[1.0-2.0] 重复台词。"])
    lines = [shot["dialogue"] for segment in man["segments"]
             for shot in segment["shots"] if shot["dialogue"]]
    assert len(lines) == len(set(lines))


def test_txt_role_aligns_to_face():
    mat = {"drama_name": "测试剧", "faces": {"白玉瑶": "p.png", "温知宁": "w.png"}}
    txt = [{"role": "温知宁", "line": "凭什么你敢欺负人！", "vo": False,
            "source": "txt", "asr_start": 1.0, "asr_end": 2.0,
            "asr_match": 0.95, "asr_text": "凭什么你敢欺负人！",
            "asr_event_indices": [0]}]
    man = script.build_script(
        mat, [], ["凭什么你敢欺负人！"], "婆媳", "ep", txt,
        quote_records=[{"text": "凭什么你敢欺负人！", "start": 1.0, "end": 2.0,
                        "score": 2, "source": "asr", "source_index": 0}],
        transcript_lines=["[1.0-2.0] 凭什么你敢欺负人！"])
    shot = man["segments"][0]["shots"][0]
    assert shot["role"] == "温知宁"
    assert shot["ref_image"] == "w.png"