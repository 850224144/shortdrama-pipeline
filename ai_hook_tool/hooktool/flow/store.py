"""项目级 SQLite 权威状态：版本、审批、任务、时间线、QC、交付与失效记录。

落盘顺序固定为：临时文件写入 → 哈希 → 同盘原子替换 → 事务登记。
JSON 快照不可变，只用于审阅；任何读取以数据库里的行和哈希为准。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..project_store import ProjectStore, now
from ..source_identity import atomic_json, json_hash
from .errors import FlowError


def _short(value: str, length: int = 16) -> str:
    return json_hash(value)[:length]


class FlowStore(ProjectStore):
    def __init__(self, directory: str | Path):
        super().__init__(directory)
        from ..project_store import FLOW_DDL

        self.upgrade()
        # 兼容后续追加的表：全部 IF NOT EXISTS，可重复执行。
        with self.connection() as conn:
            conn.executescript(FLOW_DDL)
        project = self.project()
        project.setdefault("stage", "INGESTED")
        self._project_cache = project

    # ── 基础 ────────────────────────────────────────────
    def _write_snapshot(self, relative: str, payload: dict) -> Path:
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise FlowError("PATH_OUTSIDE_PROJECT", relative)
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if json_hash(existing) != json_hash(payload):
                raise FlowError("SNAPSHOT_ALREADY_EXISTS", relative)
            return target
        atomic_json(target, payload)
        return target

    def _read_snapshot(self, relative: str) -> dict:
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise FlowError("PATH_OUTSIDE_PROJECT", relative)
        if not target.is_file():
            raise FlowError("SNAPSHOT_MISSING", relative)
        return json.loads(target.read_text(encoding="utf-8"))

    def set_stage(self, stage: str, reason: str | None = None) -> dict:
        return self.patch_project({"stage": stage,
                                   **({"stage_reason": reason} if reason else {})})

    def set_policy(self, policy: dict) -> dict:
        return self.patch_project({"policy": policy})

    def policy(self) -> dict:
        return dict(self.project().get("policy") or {})

    def add_import_root(self, directory: str | Path) -> dict:
        path = str(Path(directory).resolve(strict=True))
        if not Path(path).is_dir():
            raise FlowError("IMPORT_ROOT_NOT_DIRECTORY", path)
        project = self.project()
        roots = list(project.get("import_roots") or [])
        if path not in roots:
            roots.append(path)
        return self.patch_project({"import_roots": roots})

    # ── 证据 ────────────────────────────────────────────
    def register_evidence(self, run_id: str) -> dict:
        run = self.run(run_id)
        if run["status"] != "succeeded" or not run["snapshot_path"]:
            raise FlowError("RUN_NOT_SUCCEEDED", run_id)
        snapshot = self._read_snapshot(run["snapshot_path"])
        relative = run["snapshot_path"]
        payload_hash = json_hash(snapshot)
        evidence_revision_id = "ev_" + _short(payload_hash)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM evidence_revisions WHERE evidence_revision_id=?",
                (evidence_revision_id,)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO evidence_revisions VALUES(?,?,?,?,?,?,?)",
                    (evidence_revision_id, run_id, relative, payload_hash,
                     snapshot["cache_key"], snapshot["duration_ms"], now()))
            else:
                if existing["run_id"] != run_id:
                    raise FlowError("EVIDENCE_ALREADY_BOUND",
                                   "同一证据哈希已绑定其他运行记录", stage="evidence")
        return {"evidence_revision_id": evidence_revision_id, "run_id": run_id,
                "path": relative, "payload_hash": payload_hash,
                "snapshot": snapshot}

    def latest_evidence(self) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM evidence_revisions ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            raise FlowError("EVIDENCE_NOT_FOUND", "尚无证据修订，请先 analyze", stage="evidence")
        row = dict(row)
        row["snapshot"] = self._read_snapshot(row["path"])
        if json_hash(row["snapshot"]) != row["payload_hash"]:
            raise FlowError("EVIDENCE_HASH_MISMATCH", row["evidence_revision_id"], stage="evidence")
        return row

    def evidence_by_id(self, evidence_revision_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM evidence_revisions WHERE evidence_revision_id=?",
                (evidence_revision_id,)).fetchone()
        if row is None:
            raise FlowError("EVIDENCE_NOT_FOUND", evidence_revision_id, stage="evidence")
        row = dict(row)
        row["snapshot"] = self._read_snapshot(row["path"])
        if json_hash(row["snapshot"]) != row["payload_hash"]:
            raise FlowError("EVIDENCE_HASH_MISMATCH", evidence_revision_id, stage="evidence")
        return row

    # ── 剧情模型 ─────────────────────────────────────────
    def save_story(self, run_id: str, evidence_revision_id: str, story: dict) -> dict:
        relative = f"runs/{run_id}/story/story.json"
        self._write_snapshot(relative, story)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO story_models VALUES(?,?,?,?,?)",
                         (run_id, evidence_revision_id, relative, json_hash(story), now()))
        return story

    def story(self) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM story_models ORDER BY created_at DESC LIMIT 1").fetchone()
        return self._story_from_row(row)

    def story_row(self) -> dict | None:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM story_models ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def _story_from_row(self, row) -> dict:
        if row is None:
            raise FlowError("STORY_NOT_FOUND", "尚无剧情模型，请先 analyze", stage="story")
        payload = self._read_snapshot(row["path"])
        if json_hash(payload) != row["payload_hash"]:
            raise FlowError("STORY_HASH_MISMATCH", row["path"], stage="story")
        return payload

    def story_for_evidence(self, evidence_revision_id: str) -> dict | None:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM story_models WHERE evidence_revision_id=? "
                "ORDER BY created_at DESC LIMIT 1", (evidence_revision_id,)).fetchone()
        return self._story_from_row(row) if row else None

    # ── 方案 ────────────────────────────────────────────
    def save_proposals(self, run_id: str, evidence_revision_id: str,
                       proposals: list[dict]) -> list[dict]:
        rows = []
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for proposal in proposals:
                proposal_id = proposal["proposal_id"]
                relative = f"runs/{run_id}/proposals/{proposal_id}.json"
                digest = json_hash(proposal)
                self._write_snapshot(relative, proposal)
                existing = conn.execute(
                    "SELECT payload_hash FROM proposals WHERE proposal_id=?",
                    (proposal_id,)).fetchone()
                if existing is not None and existing["payload_hash"] != digest:
                    raise FlowError("PROPOSAL_ALREADY_EXISTS", proposal_id, stage="propose")
                if existing is None:
                    conn.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)",
                                 (proposal_id, run_id, evidence_revision_id,
                                  json.dumps(proposal, ensure_ascii=False),
                                  digest, now()))
                rows.append({"proposal_id": proposal_id, "payload_hash": digest,
                             "path": relative})
        return rows

    def proposals(self) -> list[dict]:
        with self.connection(readonly=True) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM proposals ORDER BY created_at DESC")]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
            if json_hash(row["payload"]) != row["payload_hash"]:
                raise FlowError("PROPOSAL_HASH_MISMATCH", row["proposal_id"], stage="propose")
        return rows

    def proposal(self, proposal_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM proposals WHERE proposal_id=?",
                               (proposal_id,)).fetchone()
        if row is None:
            raise FlowError("PROPOSAL_NOT_FOUND", proposal_id, stage="propose")
        row = dict(row)
        row["payload"] = json.loads(row["payload"])
        if json_hash(row["payload"]) != row["payload_hash"]:
            raise FlowError("PROPOSAL_HASH_MISMATCH", proposal_id, stage="propose")
        return row

    # ── 方案版本 ─────────────────────────────────────────
    def create_plan(self, proposal_id: str, payload: dict, *, semantic_hash: str,
                    parent_id: str | None = None) -> dict:
        proposal = self.proposal(proposal_id)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            last = conn.execute(
                "SELECT revision_no FROM plan_revisions WHERE proposal_id=? "
                "ORDER BY revision_no DESC LIMIT 1", (proposal_id,)).fetchone()
            revision_no = 1 if last is None else last["revision_no"] + 1
            plan_revision_id = "plan_" + _short(f"{proposal_id}|{revision_no}|{semantic_hash}")
            digest = json_hash(payload)
            relative = f"runs/{proposal['run_id']}/plans/{plan_revision_id}.json"
            self._write_snapshot(relative, payload)
            conn.execute("INSERT INTO plan_revisions VALUES(?,?,?,?,?,?,?,?,?)",
                         (plan_revision_id, proposal_id, revision_no, parent_id,
                          proposal["run_id"], json.dumps(payload, ensure_ascii=False),
                          digest, semantic_hash, now()))
        return {"plan_revision_id": plan_revision_id, "proposal_id": proposal_id,
                "revision_no": revision_no, "parent_id": parent_id,
                "run_id": proposal["run_id"], "payload_hash": digest,
                "semantic_hash": semantic_hash, "path": relative,
                "payload": payload}

    def plan(self, plan_revision_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM plan_revisions WHERE plan_revision_id=?",
                               (plan_revision_id,)).fetchone()
        if row is None:
            raise FlowError("PLAN_NOT_FOUND", plan_revision_id, stage="plan")
        row = dict(row)
        row["payload"] = json.loads(row["payload"])
        if json_hash(row["payload"]) != row["payload_hash"]:
            raise FlowError("PLAN_HASH_MISMATCH", plan_revision_id, stage="plan")
        return row

    def latest_plan(self, proposal_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT plan_revision_id FROM plan_revisions WHERE proposal_id=? "
                "ORDER BY revision_no DESC LIMIT 1", (proposal_id,)).fetchone()
        if row is None:
            raise FlowError("PLAN_NOT_FOUND", proposal_id, stage="plan")
        return self.plan(row[0])

    def find_plan(self, proposal_id: str, semantic_hash: str) -> dict | None:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT plan_revision_id FROM plan_revisions WHERE proposal_id=? "
                "AND semantic_hash=? ORDER BY revision_no DESC LIMIT 1",
                (proposal_id, semantic_hash)).fetchone()
        return self.plan(row[0]) if row else None

    def plans(self, proposal_id: str | None = None) -> list[dict]:
        sql = "SELECT plan_revision_id FROM plan_revisions"
        params: tuple = ()
        if proposal_id:
            sql += " WHERE proposal_id=?"
            params = (proposal_id,)
        sql += " ORDER BY created_at DESC"
        with self.connection(readonly=True) as conn:
            ids = [r[0] for r in conn.execute(sql, params)]
        return [self.plan(i) for i in ids]

    # ── 审批 ────────────────────────────────────────────
    def add_plan_review(self, plan_revision_id: str, *, semantic_hash: str,
                        decision: str, reviewer: str, notes: str = "",
                        approved_scope: str = "full", facts_reviewed: bool = False,
                        checks: list | None = None) -> dict:
        if decision not in ("approved", "rejected"):
            raise FlowError("INVALID_DECISION", decision, stage="review")
        review_id = "rev_" + _short(f"{plan_revision_id}|{semantic_hash}|{decision}|{now()}")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 同一版本的旧审批保留历史，但显式标记失效，避免跨决定复用。
            conn.execute(
                "UPDATE plan_reviews SET invalidated_at=?, invalidation_reason=? "
                "WHERE plan_revision_id=? AND invalidated_at IS NULL",
                (now(), "superseded_by_new_review", plan_revision_id))
            conn.execute("INSERT INTO plan_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (review_id, plan_revision_id, semantic_hash, decision,
                          reviewer, notes, approved_scope, 1 if facts_reviewed else 0,
                          now(), None, None))
        return {"review_id": review_id, "plan_revision_id": plan_revision_id,
                "semantic_hash": semantic_hash, "decision": decision,
                "reviewer": reviewer, "notes": notes, "approved_scope": approved_scope,
                "facts_reviewed": bool(facts_reviewed), "checks": checks or []}

    def plan_reviews(self, plan_revision_id: str) -> list[dict]:
        with self.connection(readonly=True) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM plan_reviews WHERE plan_revision_id=? ORDER BY created_at",
                (plan_revision_id,))]
        return rows

    def valid_plan_review(self, plan: dict) -> dict | None:
        """审批必须同时匹配版本哈希且未失效；旧审批不可跨版本复用。"""
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM plan_reviews WHERE plan_revision_id=? "
                "AND semantic_hash=? AND decision='approved' AND invalidated_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (plan["plan_revision_id"], plan["semantic_hash"])).fetchone()
        return dict(row) if row else None

    def invalidate_plan_reviews(self, plan_revision_id: str, reason: str) -> int:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                "UPDATE plan_reviews SET invalidated_at=?, invalidation_reason=? "
                "WHERE plan_revision_id=? AND invalidated_at IS NULL",
                (now(), reason, plan_revision_id))
            conn.execute("INSERT INTO invalidations(target_type,target_id,reason,created_at) "
                         "VALUES('plan_review',?,?,?)", (plan_revision_id, reason, now()))
        return result.rowcount

    # ── 制作任务与片段 ───────────────────────────────────
    def create_task(self, plan_revision_id: str, shot_id: str, payload: dict) -> dict:
        plan = self.plan(plan_revision_id)
        base = f"{plan_revision_id}|{shot_id}|{json_hash(payload)}"
        task_id = "task_" + _short(base)
        relative = f"runs/{plan['run_id']}/generation/tasks/{task_id}.json"
        self._write_snapshot(relative, payload)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT task_id FROM generation_tasks WHERE task_id=?",
                                    (task_id,)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO generation_tasks VALUES(?,?,?,?,?,?,?)",
                             (task_id, plan_revision_id, shot_id, plan["run_id"],
                              json.dumps(payload, ensure_ascii=False), "ready", now()))
            # 同版本同一镜头只保留一个任务；重复请求是幂等的。
            duplicate = conn.execute(
                "SELECT task_id FROM generation_tasks WHERE plan_revision_id=? "
                "AND shot_id=? AND task_id<>?", (plan_revision_id, shot_id, task_id)).fetchone()
            if duplicate is not None:
                raise FlowError("TASK_ALREADY_EXISTS", shot_id, stage="generation")
        return {"task_id": task_id, "plan_revision_id": plan_revision_id,
                "shot_id": shot_id, "run_id": plan["run_id"], "path": relative,
                "payload": payload, "status": "ready"}

    def task(self, task_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM generation_tasks WHERE task_id=?",
                               (task_id,)).fetchone()
        if row is None:
            raise FlowError("TASK_NOT_FOUND", task_id, stage="generation")
        row = dict(row)
        row["payload"] = json.loads(row["payload"])
        return row

    def task_for_shot(self, plan_revision_id: str, shot_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT task_id FROM generation_tasks WHERE plan_revision_id=? AND shot_id=?",
                (plan_revision_id, shot_id)).fetchone()
        if row is None:
            raise FlowError("TASK_NOT_FOUND", shot_id, stage="generation")
        return self.task(row[0])

    def tasks(self, plan_revision_id: str) -> list[dict]:
        with self.connection(readonly=True) as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT task_id FROM generation_tasks WHERE plan_revision_id=? "
                "ORDER BY created_at", (plan_revision_id,))]
        return [self.task(i) for i in ids]

    def add_generated_asset(self, task_id: str, *, path: str, sha256: str,
                            metadata: dict) -> dict:
        task = self.task(task_id)
        asset_id = "gen_" + _short(f"{task_id}|{sha256}")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            last = conn.execute("SELECT MAX(attempt) FROM generated_assets WHERE task_id=?",
                                (task_id,)).fetchone()[0]
            attempt = int(last or 0) + 1
            existing = conn.execute("SELECT * FROM generated_assets WHERE task_id=? "
                                    "AND sha256=?", (task_id, sha256)).fetchone()
            if existing is not None:
                # 同一文件重复导入是幂等的，不增加尝试次数。
                return dict(existing)
            payload = {"task_id": task_id, "plan_revision_id": task["plan_revision_id"],
                       "shot_id": task["shot_id"], "attempt": attempt,
                       "source_path": path, "sha256": sha256, "metadata": metadata}
            conn.execute("INSERT INTO generated_assets VALUES(?,?,?,?,?,?,?,?)",
                         (asset_id, task_id, attempt, path, sha256,
                          json.dumps(payload, ensure_ascii=False), "pending_review", now()))
        return {"asset_id": asset_id, "task_id": task_id, "attempt": attempt,
                "path": path, "sha256": sha256, "payload": payload,
                "status": "pending_review"}

    def generated_asset(self, asset_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM generated_assets WHERE asset_id=?",
                               (asset_id,)).fetchone()
        if row is None:
            raise FlowError("ASSET_NOT_FOUND", asset_id, stage="generation")
        row = dict(row)
        row["payload"] = json.loads(row["payload"])
        return row

    def assets_for_task(self, task_id: str) -> list[dict]:
        with self.connection(readonly=True) as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT asset_id FROM generated_assets WHERE task_id=? ORDER BY attempt",
                (task_id,))]
        return [self.generated_asset(i) for i in ids]

    def add_asset_review(self, asset_id: str, *, decision: str, usable_ranges: list,
                         checks: dict, reviewer: str | None = None) -> dict:
        if decision not in ("approved", "rejected"):
            raise FlowError("INVALID_DECISION", decision, stage="review")
        review_id = "asrev_" + _short(f"{asset_id}|{json_hash(usable_ranges)}|{decision}|{now()}")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE asset_reviews SET invalidated_at=? WHERE asset_id=? "
                         "AND invalidated_at IS NULL", (now(), asset_id))
            conn.execute("INSERT INTO asset_reviews VALUES(?,?,?,?,?,?,?,?)",
                         (review_id, asset_id, decision,
                          json.dumps(usable_ranges, ensure_ascii=False),
                          json.dumps(checks, ensure_ascii=False), reviewer, now(), None))
            conn.execute("UPDATE generated_assets SET status=? WHERE asset_id=?",
                         ("approved" if decision == "approved" else "rejected", asset_id))
        return {"review_id": review_id, "asset_id": asset_id, "decision": decision,
                "usable_ranges": usable_ranges, "checks": checks,
                "reviewer": reviewer}

    def asset_review(self, asset_id: str) -> dict | None:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM asset_reviews WHERE asset_id=? AND invalidated_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1", (asset_id,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        row["usable_ranges"] = json.loads(row["usable_ranges"])
        row["checks"] = json.loads(row["checks"])
        return row

    def invalidate_asset_reviews(self, reason: str) -> int:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute("UPDATE asset_reviews SET invalidated_at=? "
                                  "WHERE invalidated_at IS NULL", (now(),))
            conn.execute("INSERT INTO invalidations(target_type,target_id,reason,created_at) "
                         "VALUES('asset_review','all',?,?)", (reason, now()))
        return result.rowcount

    def invalidate_asset_review(self, asset_id: str, reason: str) -> int:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute("UPDATE asset_reviews SET invalidated_at=? "
                                  "WHERE asset_id=? AND invalidated_at IS NULL",
                                  (now(), asset_id))
            conn.execute("INSERT INTO invalidations(target_type,target_id,reason,created_at) "
                         "VALUES('asset_review',?,?,?)", (asset_id, reason, now()))
        return result.rowcount

    # ── 时间线 ───────────────────────────────────────────
    def create_timeline(self, plan_revision_id: str, payload: dict) -> dict:
        plan = self.plan(plan_revision_id)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            last = conn.execute(
                "SELECT revision_no FROM timeline_revisions WHERE plan_revision_id=? "
                "ORDER BY revision_no DESC LIMIT 1", (plan_revision_id,)).fetchone()
            revision_no = 1 if last is None else last["revision_no"] + 1
            digest = json_hash(payload)
            timeline_revision_id = "tl_" + _short(
                f"{plan_revision_id}|{revision_no}|{digest}")
            relative = f"runs/{plan['run_id']}/timeline/{timeline_revision_id}.json"
            self._write_snapshot(relative, payload)
            conn.execute("INSERT INTO timeline_revisions VALUES(?,?,?,?,?,?,?)",
                         (timeline_revision_id, plan_revision_id, revision_no,
                          plan["run_id"], json.dumps(payload, ensure_ascii=False),
                          digest, now()))
        return {"timeline_revision_id": timeline_revision_id,
                "plan_revision_id": plan_revision_id, "revision_no": revision_no,
                "run_id": plan["run_id"], "payload_hash": digest, "path": relative,
                "payload": payload}

    def timeline(self, timeline_revision_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM timeline_revisions WHERE timeline_revision_id=?",
                               (timeline_revision_id,)).fetchone()
        if row is None:
            raise FlowError("TIMELINE_NOT_FOUND", timeline_revision_id, stage="timeline")
        row = dict(row)
        row["payload"] = json.loads(row["payload"])
        if json_hash(row["payload"]) != row["payload_hash"]:
            raise FlowError("TIMELINE_HASH_MISMATCH", timeline_revision_id, stage="timeline")
        return row

    def latest_timeline(self, plan_revision_id: str | None = None) -> dict:
        with self.connection(readonly=True) as conn:
            if plan_revision_id:
                row = conn.execute(
                    "SELECT timeline_revision_id FROM timeline_revisions WHERE plan_revision_id=? "
                    "ORDER BY revision_no DESC LIMIT 1", (plan_revision_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT timeline_revision_id FROM timeline_revisions "
                    "ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            raise FlowError("TIMELINE_NOT_FOUND", "尚无时间线版本", stage="timeline")
        return self.timeline(row[0])

    def add_qc(self, timeline_revision_id: str, report: dict) -> dict:
        qc_id = "qc_" + _short(f"{timeline_revision_id}|{json_hash(report)}")
        relative = f"runs/{self.timeline(timeline_revision_id)['run_id']}/qc/{qc_id}.json"
        self._write_snapshot(relative, report)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT qc_id FROM qc_reports WHERE qc_id=?",
                                    (qc_id,)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO qc_reports VALUES(?,?,?,?)",
                             (qc_id, timeline_revision_id,
                              json.dumps(report, ensure_ascii=False), now()))
        return {"qc_id": qc_id, "timeline_revision_id": timeline_revision_id,
                "path": relative, "report": report}

    def latest_qc(self, timeline_revision_id: str) -> dict | None:
        with self.connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM qc_reports WHERE timeline_revision_id=? "
                "ORDER BY created_at DESC LIMIT 1", (timeline_revision_id,)).fetchone()
        if row is None:
            return None
        return {"qc_id": row["qc_id"], "timeline_revision_id": row["timeline_revision_id"],
                "report": json.loads(row["payload"]), "created_at": row["created_at"]}

    def add_delivery(self, timeline_revision_id: str, payload: dict) -> dict:
        delivery_id = "deliv_" + _short(f"{timeline_revision_id}|{json_hash(payload)}")
        relative = f"runs/{self.timeline(timeline_revision_id)['run_id']}/output/{delivery_id}.json"
        self._write_snapshot(relative, payload)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT delivery_id FROM delivery_records WHERE delivery_id=?",
                                    (delivery_id,)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO delivery_records VALUES(?,?,?,?)",
                             (delivery_id, timeline_revision_id,
                              json.dumps(payload, ensure_ascii=False), now()))
        return {"delivery_id": delivery_id, "timeline_revision_id": timeline_revision_id,
                "path": relative, "payload": payload}

    def deliveries(self, timeline_revision_id: str | None = None) -> list[dict]:
        with self.connection(readonly=True) as conn:
            if timeline_revision_id:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM delivery_records WHERE timeline_revision_id=? "
                    "ORDER BY created_at DESC", (timeline_revision_id,))]
            else:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM delivery_records ORDER BY created_at DESC")]
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    # ── 作业与失效 ───────────────────────────────────────
    def start_job(self, stage: str) -> dict:
        job_id = "job_" + _short(f"{self.context.project_id}|{stage}|{now()}")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute(
                "SELECT job_id,stage FROM job_events WHERE project_id=? AND status='running' "
                "ORDER BY created_at DESC LIMIT 1", (self.context.project_id,)).fetchone()
            if running is not None:
                raise FlowError("PROJECT_BUSY",
                                f"已有运行中的 {running['stage']} 任务", stage=stage,
                                blocking_issues=[running["job_id"]])
            conn.execute("INSERT INTO job_events(job_id,project_id,stage,status,created_at,"
                         "started_at) VALUES(?,?,?,?,?,?)",
                         (job_id, self.context.project_id, stage, "running", now(), now()))
        return {"job_id": job_id, "stage": stage, "status": "running"}

    def finish_job(self, job_id: str, status: str, payload: dict | None = None,
                   error_code: str | None = None) -> dict:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE job_events SET status=?,payload=?,finished_at=?,error_code=? "
                         "WHERE job_id=?",
                         (status, json.dumps(payload, ensure_ascii=False) if payload else None,
                          now(), error_code, job_id))
        return {"job_id": job_id, "status": status}

    def job(self, job_id: str) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM job_events WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise FlowError("JOB_NOT_FOUND", job_id, stage="jobs")
        row = dict(row)
        row["payload"] = json.loads(row["payload"]) if row["payload"] else None
        return row

    def jobs(self) -> list[dict]:
        with self.connection(readonly=True) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM job_events ORDER BY created_at DESC LIMIT 100")]
        for row in rows:
            row["payload"] = json.loads(row["payload"]) if row["payload"] else None
        return rows

    def log_invalidation(self, target_type: str, target_id: str, reason: str) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO invalidations(target_type,target_id,reason,created_at) "
                         "VALUES(?,?,?,?)", (target_type, target_id, reason, now()))

    def invalidations(self) -> list[dict]:
        with self.connection(readonly=True) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM invalidations ORDER BY id DESC LIMIT 200")]

    # ── 最终验收 ────────────────────────────────────────
    def add_final_review(self, timeline_revision_id: str, *, qc_id: str,
                         decision: str, reviewer: str, notes: str = "") -> dict:
        if decision not in ("approved", "rejected"):
            raise FlowError("INVALID_DECISION", decision, stage="review")
        review_id = "frev_" + _short(f"{timeline_revision_id}|{qc_id}|{decision}|{now()}")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE final_reviews SET invalidated_at=? "
                         "WHERE timeline_revision_id=? AND invalidated_at IS NULL",
                         (now(), timeline_revision_id))
            conn.execute("INSERT INTO final_reviews VALUES(?,?,?,?,?,?,?,?)",
                         (review_id, timeline_revision_id, qc_id, decision,
                          reviewer, notes, now(), None))
        return {"review_id": review_id, "timeline_revision_id": timeline_revision_id,
                "qc_id": qc_id, "decision": decision, "reviewer": reviewer,
                "notes": notes}

    def final_review(self, timeline_revision_id: str,
                     qc_id: str | None = None) -> dict | None:
        sql = ("SELECT * FROM final_reviews WHERE timeline_revision_id=? "
               "AND invalidated_at IS NULL")
        params: tuple = (timeline_revision_id,)
        if qc_id:
            sql += " AND qc_id=?"
            params += (qc_id,)
        sql += " ORDER BY created_at DESC LIMIT 1"
        with self.connection(readonly=True) as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def invalidate_final_reviews(self, reason: str) -> int:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute("UPDATE final_reviews SET invalidated_at=? "
                                  "WHERE invalidated_at IS NULL", (now(),))
            conn.execute("INSERT INTO invalidations(target_type,target_id,reason,created_at) "
                         "VALUES('final_review','all',?,?)", (reason, now()))
        return result.rowcount

    # ── 一致性检查 ───────────────────────────────────────
    def assert_current_source(self) -> dict:
        """首集或字幕变更后，旧证据与旧审批全部视为失效。"""
        from ..source_identity import contained_path, file_hash

        project = self.project()
        selection = project.get("selection") or {}
        if selection.get("issues") or not selection.get("episode"):
            raise FlowError("NEEDS_INPUT", "首集尚未确认", stage="source")
        watched = [(selection["episode"], "video")]
        if selection.get("transcript"):
            watched.append((selection["transcript"], "transcript"))
        for path, kind in watched:
            asset = next((a for a in (project.get("bound_assets") or [])
                          if a.get("path") == path), None)
            if not asset:
                continue
            digest = file_hash(contained_path(project["source_dir"], path))
            if digest != asset["sha256"]:
                raise FlowError("SOURCE_CHANGED",
                                f"{kind} 内容已变化，旧证据/方案/审批不可继续使用",
                                stage="source")
        return project
