# -*- coding: utf-8 -*-
"""审批：版本绑定、编辑失效、制作门禁与退回代码。"""
import pytest

from flow_support import (RUN_ID, build_story_and_proposals, make_project,
                          plan_for)
from hooktool.flow import approval, settings
from hooktool.flow.errors import FlowError


def _setup(tmp_path):
    store = make_project(tmp_path)
    snapshot, story, result = build_story_and_proposals(store)
    proposal = result["proposals"][0]
    plan = plan_for(store, proposal, snapshot, story)
    return store, snapshot, story, proposal, plan


def test_approval_binds_revision_and_semantic_hash(tmp_path):
    store, snapshot, story, proposal, plan = _setup(tmp_path)
    review = approval.review_plan(store, plan["plan_revision_id"], "approved",
                                  reviewer="qa", facts_reviewed=True,
                                  story=story, evidence_snapshot=snapshot,
                                  policy=store.policy())
    assert review["decision"] == "approved"
    assert review["semantic_hash"] == plan["semantic_hash"]
    assert review["facts_reviewed"] == 1
    assert approval.assert_production_allowed(
        store, plan["plan_revision_id"], story, snapshot,
        policy=store.policy())["review"]["review_id"] == review["review_id"]


def test_semantic_edit_invalidates_previous_approval(tmp_path):
    store, snapshot, story, proposal, plan = _setup(tmp_path)
    approval.review_plan(store, plan["plan_revision_id"], "approved",
                         reviewer="qa", facts_reviewed=True,
                         story=story, evidence_snapshot=snapshot,
                         policy=store.policy())
    edited = approval.revise_plan(
        store, plan["plan_revision_id"],
        [{"op": "set_answers", "answers": {"ending_question": "他会不会签？"}}],
        expected_revision=1, story=story, evidence_snapshot=snapshot,
        policy=store.policy())
    assert edited["revision_no"] == 2
    assert edited["semantic_hash"] != plan["semantic_hash"]
    with pytest.raises(FlowError) as conflict:
        approval.review_plan(store, plan["plan_revision_id"], "approved",
                            reviewer="qa", facts_reviewed=True,
                            story=story, evidence_snapshot=snapshot,
                            policy=store.policy())
    assert conflict.value.code == "PLAN_NOT_CURRENT"
    with pytest.raises(FlowError) as gate:
        approval.assert_production_allowed(store, edited["plan_revision_id"],
                                           story, snapshot, policy=store.policy())
    assert gate.value.code == "PLAN_NOT_APPROVED"


def test_cosmetic_edit_can_keep_semantics(tmp_path):
    store, snapshot, story, proposal, plan = _setup(tmp_path)
    edited = approval.revise_plan(store, plan["plan_revision_id"],
                                  [{"op": "set_title", "value": "改名不改剧情"}],
                                  expected_revision=1, story=story,
                                  evidence_snapshot=snapshot, policy=store.policy())
    assert edited["semantic_hash"] == plan["semantic_hash"]
    assert edited["revision_no"] == 2


def test_stale_revision_is_rejected(tmp_path):
    store, snapshot, story, proposal, plan = _setup(tmp_path)
    with pytest.raises(FlowError) as conflict:
        approval.revise_plan(store, plan["plan_revision_id"],
                             [{"op": "set_title", "value": "旧版本"}],
                             expected_revision=99, story=story,
                             evidence_snapshot=snapshot, policy=store.policy())
    assert conflict.value.code == "REVISION_CONFLICT"


def test_rejection_requires_known_reason_code(tmp_path):
    store, snapshot, story, proposal, plan = _setup(tmp_path)
    with pytest.raises(FlowError) as invalid:
        approval.review_plan(store, plan["plan_revision_id"], "rejected",
                             reviewer="qa", reason_code="BORED",
                             story=story, evidence_snapshot=snapshot,
                             policy=store.policy())
    assert invalid.value.code == "INVALID_REJECTION_CODE"
    review = approval.review_plan(store, plan["plan_revision_id"], "rejected",
                                  reviewer="qa", reason_code="OPENING_WEAK",
                                  story=story, evidence_snapshot=snapshot,
                                  policy=store.policy())
    assert review["decision"] == "rejected"


def test_blocked_plan_still_persists_for_trace(tmp_path):
    store = make_project(tmp_path)
    snapshot, story, result = build_story_and_proposals(store)
    proposal = result["proposals"][0]
    proposal = dict(proposal)
    proposal["proposal_id"] = "prop_blocked_test"
    proposal["shots"] = [dict(proposal["shots"][0], planned_ms=1)]
    proposal["planned_duration_ms"] = 1
    store.save_proposals(RUN_ID, "ev_blocked", [proposal])
    plan = plan_for(store, proposal, snapshot, story)
    assert plan["blocked"] is True
    assert store.plan(plan["plan_revision_id"])["payload"]["blocked"] is True
