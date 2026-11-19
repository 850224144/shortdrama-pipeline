# -*- coding: utf-8 -*-
"""策划：方案数量、差异化、确定性校验与五问。"""
from flow_support import make_project, build_story_and_proposals
from hooktool.flow import settings
from hooktool.flow.validator import validate_differentiation, validate_plan


def test_proposals_are_bounded_and_semantically_distinct(tmp_path):
    store = make_project(tmp_path)
    snapshot, story, result = build_story_and_proposals(store)
    assert result["status"] == "PROPOSED"
    proposals = result["proposals"]
    assert 1 <= len(proposals) <= settings.policy()["proposal_limit"]
    signatures = {(p["mechanism"], p["opening"], p["turn"], p["ending"])
                  for p in proposals}
    assert len(signatures) == len(proposals)
    for proposal in proposals:
        assert proposal["perspective"]["char_id"] in {
            c["char_id"] for c in story["characters"]}


def test_plan_passes_deterministic_validation(tmp_path):
    store = make_project(tmp_path)
    snapshot, story, result = build_story_and_proposals(store)
    for proposal in result["proposals"]:
        validation = validate_plan(proposal, story, snapshot,
                                   policy=settings.policy())
        assert validation["ok"], validation["blocking_issues"]
        answers = proposal["answers"]
        for key in ("what_first", "who_wants", "obstacle", "new_info",
                    "ending_question"):
            assert str(answers.get(key) or "").strip()
        assert proposal["planned_duration_ms"] == sum(
            shot["planned_ms"] for shot in proposal["shots"])
        assert len(proposal["shots"]) <= settings.policy()["max_shots"]


def test_each_shot_is_locatable_and_not_duplicated(tmp_path):
    store = make_project(tmp_path)
    _, _, result = build_story_and_proposals(store)
    proposal = result["proposals"][0]
    spans = [(s["source_start_ms"], s["source_end_ms"]) for s in proposal["shots"]]
    for start, end in spans:
        assert start >= 0 and end > start
    assert len(set(spans)) == len(spans)


def test_insufficient_material_is_not_padded(tmp_path):
    store = make_project(tmp_path)
    snapshot, story, result = build_story_and_proposals(store)
    differentiation = validate_differentiation(result["proposals"])
    assert differentiation["ok"]


def test_short_episode_reports_shortfall_not_fake_duration(tmp_path):
    from flow_support import make_snapshot
    from hooktool.flow.planner import build_proposals
    from hooktool.flow.story import build_story

    short = make_snapshot(duration_ms=12000,
                          lines=[(1000, 3000, "你到底签不签？"),
                                 (6000, 8000, "我不签，你走开。")])
    story = build_story(short, config=settings.config())
    result = build_proposals(story, "ev_short", policy=settings.policy(),
                             config=settings.config())
    if result["proposals"]:
        for proposal in result["proposals"]:
            assert proposal["insufficient_material"] or \
                proposal["planned_duration_ms"] >= 30000
    else:
        assert result["status"] == "INSUFFICIENT_MATERIAL"
