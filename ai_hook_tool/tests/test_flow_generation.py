# -*- coding: utf-8 -*-
"""制作任务、片段导入、逐镜验收与时间线。"""
import pytest

from flow_support import build_story_and_proposals, make_project, plan_for
from hooktool.flow import approval, generation, settings, timeline
from hooktool.flow.errors import FlowError


FAKE_MEDIA = {"duration": 60.0, "width": 720, "height": 1280,
              "has_video": True, "has_audio": True, "error": None}


def _approved(tmp_path, monkeypatch):
    store = make_project(tmp_path)
    snapshot, story, result = build_story_and_proposals(store)
    plan = plan_for(store, result["proposals"][0], snapshot, story)
    approval.review_plan(store, plan["plan_revision_id"], "approved",
                         reviewer="qa", facts_reviewed=True, story=story,
                         evidence_snapshot=snapshot, policy=store.policy())
    monkeypatch.setattr("hooktool.flow.generation.probe.probe_media",
                        lambda path: dict(FAKE_MEDIA))
    tasks = generation.create_tasks(store, plan["plan_revision_id"], story, snapshot,
                                    policy=store.policy())
    return store, snapshot, story, plan, tasks


def test_task_pack_is_per_shot_and_carries_policy(tmp_path, monkeypatch):
    store, snapshot, story, plan, tasks = _approved(tmp_path, monkeypatch)
    assert len(tasks["tasks"]) == len(plan["payload"]["shots"])
    flags = []
    for task in tasks["tasks"]:
        payload = task["payload"]
        assert payload["plan_revision_id"] == plan["plan_revision_id"]
        assert payload["shot_id"] == task["shot_id"]
        assert payload["approved_action"]
        assert payload["forbidden"]
        assert payload["output_spec"]["aspect_ratio"] == "9:16"
        flags.append(payload["forbidden"])
    assert all(any("旁白" in item for item in row) for row in flags)


def test_import_rejects_unapproved_directory(tmp_path, monkeypatch):
    store, snapshot, story, plan, tasks = _approved(tmp_path, monkeypatch)
    outsider = tmp_path / "downloads"
    outsider.mkdir()
    clip = outsider / "clip.mp4"
    clip.write_bytes(b"generated")
    with pytest.raises(FlowError) as error:
        generation.import_asset(store, tasks["tasks"][0]["task_id"], str(clip))
    assert error.value.code == "IMPORT_PATH_NOT_APPROVED"


def test_import_and_review_requires_explicit_ranges(tmp_path, monkeypatch):
    store, snapshot, story, plan, tasks = _approved(tmp_path, monkeypatch)
    store.add_import_root(tmp_path)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"generated")
    task = tasks["tasks"][0]
    shot = next(s for s in plan["payload"]["shots"] if s["shot_id"] == task["shot_id"])
    asset = generation.import_asset(store, task["task_id"], str(clip))
    assert asset["status"] == "pending_review"
    with pytest.raises(FlowError) as missing:
        generation.review_asset(store, asset["asset_id"], "approved", [], {})
    assert missing.value.code == "USABLE_RANGE_REQUIRED"
    review = generation.review_asset(
        store, asset["asset_id"], "approved",
        [{"start_ms": 0, "end_ms": shot["planned_ms"]}],
        {"identity": "pass", "action": "pass", "dialogue": "pass",
         "usable_footage": "pass"}, reviewer="qa")
    assert review["decision"] == "approved"


def test_old_plan_assets_are_history_and_cannot_be_approved(tmp_path, monkeypatch):
    store, snapshot, story, plan, tasks = _approved(tmp_path, monkeypatch)
    store.add_import_root(tmp_path)
    clip = tmp_path / "old.mp4"
    clip.write_bytes(b"old")
    approval.revise_plan(store, plan["plan_revision_id"],
                         [{"op": "set_title", "value": "换版本"}],
                         expected_revision=1, story=story,
                         evidence_snapshot=snapshot, policy=store.policy())
    asset = generation.import_asset(store, tasks["tasks"][0]["task_id"], str(clip))
    assert asset["usable"] is False
    with pytest.raises(FlowError) as stale:
        generation.review_asset(
            store, asset["asset_id"], "approved",
            [{"start_ms": 0, "end_ms": 3000}],
            {"identity": "pass", "action": "pass", "dialogue": "pass",
             "usable_footage": "pass"})
    assert stale.value.code == "ASSET_STALE_PLAN"


def test_timeline_uses_reviewed_ranges_and_splits_subtitles(tmp_path, monkeypatch):
    store, snapshot, story, plan, tasks = _approved(tmp_path, monkeypatch)
    store.add_import_root(tmp_path)
    for index, task in enumerate(tasks["tasks"]):
        clip = tmp_path / f"shot{index}.mp4"
        clip.write_bytes(f"clip-{index}".encode())
        asset = generation.import_asset(store, task["task_id"], str(clip))
        shot = next(s for s in plan["payload"]["shots"] if s["shot_id"] == task["shot_id"])
        generation.review_asset(
            store, asset["asset_id"], "approved",
            [{"start_ms": 0, "end_ms": shot["planned_ms"]}],
            {"identity": "pass", "action": "pass", "dialogue": "pass",
             "usable_footage": "pass"})
    result = timeline.build_timeline(store, plan["plan_revision_id"], story, snapshot,
                                     policy=store.policy())
    payload = result["payload"]
    assert payload["video_items"]
    for item in payload["video_items"]:
        assert item["source_out_ms"] > item["source_in_ms"]
        assert item["generated_asset_id"].startswith("gen_")
    for event in payload["subtitle_events"]:
        assert len(event["text"]) <= settings.policy()["subtitle"]["max_chars_per_line"]
        assert event["end_ms"] > event["start_ms"]
    validation = timeline.validate_timeline(store, payload, policy=store.policy())
    assert validation["blocking_issues"] == []
