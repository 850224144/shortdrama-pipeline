# -*- coding: utf-8 -*-
"""候选计划人工审核关口（定稿 P2）回归。"""
import argparse
import copy
import os

from hooktool import cli, manifest as mf, review


def _manifest():
    return {
        "drama_name": "测试剧",
        "source_episode": "1.mp4",
        "episode_label": "E01",
        "segments": [
            {"segment": mf.SEG_PRE, "shots": [
                {"shot": 1, "candidate_id": "asr:0", "subtitle": "开场台词",
                 "source_time": {"start": 0.0, "end": 3.0},
                 "conflict_score": 3.5, "conflict_percentile": 92,
                 "emotion": "愤怒", "opening_hook": True, "planning_hint": {}},
                {"shot": 2, "candidate_id": "asr:4", "subtitle": "第二镜台词",
                 "source_time": {"start": 12.0, "end": 15.0},
                 "conflict_score": 2.8, "emotion": "紧张",
                 "planning_hint": {"role_in_hook": "escalation",
                                   "why_it_hooks": "矛盾升级"}},
            ]},
            {"segment": mf.SEG_HOOK, "shots": [
                {"shot": 1, "candidate_id": "asr:9", "subtitle": "后钩子悬念",
                 "source_time": {"start": 61.0, "end": 64.0},
                 "conflict_score": 4.1, "is_cliffhanger": True,
                 "planning_hint": {}},
            ]},
        ],
        "status": "draft",
        "warns": [],
    }


def test_prepare_persists_candidates_reason_and_slot_map():
    man = _manifest()
    rv = review.prepare(man)
    assert rv["status"] == review.STATUS_PENDING
    assert rv["revision"] == 1
    assert len(rv["candidates"]) == 3
    rows = rv["candidates"]
    assert rows[0]["slot"] == f"{mf.SEG_PRE}/1.mp4"
    assert rows[2]["slot"] == f"{mf.SEG_HOOK}/1.mp4"
    assert "规则冲突分 3.5" in rows[0]["selection_reason"]
    assert "开场 3 秒钩子" in rows[0]["selection_reason"]
    assert "LLM 理由：矛盾升级" in rows[1]["selection_reason"]
    assert rows[2]["candidate_id"] == "asr:9"


def test_pending_blocks_collect_and_assemble(tmp_path, monkeypatch):
    man = _manifest()
    man["review"] = review.prepare(man)
    block = review.approval_block_reason(man)
    assert block and "待人工审核" in block

    monkeypatch.setattr(cli.mf, "load", lambda *a, **k: copy.deepcopy(man))
    monkeypatch.setattr(cli.mf, "save", lambda *a, **k: None)
    monkeypatch.setattr(cli.collect, "collect", lambda m: (_ for _ in ()).throw(
        AssertionError("未审核不得收集片段")))
    assert cli._cmd_collect(argparse.Namespace()) == 1

    monkeypatch.setattr(cli.assemble, "run_assemble", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("未审核不得拼接")))
    assert cli._cmd_assemble(argparse.Namespace(badge_text=None)) == 1


def test_approve_unblocks_collect(tmp_path, monkeypatch):
    man = _manifest()
    man["review"] = review.prepare(man)
    rv, err = review.decide(man, "approve", note="同意", reviewer="tester")
    assert err == ""
    assert rv["status"] == review.STATUS_APPROVED
    assert rv["decided_by"] == "tester" and rv["note"] == "同意"
    assert review.approval_block_reason(man) == ""

    collected = {"clips": [{"segment": mf.SEG_PRE, "shot": 1, "path": "a.mp4"}],
                 "missing": [], "warns": [], "pending": [], "hard_block": []}
    monkeypatch.setattr(cli.mf, "load", lambda *a, **k: copy.deepcopy(man))
    monkeypatch.setattr(cli.mf, "save", lambda *a, **k: None)
    monkeypatch.setattr(cli.collect, "collect", lambda m, **k: collected)
    assert cli._cmd_collect(argparse.Namespace()) == 0


def test_reject_blocks_with_note():
    man = _manifest()
    man["review"] = review.prepare(man)
    rv, err = review.decide(man, "reject", note="开场不够狠，重挑")
    assert err == ""
    block = review.approval_block_reason(man)
    assert "退回" in block and "开场不够狠" in block


def test_rerun_script_invalidates_previous_approval():
    man = _manifest()
    review.prepare(man)
    review.decide(man, "approve", note="第一次批准")
    assert review.is_approved(man)

    review.prepare(man)  # 重跑 script
    assert man["review"]["revision"] == 2
    assert man["review"]["status"] == review.STATUS_PENDING
    assert not review.is_approved(man), "新版本清单必须重新审核"
    assert any(h["action"] == "approve" for h in man["review"]["history"])


def test_manifest_edit_after_approval_requires_review():
    man = _manifest()
    review.prepare(man)
    review.decide(man, "approve")
    man["segments"][0]["shots"][0]["subtitle"] = "被手改过的台词"
    block = review.approval_block_reason(man)
    assert "不一致" in block


def test_approve_empty_candidates_refused():
    man = {"drama_name": "空", "segments": [{"segment": mf.SEG_PRE, "shots": []}]}
    man["review"] = review.prepare(man)
    assert man["review"]["status"] == review.STATUS_PENDING
    rv, err = review.decide(man, "approve")
    assert err and "为空" in err
    assert rv["status"] == review.STATUS_PENDING


def test_cli_review_command_export(tmp_path, monkeypatch):
    man = _manifest()
    man["review"] = review.prepare(man)
    monkeypatch.setattr(cli.mf, "load", lambda *a, **k: copy.deepcopy(man))
    monkeypatch.setattr(cli.mf, "save", lambda *a, **k: None)
    out = str(tmp_path / "审核单.md")
    rc = cli._cmd_review(argparse.Namespace(
        action="approve", note="ok", reviewer="me", export=out))
    assert rc == 0
    assert os.path.exists(out)
    text = open(out, encoding="utf-8").read()
    assert "钩子候选审核单" in text and "前核15s/1.mp4" in text


def test_parser_has_review_subcommand():
    parser = cli.build_parser()
    args = parser.parse_args(["review", "--action", "approve"])
    assert args.func is cli._cmd_review
    assert args.action == "approve"
