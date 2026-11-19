# -*- coding: utf-8 -*-
"""存储：schema 升级、乐观并发、作业互斥与失效记录。"""
import json
import sqlite3

import pytest

from flow_support import make_project
from hooktool.flow.errors import FlowError
from hooktool.flow.store import FlowStore
from hooktool.project_store import ProjectStore


def test_legacy_schema_is_upgraded_in_place(tmp_path):
    store = make_project(tmp_path)
    database = store.root / "project.sqlite"
    with sqlite3.connect(database) as conn:
        project = json.loads(conn.execute(
            "SELECT value FROM metadata WHERE key='project'").fetchone()[0])
        project["schema_version"] = 1
        conn.execute("UPDATE metadata SET value=? WHERE key='project'",
                     (json.dumps(project),))
    upgraded = FlowStore(store.root)
    assert upgraded.project()["schema_version"] == 2
    with upgraded.connection(readonly=True) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"plan_revisions", "generated_assets", "timeline_revisions",
            "qc_reports", "job_events", "invalidations"} <= tables


def test_optimistic_concurrency_uses_revision(tmp_path):
    store = make_project(tmp_path)
    revision = store.project()["revision"]
    store.patch_project({"note": "first"}, expected_revision=revision)
    with pytest.raises(ValueError) as conflict:
        store.patch_project({"note": "stale"}, expected_revision=revision)
    assert "REVISION_CONFLICT" in str(conflict.value)


def test_jobs_are_mutually_exclusive_per_project(tmp_path):
    store = make_project(tmp_path)
    first = store.start_job("analyze")
    with pytest.raises(FlowError) as busy:
        store.start_job("propose")
    assert busy.value.code == "PROJECT_BUSY"
    store.finish_job(first["job_id"], "succeeded", {"ok": True})
    second = store.start_job("propose")
    assert second["job_id"] != first["job_id"]


def test_jobs_are_visible_and_cancellable(tmp_path):
    store = make_project(tmp_path)
    job = store.start_job("render")
    assert store.job(job["job_id"])["status"] == "running"
    assert [row["job_id"] for row in store.jobs()] == [job["job_id"]]
    store.finish_job(job["job_id"], "cancelled", {"note": "cooperative"})
    assert store.job(job["job_id"])["status"] == "cancelled"


def test_invalidations_are_append_only(tmp_path):
    store = make_project(tmp_path)
    store.log_invalidation("plan_review", "plan_x", "edited")
    store.log_invalidation("plan_review", "plan_x", "source_changed")
    rows = store.invalidations()
    assert [row["reason"] for row in rows[:2]] == ["source_changed", "edited"]
