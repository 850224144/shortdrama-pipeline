# -*- coding: utf-8 -*-
"""候选召回：覆盖整集、分块重叠、最小候选规模。"""
from flow_support import make_snapshot
from hooktool.flow import settings
from hooktool.flow.recall import _chunks, recall
from hooktool.flow.story import build_story


def test_recall_covers_whole_episode():
    story = build_story(make_snapshot(), config=settings.config())
    result = recall(story, policy=settings.policy(), config=settings.config())
    coverage = result["coverage"]
    assert coverage["event_total"] == len(story["events"])
    assert coverage["covered"] == coverage["event_total"]
    assert coverage["uncovered_event_ids"] == []


def test_recall_keeps_minimum_candidate_pool():
    story = build_story(make_snapshot(), config=settings.config())
    result = recall(story, policy=settings.policy(), config=settings.config())
    assert len(result["candidates"]) >= min(
        settings.policy()["min_candidates"], len(story["events"]))
    for candidate in result["candidates"]:
        assert isinstance(candidate["score"], int)
        assert candidate["role"] in ("opening", "setup", "turn", "obstacle")
        assert candidate["context_event_ids"]


def test_chunks_overlap_and_cover_tail():
    items = list(range(25))
    chunks = _chunks(items, 10, 2)
    assert chunks[0][0] == 0
    assert chunks[-1][-1] == 24
    for left, right in zip(chunks, chunks[1:]):
        assert set(left) & set(right)


def test_empty_events_are_reported_not_invented():
    story = build_story(make_snapshot(), config=settings.config())
    story["events"] = []
    result = recall(story, policy=settings.policy(), config=settings.config())
    assert result["candidates"] == []
    assert result["issues"][0]["code"] == "INSUFFICIENT_MATERIAL"
