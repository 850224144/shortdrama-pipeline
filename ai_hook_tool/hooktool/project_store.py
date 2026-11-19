"""Small project-local P0 store. SQLite is authoritative; snapshots are immutable."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from . import paths
from .source_identity import atomic_json, json_hash

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSIONS = {1}

# 第一版完整流程（P1–P4）新增表：P0 的表保持原样，升级只追加，不重建。
FLOW_DDL = """
CREATE TABLE IF NOT EXISTS evidence_revisions(
    evidence_revision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    path TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS story_models(
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    evidence_revision_id TEXT NOT NULL,
    path TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(
    proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    evidence_revision_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_revisions(
    plan_revision_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    parent_id TEXT,
    run_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(proposal_id, revision_no));
CREATE TABLE IF NOT EXISTS plan_reviews(
    review_id TEXT PRIMARY KEY,
    plan_revision_id TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    notes TEXT,
    approved_scope TEXT,
    facts_reviewed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidation_reason TEXT);
CREATE TABLE IF NOT EXISTS generation_tasks(
    task_id TEXT PRIMARY KEY,
    plan_revision_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_revision_id, shot_id));
CREATE TABLE IF NOT EXISTS generated_assets(
    asset_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, sha256));
CREATE TABLE IF NOT EXISTS asset_reviews(
    review_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    usable_ranges TEXT NOT NULL,
    checks TEXT NOT NULL,
    reviewer TEXT,
    created_at TEXT NOT NULL,
    invalidated_at TEXT);
CREATE TABLE IF NOT EXISTS timeline_revisions(
    timeline_revision_id TEXT PRIMARY KEY,
    plan_revision_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_revision_id, revision_no));
CREATE TABLE IF NOT EXISTS qc_reports(
    qc_id TEXT PRIMARY KEY,
    timeline_revision_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_records(
    delivery_id TEXT PRIMARY KEY,
    timeline_revision_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS final_reviews(
    review_id TEXT PRIMARY KEY,
    timeline_revision_id TEXT NOT NULL,
    qc_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    invalidated_at TEXT);
CREATE TABLE IF NOT EXISTS job_events(
    job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT);
CREATE TABLE IF NOT EXISTS invalidations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WorkspaceContext:
    project_id: str
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "project.sqlite"

    def run_dir(self, run_id: str) -> Path:
        # Never use external path fragments as run IDs.
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError("INVALID_RUN_ID")
        return self.root / "runs" / run_id


class ProjectStore:
    def __init__(self, directory: str | Path):
        self.root = Path(directory).resolve(strict=True)
        database = self.root / "project.sqlite"
        if not database.is_file() or database.is_symlink():
            raise ValueError("PROJECT_DATABASE_MISSING")
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='project'").fetchone()
            if row is None:
                raise ValueError("INVALID_PROJECT")
            project = json.loads(row[0])
        if project.get("schema_version") not in ({SCHEMA_VERSION} | LEGACY_SCHEMA_VERSIONS):
            raise ValueError("UNSUPPORTED_PROJECT_SCHEMA")
        self.context = WorkspaceContext(project["project_id"], self.root)

    @contextmanager
    def connection(self, readonly=False):
        uri = (self.root / "project.sqlite").as_uri() + ("?mode=ro" if readonly else "?mode=rw")
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            if not readonly:
                conn.commit()
        except BaseException:
            if not readonly:
                conn.rollback()
            raise
        finally:
            conn.close()

    @classmethod
    def create(cls, source_dir: str, discovery: dict, selection: dict,
               projects_root: str | Path | None = None):
        project_id = str(uuid.uuid4())
        root = Path(projects_root or Path(paths.WORKDIR) / "projects") / project_id
        root.mkdir(parents=True, exist_ok=False)
        project = {"schema_version": SCHEMA_VERSION, "project_id": project_id,
                   "stage": "INGESTED",
                   "source_dir": str(Path(source_dir).resolve(strict=True)),
                   "created_at": now(), "revision": 1,
                   "status": "NEEDS_INPUT" if selection["issues"] else "READY",
                   "discovery": discovery, "selection": selection}
        conn = sqlite3.connect(root / "project.sqlite")
        try:
            conn.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE assets(asset_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE runs(run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    input_json TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, error_json TEXT, snapshot_path TEXT,
                    snapshot_hash TEXT);
                CREATE UNIQUE INDEX one_running_job ON runs(status) WHERE status='running';
                CREATE TABLE evidence_snapshots(run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    path TEXT NOT NULL, sha256 TEXT NOT NULL, cache_key TEXT NOT NULL);
            """ + FLOW_DDL)
            conn.execute("INSERT INTO metadata VALUES ('project', ?)",
                         (json.dumps(project, ensure_ascii=False),))
            conn.commit()
        finally:
            conn.close()
        atomic_json(root / "project.json", project)
        return cls(root)

    def upgrade(self) -> dict:
        """v1 → v2 追加新表；旧表与旧产物保持只读兼容，不做重写。"""
        project = self.project()
        if project.get("schema_version") == SCHEMA_VERSION:
            return project
        with self.connection() as conn:
            # executescript 会先提交挂起事务；DDL 全部 IF NOT EXISTS，可重复执行。
            conn.executescript(FLOW_DDL)
            project["schema_version"] = SCHEMA_VERSION
            project.setdefault("stage", "INGESTED")
            conn.execute("UPDATE metadata SET value=? WHERE key='project'",
                         (json.dumps(project, ensure_ascii=False),))
        atomic_json(self.root / "project.json", project)
        return project

    def patch_project(self, patch: dict, expected_revision: int | None = None) -> dict:
        """受控更新；传 expected_revision 时做乐观并发校验并递增内容版本。"""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = json.loads(conn.execute(
                "SELECT value FROM metadata WHERE key='project'").fetchone()[0])
            if expected_revision is not None and project["revision"] != expected_revision:
                raise ValueError("REVISION_CONFLICT")
            if expected_revision is not None:
                project["revision"] = expected_revision + 1
            project.update(patch)
            conn.execute("UPDATE metadata SET value=? WHERE key='project'",
                         (json.dumps(project, ensure_ascii=False),))
        atomic_json(self.root / "project.json", project)
        return project

    def project(self) -> dict:
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='project'").fetchone()
        return json.loads(row[0])

    def confirm(self, discovery: dict, selection: dict, expected_revision: int) -> dict:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise ValueError("PROJECT_BUSY: 等待当前准备任务结束")
            project = json.loads(conn.execute(
                "SELECT value FROM metadata WHERE key='project'").fetchone()[0])
            if project["revision"] != expected_revision:
                raise ValueError("REVISION_CONFLICT")
            project.update(revision=expected_revision + 1, discovery=discovery,
                           selection=selection,
                           status="NEEDS_INPUT" if selection["issues"] else "READY")
            # User confirmation establishes a new input version; old runs remain historical.
            project.pop("bound_assets", None)
            conn.execute("UPDATE metadata SET value=? WHERE key='project'",
                         (json.dumps(project, ensure_ascii=False),))
        atomic_json(self.root / "project.json", project)
        return project

    def begin_run(self, expected_revision: int) -> tuple[str, dict]:
        run_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = json.loads(conn.execute(
                "SELECT value FROM metadata WHERE key='project'").fetchone()[0])
            if project["revision"] != expected_revision:
                raise ValueError("REVISION_CONFLICT")
            if conn.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise ValueError("PROJECT_BUSY: 已有运行中任务，不自动接管")
            conn.execute("INSERT INTO runs(run_id,status,input_json,started_at) VALUES(?,?,?,?)",
                         (run_id, "running", json.dumps(project, ensure_ascii=False), now()))
        return run_id, project

    def finish_run(self, run_id: str, *, snapshot: dict | None = None,
                   error: dict | None = None, assets: list[dict] | None = None) -> None:
        target = self.context.run_dir(run_id) / "evidence" / "evidence.json"
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row[0] != "running":
            raise ValueError("RUN_NOT_RUNNING")
        if snapshot is not None:
            if target.exists():
                raise ValueError("SNAPSHOT_ALREADY_EXISTS")
            atomic_json(target, snapshot)
        relative = str(target.relative_to(self.root)) if snapshot is not None else None
        digest = json_hash(snapshot) if snapshot is not None else None
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                "UPDATE runs SET status=?,finished_at=?,error_json=?,snapshot_path=?,"
                "snapshot_hash=? WHERE run_id=? AND status='running'",
                ("succeeded" if snapshot is not None else "failed", now(),
                 json.dumps(error, ensure_ascii=False) if error else None,
                 relative, digest, run_id))
            if result.rowcount != 1:
                raise ValueError("RUN_STATE_CHANGED")
            if snapshot is not None:
                conn.execute("INSERT INTO evidence_snapshots VALUES(?,?,?,?)",
                             (run_id, relative, digest, snapshot["cache_key"]))
                for asset in assets or []:
                    conn.execute("INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?)",
                                 (asset["asset_id"], asset["kind"], asset["path"], asset["sha256"], now()))
                project = json.loads(conn.execute(
                    "SELECT value FROM metadata WHERE key='project'").fetchone()[0])
                project["bound_assets"] = assets or []
                conn.execute("UPDATE metadata SET value=? WHERE key='project'",
                             (json.dumps(project, ensure_ascii=False),))

    def interrupt_run(self, run_id: str) -> None:
        """Explicit operator action only, after confirming no worker still owns it."""
        self.context.run_dir(run_id)
        with self.connection() as conn:
            result = conn.execute(
                "UPDATE runs SET status='interrupted',finished_at=? WHERE run_id=? AND status='running'",
                (now(), run_id))
            if result.rowcount != 1:
                raise ValueError("RUN_NOT_RUNNING")

    def runs(self) -> list[dict]:
        with self.connection(readonly=True) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC")]

    def run(self, run_id: str) -> dict:
        self.context.run_dir(run_id)
        with self.connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("RUN_NOT_FOUND")
        return dict(row)
