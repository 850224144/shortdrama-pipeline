# -*- coding: utf-8 -*-
"""剧级档案：下载/切片状态追踪 + 防重下载检查 + 搜索。

每剧一行（drama_archive），聚合三阶段状态：
  ① 下载（download_state）  —— 用户"已下载"
  ② 切片（slice_state）     —— 用户"已切片"
  ③ 上传（upload_state）    —— 用户"已上传"，查询时从 upload_records 聚合（不冗余写）

设计决策：
- 集数完整性阈值不校验（用户确认）——防重只查 本地目录已存在 或 历史 download_state=done。
- 上传状态查询时聚合（upload_records 是版本×账号级权威来源，archive 不复制）。
- 幂等建表 + 复用 upload_manager._connect_db（WAL + busy_timeout）。
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'upload_records.db')


def _connect_db(path):
    """打开 SQLite 连接并启用 WAL + busy_timeout（与 upload_manager 同参数）。

    独立实现避免循环 import（upload_manager 依赖 processor，processor 需用 archive）。
    """
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass  # WAL 失败不致命（如只读介质），回落默认 rollback journal
    return conn


def _conn():
    return _connect_db(DB_PATH)


def init_database():
    """建 drama_archive 表 + 索引（幂等）。"""
    conn = _conn()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS drama_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drama_name TEXT NOT NULL UNIQUE,
            source_url TEXT,
            source_passwd TEXT,
            local_dir TEXT,
            rating TEXT,
            category TEXT,
            account TEXT,
            download_state TEXT DEFAULT 'none',   -- none|downloading|done|failed
            download_size INTEGER,
            download_files INTEGER,
            episode_count INTEGER,
            download_error TEXT,
            downloaded_at TIMESTAMP,
            slice_state TEXT DEFAULT 'none',      -- none|cutting|done|failed
            slice_version_count INTEGER,
            slice_error TEXT,
            sliced_at TIMESTAMP,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_archive_drama ON drama_archive(drama_name)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_archive_dl ON drama_archive(download_state)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_archive_slice ON drama_archive(slice_state)')
        conn.commit()
    finally:
        conn.close()


def _cols(conn):
    return [d[0] for d in conn.execute('SELECT * FROM drama_archive LIMIT 0').description]


def upsert_archive(drama_name, **fields):
    """按 drama_name 唯一键插入或更新。fields 为列名→值。"""
    if not fields:
        return
    conn = _conn()
    try:
        cols = list(fields.keys())
        sets = ', '.join(f'{c}=excluded.{c}' for c in cols)
        placeholders = ', '.join('?' for _ in cols)
        conn.execute(
            f'''INSERT INTO drama_archive (drama_name, {', '.join(cols)})
                VALUES (?, {placeholders})
                ON CONFLICT(drama_name) DO UPDATE SET
                    {sets}, updated_at=CURRENT_TIMESTAMP''',
            [drama_name] + [fields[c] for c in cols])
        conn.commit()
    finally:
        conn.close()


def get_archive(drama_name):
    """返回剧档案 dict（含聚合上传状态）；不存在返回 None。"""
    conn = _conn()
    try:
        row = conn.execute('SELECT * FROM drama_archive WHERE drama_name=?', (drama_name,)).fetchone()
        if not row:
            return None
        d = dict(zip(_cols(conn), row))
    finally:
        conn.close()
    up_state, up_ok, up_bad = aggregate_upload_state(drama_name)
    d['upload_state'] = up_state
    d['upload_success'] = up_ok
    d['upload_failed'] = up_bad
    return d


def should_download(drama_name, local_dir):
    """防重下载检查。返回 (是否可下载, 原因)。

    本地目录已存在 或 历史 download_state=done → 不下载。
    集数完整性不校验（用户确认）。
    """
    if os.path.isdir(local_dir):
        return False, f'本地目录已存在: {local_dir}'
    row = get_archive(drama_name)
    if row and row['download_state'] == 'done':
        return False, f'历史记录已下载成功（{row.get("downloaded_at") or ""}）'
    return True, '可下载'


def aggregate_upload_state(drama_name):
    """查询时从 upload_records 聚合上传状态。返回 (state, success_count, failed_count)。

    none（无记录）/ done（全成功）/ partial（部分成功）/ failed（全失败）。
    """
    conn = _conn()
    try:
        row = conn.execute(
            '''SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS bad
               FROM upload_records WHERE drama_name=?''', (drama_name,)).fetchone()
    except sqlite3.OperationalError:
        # upload_records 表尚未建（upload_manager 未初始化）——视为无记录
        return 'none', 0, 0
    finally:
        conn.close()
    total, ok, bad = row[0] or 0, row[1] or 0, row[2] or 0
    if total == 0:
        return 'none', 0, 0
    if bad == 0:
        return 'done', ok, 0
    if ok > 0:
        return 'partial', ok, bad
    return 'failed', 0, bad


def search_archive(q=None, download_state=None, slice_state=None, upload_state=None,
                   limit=50):
    """搜索剧档案。q 模糊匹配剧名；状态可任一/组合筛选；含聚合上传状态。

    返回按 updated_at DESC 排序的 dict 列表。
    """
    conn = _conn()
    try:
        sql = 'SELECT * FROM drama_archive WHERE 1=1'
        params = []
        if q:
            sql += ' AND drama_name LIKE ?'
            params.append(f'%{q}%')
        if download_state:
            sql += ' AND download_state=?'
            params.append(download_state)
        if slice_state:
            sql += ' AND slice_state=?'
            params.append(slice_state)
        sql += ' ORDER BY updated_at DESC LIMIT ?'
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        results = [dict(zip(_cols(conn), row)) for row in rows]
    finally:
        conn.close()
    for d in results:
        up_state, up_ok, up_bad = aggregate_upload_state(d['drama_name'])
        d['upload_state'] = up_state
        d['upload_success'] = up_ok
        d['upload_failed'] = up_bad
    if upload_state:
        results = [d for d in results if d['upload_state'] == upload_state]
    return results
