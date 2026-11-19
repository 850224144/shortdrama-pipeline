# -*- coding: utf-8 -*-
import json

from hooktool import hook_planner, script


def _candidate(index=4, text="他是谁？", start=2.0, end=3.0):
    return {"source_index": index, "text": text, "start": start, "end": end,
            "score": 0.0, "source": "asr"}


def test_candidate_id_uses_original_asr_index():
    assert hook_planner.candidate_id(_candidate()) == "asr:4"
    assert hook_planner.candidate_id({"asr_event_indices": [4, 5]}) == "asr:4"


def test_plan_candidates_accepts_only_known_ids():
    candidate = _candidate()
    valid = hook_planner.candidate_id(candidate)

    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return json.dumps([
            {"candidate_id": valid, "role_in_hook": "opening", "why_it_hooks": "提出身份疑问"},
            {"candidate_id": "invented", "role_in_hook": "cliffhanger", "why_it_hooks": "虚构项"},
        ], ensure_ascii=False)

    hints, status = hook_planner.plan_candidates(
        [candidate], "悬疑", "简介里的身份信息", respond)

    assert status == "used"
    assert list(hints) == [valid]
    assert "不得输出时间码" in prompts[0]


def test_plan_candidates_handles_provider_and_json_errors():
    candidate = _candidate()
    assert hook_planner.plan_candidates(
        [candidate], "", "", lambda _prompt: "not json") == ({}, "invalid_output")

    def unavailable(_prompt):
        raise TimeoutError("offline")

    assert hook_planner.plan_candidates([candidate], "", "", unavailable) == ({}, "unavailable")


def test_camera_suggestion_discards_unknown_id_and_limits_fields():
    shot = {"candidate_id": "asr:4", "segment": "前核15s", "role": "甲",
            "dialogue": "他是谁？", "action": {"source_actions": []}}
    suggestions, status = hook_planner.suggest_camera(
        [shot], lambda _prompt: json.dumps([
            {"candidate_id": "asr:4", "framing": "近景", "expression": "未在枚举中的新剧情"},
            {"candidate_id": "fake", "framing": "全景"},
        ], ensure_ascii=False))
    assert status == "used"
    assert suggestions == {"asr:4": {"candidate_id": "asr:4", "framing": "近景"}}


def test_action_for_does_not_infer_a_slap_from_raising_hand():
    action = script._action_for(
        {"text": "她抬手", "action_context": ["她抬手整理头发"]}, 1, "前核15s")
    assert "巴掌" not in action["motion"]
    assert "整理头发" in action["motion"]


def test_unknown_action_uses_neutral_camera_direction():
    action = script._action_for({"text": "一句冲突对白", "action_context": []}, 1, "前核15s")
    assert "不增加动作" in action["motion"]


def test_camera_suggestion_only_appends_shooting_details():
    prompt = "台词：他是谁？"
    result = script._append_camera_suggestion(
        prompt, {"framing": "近景", "camera_movement": "缓慢推进", "new_plot": "追逐"})
    assert "近景" in result and "缓慢推进" in result
    assert "追逐" not in result


def test_genre_keywords_are_additive_and_do_not_change_other_genres():
    from hooktool import config

    mystery = config.keywords_for_genre("悬疑")
    romance = config.keywords_for_genre("甜宠")
    assert "凶手" in mystery["suspense_words"]
    assert "凶手" not in romance["suspense_words"]
    assert "换医生" not in mystery["high_intensity_phrases"]
