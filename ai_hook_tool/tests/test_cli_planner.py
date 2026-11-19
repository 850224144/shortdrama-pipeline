# -*- coding: utf-8 -*-
import json

from hooktool import cli, manifest as mf


def _timed_quote(index, text, start, end, score=2.0):
    return {"text": text, "short_title": text[:16], "start": start, "end": end,
            "score": score, "source": "asr", "source_index": index,
            "high_hits": [], "conflict_hits": ["冲突"], "suspense_hits": [],
            "low_signal_hits": []}


def test_rules_mode_never_loads_provider(monkeypatch):
    monkeypatch.setattr(cli.paths, "ensure_workdir", lambda: None)
    monkeypatch.setattr(cli.material, "scan_material", lambda _src: {
        "missing": [], "drama_name": "测试剧", "faces": {"甲": "甲.png"},
        "first_episode": (1, "E01.mp4"), "texts": [],
    })
    quotes = [_timed_quote(0, "开场冲突", 0.0, .5),
              _timed_quote(1, "升级冲突", 5.0, 5.5),
              _timed_quote(2, "更多冲突", 12.0, 12.5),
              _timed_quote(3, "后段变化", 31.0, 31.5),
              _timed_quote(4, "片尾真相", 40.0, 40.5)]
    monkeypatch.setattr(cli.transcribe, "load_or_transcribe", lambda _mat: (
        ["[0-0.5] 开场冲突", "[5-5.5] 升级冲突", "[12-12.5] 更多冲突",
         "[31-31.5] 后段变化", "[40-40.5] 片尾真相"], None))
    monkeypatch.setattr(cli.quotes, "rank_quotes", lambda *_a, **_kw: quotes)
    monkeypatch.setattr(cli.genre, "detect_genre", lambda _text: {"genre": "悬疑", "pending": False})
    monkeypatch.setattr(cli.material, "read_texts", lambda _paths: "")
    monkeypatch.setattr(cli, "_load_hook_planner", lambda: (_ for _ in ()).throw(AssertionError("provider called")))
    monkeypatch.setattr(cli.mf, "save", lambda _manifest: None)

    result = cli._cmd_script(type("Args", (), {"src": "素材", "planner": "rules"})())
    assert result == 0


def test_llm_planner_uses_two_calls_and_records_camera_suggestions(monkeypatch):
    monkeypatch.setattr(cli.paths, "ensure_workdir", lambda: None)
    monkeypatch.setattr(cli.material, "scan_material", lambda _src: {
        "missing": [], "drama_name": "测试剧", "faces": {"甲": "甲.png"},
        "first_episode": (1, "E01.mp4"), "texts": [],
    })
    starts = [0.0, 5.0, 12.0, 20.0, 28.0, 31.0, 40.0]
    quotes = [_timed_quote(i, f"候选台词{i}", start, start + .5)
              for i, start in enumerate(starts)]
    transcript = [f"[{start}-{start + .5}] 候选台词{i}"
                  for i, start in enumerate(starts)]
    monkeypatch.setattr(cli.transcribe, "load_or_transcribe", lambda _mat: (transcript, None))
    monkeypatch.setattr(cli.quotes, "rank_quotes", lambda *_a, **_kw: quotes)
    monkeypatch.setattr(cli.genre, "detect_genre", lambda _text: {"genre": "悬疑", "pending": False})
    monkeypatch.setattr(cli.material, "read_texts", lambda _paths: "")
    monkeypatch.setattr(cli.script_source, "parse_script_txt", lambda _path: [])
    monkeypatch.setattr(cli, "_first_script_txt", lambda _paths: None)

    calls = []

    def response(prompt):
        calls.append(prompt)
        if "分镜拍法助手" in prompt:
            ids = [f"asr:{i}" for i in range(7)]
            return json.dumps([{"candidate_id": item_id, "framing": "近景"}
                               for item_id in ids])
        return json.dumps([
            {"candidate_id": "asr:0", "role_in_hook": "opening", "why_it_hooks": "冲突开场"},
            {"candidate_id": "asr:1", "role_in_hook": "escalation", "why_it_hooks": "矛盾升级"},
            {"candidate_id": "asr:6", "role_in_hook": "cliffhanger", "why_it_hooks": "留下问题"},
            {"candidate_id": "invented", "role_in_hook": "opening", "why_it_hooks": "虚构"},
        ])

    monkeypatch.setattr(cli, "_load_hook_planner", lambda: response)

    def camera_response(shots, _provider):
        calls.append("camera")
        return ({shot["candidate_id"]: {"candidate_id": shot["candidate_id"],
                                        "framing": "近景"} for shot in shots}, "used")

    monkeypatch.setattr(cli.hook_planner, "suggest_camera", camera_response)
    saved = {}
    monkeypatch.setattr(cli.mf, "save", lambda manifest: saved.update(manifest))

    result = cli._cmd_script(type("Args", (), {"src": "素材", "planner": "llm"})())
    assert result == 0
    assert len(calls) == 2
    assert saved["planning"]["logical_calls"] == 2
    assert saved["planning"]["method"] == "llm_assisted"
    assert all("补充景别" in shot["prompt"]
               for segment in saved["segments"] for shot in segment["shots"])
    assert saved["hook_qc"]["opening_first_shot_start"] == 0.0


def test_no_timed_opening_candidate_stays_blocked(monkeypatch):
    quotes = [{"text": "冲突对白", "short_title": "冲突对白", "start": None,
               "end": None, "score": 5, "source": "asr", "source_index": 0}]
    result = cli.script.build_script(
        {"drama_name": "剧", "faces": {"甲": "a.png"}}, ["冲突对白"],
        ["冲突对白"], "悬疑", "ep", quote_records=quotes,
        transcript_lines=["冲突对白"])
    assert result["status"] == "blocked"
    assert not result["hook_qc"]["opening_within_limit"]
