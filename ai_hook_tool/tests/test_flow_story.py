# -*- coding: utf-8 -*-
"""剧情理解层：事件、人物、前提、双时间线、说话人分离。"""
from flow_support import make_snapshot
from hooktool.flow import settings
from hooktool.flow.story import build_story


def test_events_keep_evidence_and_participants():
    story = build_story(make_snapshot(), config=settings.config())
    assert story["story_version"] == "1"
    assert len(story["events"]) >= 4
    assert len(story["characters"]) >= 2
    for event in story["events"]:
        assert event["participants"]
        assert event["character_ids"]
        assert event["evidence_ids"]
        assert event["verification_status"] == "needs_review"
        assert event["inference"] == "heuristic_not_fact"
        span = event["source_spans"][0]
        assert span["end_ms"] > span["start_ms"]


def test_dual_timeline_is_explicit():
    story = build_story(make_snapshot(), config=settings.config())
    timeline = story["story_time"]
    assert timeline["status"] in ("presentation_assumed", "partially_unknown")
    assert story["presentation_order"] == [event["event_id"] for event in story["events"]]
    for constraint in timeline["constraints"]:
        assert constraint["verification_status"] == "needs_review"


def test_prerequisite_chain_points_backwards():
    story = build_story(make_snapshot(), config=settings.config())
    by_id = {event["event_id"]: event for event in story["events"]}
    linked = [event for event in story["events"] if event["prerequisite_event_ids"]]
    assert linked, "因果标记应至少建立一条前提关系"
    for event in linked:
        for prerequisite in event["prerequisite_event_ids"]:
            assert prerequisite in by_id


def test_speakers_are_not_identity_claims():
    story = build_story(make_snapshot(), config=settings.config())
    characters = story["characters"]
    assert len(characters) >= 2
    for character in characters:
        assert character["verification_status"] == "needs_review"
        assert character["char_id"].startswith("char_")


def test_coverage_reports_timing_gaps():
    story = build_story(make_snapshot(), config=settings.config())
    coverage = story["coverage"]
    assert coverage["event_count"] == len(story["events"])
    assert coverage["duration_ms"] > 0
    assert 0 < coverage["ratio"] <= 1
