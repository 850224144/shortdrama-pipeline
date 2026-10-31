# -*- coding: utf-8 -*-
"""持久化任务库：jobs + job_events。

所有长任务（批量切片、后续主处理、上传）共享同一个任务存储，作为权威状态源。
JSON/文件标记是兼容层，数据库是恢复现场的唯一依据。

状态机约定（status 字段自由文本，本模块不强制枚举）：
  pending -> cutting -> completed | failed
  停止/暂停通过 cancel_requested + 独立事件表达，不写入业务结果状态；
  被停止的任务保持 pending，重启后可继续。
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _dumps(obj):
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


def _loads(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


class JobStore:
    """SQLite 任务与事件存储。

    - 文件路径：每次操作新建连接（WAL + busy_timeout），与 upload.db/archive 一致；
    - ':memory:'：多线程共享单个连接 + 锁，避免多连接各自为独立内存库的陷阱。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._mem_conn = None
        self._mem_lock = threading.Lock()
        if db_path == ':memory:':
            self._mem_conn = self._connect()
        self._init_db()

    # ── 连接 ───────────────────────────────────────────────
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=5000')
        except Exception:
            pass  # WAL 失败不致命
        return conn

    @contextmanager
    def _conn(self):
        if self._mem_conn is not None:
            with self._mem_lock:
                try:
                    yield self._mem_conn
                    self._mem_conn.commit()
                except Exception:
                    self._mem_conn.rollback()
                    raise
        else:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_db(self):
        if self.db_path != ':memory:':
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  job_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  stage TEXT,
                  progress INTEGER DEFAULT 0,
                  params_json TEXT,
                  result_json TEXT,
                  error TEXT,
                  cancel_requested INTEGER DEFAULT 0,
                  created_at TEXT,
                  updated_at TEXT,
                  started_at TEXT,
                  finished_at TEXT,
                  heartbeat_at TEXT
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS job_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id TEXT NOT NULL,
                  level TEXT,
                  event TEXT,
                  message TEXT,
                  metadata_json TEXT,
                  created_at TEXT
                )
                '''
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_jobs_type_status '
                'ON jobs (job_type, status)'
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_events_job '
                'ON job_events (job_id, id)'
            )
            # 显式列迁移：老库无 heartbeat_at 列则补上（幂等）
            try:
                conn.execute('ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT')
            except sqlite3.OperationalError:
                pass  # 列已存在

    # ── 行映射 ─────────────────────────────────────────────
    @staticmethod
    def _row_to_job(row):
        d = dict(row)
        d['params'] = _loads(d.pop('params_json', None))
        d['result'] = _loads(d.pop('result_json', None))
        d['cancel_requested'] = bool(d.get('cancel_requested'))
        return d

    # ── 任务写入 ───────────────────────────────────────────
    def put(self, job):
        """插入或整体覆盖一个任务（保留已存在的 started_at/finished_at）。"""
        now = job.get('updated_at') or _now()
        with self._conn() as conn:
            conn.execute(
                '''
                INSERT INTO jobs (id, job_type, status, stage, progress,
                                  params_json, result_json, error,
                                  cancel_requested, created_at, updated_at,
                                  started_at, finished_at, heartbeat_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  job_type=excluded.job_type,
                  status=excluded.status,
                  stage=excluded.stage,
                  progress=excluded.progress,
                  params_json=excluded.params_json,
                  result_json=excluded.result_json,
                  error=excluded.error,
                  cancel_requested=excluded.cancel_requested,
                  updated_at=excluded.updated_at,
                  started_at=COALESCE(excluded.started_at, jobs.started_at),
                  finished_at=COALESCE(excluded.finished_at, jobs.finished_at),
                  heartbeat_at=COALESCE(excluded.heartbeat_at, jobs.heartbeat_at)
                ''',
                (
                    job['id'],
                    job.get('job_type', 'cut'),
                    job.get('status', 'pending'),
                    job.get('stage'),
                    job.get('progress', 0),
                    _dumps(job.get('params')),
                    _dumps(job.get('result')),
                    job.get('error'),
                    1 if job.get('cancel_requested') else 0,
                    job.get('created_at') or now,
                    now,
                    job.get('started_at'),
                    job.get('finished_at'),
                    job.get('heartbeat_at'),
                ),
            )

    def update(self, job_id, **fields):
        """局部更新指定字段（自动刷新 updated_at）。"""
        allowed = {'status', 'stage', 'progress', 'error',
                   'cancel_requested', 'started_at', 'finished_at',
                   'params', 'result', 'job_type', 'heartbeat_at'}
        updates = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f'非法任务字段: {key}')
            if key in ('params', 'result'):
                updates.append(f'{key}_json=?')
                values.append(_dumps(value))
            elif key == 'cancel_requested':
                updates.append('cancel_requested=?')
                values.append(1 if value else 0)
            elif key in ('progress',):
                updates.append('progress=?')
                values.append(int(value or 0))
            else:
                updates.append(f'{key}=?')
                values.append(value)
        if not updates:
            return
        updates.append('updated_at=?')
        values.append(_now())
        values.append(job_id)
        with self._conn() as conn:
            conn.execute(
                f'UPDATE jobs SET {", ".join(updates)} WHERE id=?',
                values,
            )

    def touch_heartbeat(self, job_id):
        """刷新任务心跳（进程存活信号）。"""
        self.update(job_id, heartbeat_at=_now())

    def stale_jobs(self, job_type='cut', statuses=('cutting',), window_seconds=30):
        """返回心跳过期（或从未心跳）的进行中任务——判为僵死。

        心跳以 heartbeat_at 为准；无心跳列（老数据）回退 updated_at。
        """
        import datetime as _dt
        now = _dt.datetime.now()
        cutoff = (now - _dt.timedelta(seconds=window_seconds)).strftime(
            '%Y-%m-%d %H:%M:%S')
        placeholders = ','.join('?' for _ in statuses)
        query = (
            "SELECT * FROM jobs WHERE job_type=? AND status IN ("
            + placeholders + ') AND (heartbeat_at IS NULL OR heartbeat_at < ?)'
        )
        values = [job_type] + list(statuses) + [cutoff]
        with self._conn() as conn:
            rows = conn.execute(query, values).fetchall()
        return [self._row_to_job(r) for r in rows]

    def get(self, job_id):
        with self._conn() as conn:
            row = conn.execute(
                'SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list(self, job_type='cut', statuses=None):
        """按类型列出任务，可选按状态集合过滤。"""
        query = 'SELECT * FROM jobs WHERE job_type=?'
        values = [job_type]
        if statuses:
            placeholders = ','.join('?' for _ in statuses)
            query += f' AND status IN ({placeholders})'
            values.extend(statuses)
        query += ' ORDER BY created_at, id'
        with self._conn() as conn:
            rows = conn.execute(query, values).fetchall()
        return [self._row_to_job(r) for r in rows]

    def delete(self, job_id):
        with self._conn() as conn:
            conn.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            conn.execute('DELETE FROM job_events WHERE job_id=?', (job_id,))

    def delete_by_status(self, job_type, statuses):
        """删除某类型下所有指定状态的任务及其事件。"""
        placeholders = ','.join('?' for _ in statuses)
        with self._conn() as conn:
            rows = conn.execute(
                f'SELECT id FROM jobs WHERE job_type=? AND status IN ({placeholders})',
                [job_type] + list(statuses)).fetchall()
            ids = [r['id'] for r in rows]
            if ids:
                id_marks = ','.join('?' for _ in ids)
                conn.execute(f'DELETE FROM jobs WHERE id IN ({id_marks})', ids)
                conn.execute(
                    f'DELETE FROM job_events WHERE job_id IN ({id_marks})', ids)
        return len(ids)

    # ── 事件 ───────────────────────────────────────────────
    def add_event(self, job_id, level, event, message, metadata=None):
        with self._conn() as conn:
            conn.execute(
                '''
                INSERT INTO job_events (job_id, level, event, message,
                                        metadata_json, created_at)
                VALUES (?,?,?,?,?,?)
                ''',
                (job_id or '', level, event, message,
                 _dumps(metadata), _now()),
            )

    def events(self, job_id=None, limit=100):
        query = 'SELECT * FROM job_events'
        values = []
        if job_id is not None:
            query += ' WHERE job_id=?'
            values.append(job_id or '')
        query += ' ORDER BY id DESC LIMIT ?'
        values.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, values).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['metadata'] = _loads(d.pop('metadata_json', None))
            result.append(d)
        result.reverse()  # 时间正序返回
        return result
