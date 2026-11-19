# -*- coding: utf-8 -*-
"""剧级档案：下载/切片状态追踪 + 防重下载检查 + 搜索。

每剧一行（drama_archive），聚合三阶段状态：
  ① 下载（download_state）  —— 用户"已下载"
  ② 切片（slice_state）     —— 用户"已切片"
  ③ 上传（upload_state）    —— 用户"已上传"，查询时从 upload_records 聚合（不冗余写）

设计决策：
- 集数完整性阈值不校验（用户确认）——防重只查 本地目录已存在 或 历史 download_state=done。
- 上传状态查询时聚合（upload_records 是版本×账号级权威来源，archive 不复制）。
- 幂等建表并直接落在上传库 upload.db，确保 upload_records 聚合可用。
"""
import os
import re
import sqlite3

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'upload.db')
LEGACY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'upload_records.db')
DB_PATH = DEFAULT_DB_PATH

ARCHIVE_COLUMNS = (
    'id', 'drama_name', 'source_url', 'source_passwd', 'local_dir', 'rating',
    'category', 'account', 'download_state', 'download_size', 'download_files',
    'episode_count', 'download_error', 'downloaded_at', 'slice_state',
    'slice_version_count', 'slice_error', 'sliced_at', 'notes', 'created_at',
    'updated_at', 'source_key',
)

_LINK_ID_RE = re.compile(r'/s/([0-9A-Za-z_\-]+)')


def extract_link_id(source_url):
    """从网盘分享链接取分享码；取不到返回 None。"""
    m = _LINK_ID_RE.search(str(source_url or ''))
    return m.group(1) if m else None


def source_key_for(source_url=None, drama_name=None):
    """档案行的来源唯一键：link:<分享码> / manual:<剧名>。

    为什么要唯一键：只按 drama_name 做主键时，下载失败路径用分享码派生名
    会写出一条重复档案行（2026-11-19 事故：1txjKms… 这类分享码行），真实
    剧名行的 slice_state 永远停在 none，连带完整性闸门与防重全部误判。
    """
    link_id = extract_link_id(source_url)
    if link_id:
        return f'link:{link_id}'
    name = str(drama_name or '').strip()
    return f'manual:{name}' if name else None


def _connect_db(path):
    """打开 SQLite 连接并启用 WAL + busy_timeout（与 upload_manager 同参数）。

    独立实现避免循环 import（upload_manager 依赖 processor，processor 需用 archive）。
    """
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass  # WAL 失败不致命（如只读介质），回落默认 rollback journal
    return conn


def _conn():
    return _connect_db(DB_PATH)


def _looks_like_link_id_name(name):
    """分享码形态（纯字母数字/下划线/短横线、长度 ≥20）→ 不是剧名。"""
    return bool(re.fullmatch(r'[0-9A-Za-z_\-]{20,}', str(name or '')))


_SLICE_RANK = {'done': 3, 'cutting': 2, 'failed': 1, 'none': 0}
_DL_RANK = {'done': 2, 'downloading': 1, 'failed': 1, 'none': 0}
_MERGE_COLS = ('local_dir', 'rating', 'category', 'account', 'download_size',
               'download_files', 'episode_count', 'downloaded_at', 'sliced_at',
               'slice_version_count', 'notes', 'source_passwd', 'download_error',
               'slice_error')


def _migrate_source_key(conn):
    """加 source_key 列 → 回填 → 合并同来源重复行 → 建唯一索引（幂等）。

    合并规则：同 source_key 多行时保留“切片状态最完整”的一行（done 优先），
    其余行的非空字段补进保留行后删除，避免历史分享码行继续污染统计与防重。
    """
    try:
        conn.execute('ALTER TABLE drama_archive ADD COLUMN source_key TEXT')
    except sqlite3.OperationalError:
        pass  # 列已存在
    for row in conn.execute(
            'SELECT id, drama_name, source_url FROM drama_archive '
            'WHERE source_key IS NULL').fetchall():
        key = source_key_for(row['source_url'], row['drama_name'])
        if key:
            conn.execute('UPDATE drama_archive SET source_key=? WHERE id=?',
                         (key, row['id']))
    groups = conn.execute(
        'SELECT source_key FROM drama_archive WHERE source_key IS NOT NULL '
        'GROUP BY source_key HAVING COUNT(*) > 1').fetchall()
    for grp in groups:
        key = grp[0]
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM drama_archive WHERE source_key=?', (key,)).fetchall()]
        keep = max(rows, key=lambda r: (
            _SLICE_RANK.get(r.get('slice_state') or 'none', 0),
            _DL_RANK.get(r.get('download_state') or 'none', 0),
            1 if r.get('source_url') else 0,
            r['id']))
        for r in rows:
            if r['id'] == keep['id']:
                continue
            fields = {c: r[c] for c in _MERGE_COLS
                      if keep.get(c) in (None, '') and r.get(c) not in (None, '')}
            if fields:
                sets = ', '.join(f'{c}=?' for c in fields)
                conn.execute(
                    f'UPDATE drama_archive SET {sets}, '
                    f'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                    list(fields.values()) + [keep['id']])
            conn.execute('DELETE FROM drama_archive WHERE id=?', (r['id'],))
    try:
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_archive_source_key '
                     'ON drama_archive(source_key) WHERE source_key IS NOT NULL')
    except sqlite3.OperationalError as e:
        print(f'WARN: source_key 唯一索引创建失败（回退非唯一）：{e}')


def migrate_legacy_archive(target_path=None, legacy_path=None):
    """把旧 webapp/upload_records.db 的历史档案迁入统一上传库。

    仅当目标 drama_archive 为空时迁移，幂等且不覆盖新库数据。
    """
    target_path = os.path.abspath(target_path or DB_PATH)
    legacy_path = os.path.abspath(legacy_path or LEGACY_DB_PATH)
    if target_path == legacy_path or not os.path.isfile(legacy_path):
        return 0

    target = _connect_db(target_path)
    legacy = sqlite3.connect(legacy_path, timeout=5)
    try:
        if target.execute('SELECT COUNT(*) FROM drama_archive').fetchone()[0] > 0:
            return 0
        exists = legacy.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drama_archive'"
        ).fetchone()
        if not exists:
            return 0
        legacy_cols = {row[1] for row in legacy.execute(
            'PRAGMA table_info(drama_archive)').fetchall()}
        cols = [c for c in ARCHIVE_COLUMNS if c in legacy_cols]
        if 'drama_name' not in cols:
            return 0
        rows = legacy.execute(
            f"SELECT {', '.join(cols)} FROM drama_archive"
        ).fetchall()
        if not rows:
            return 0
        placeholders = ', '.join('?' for _ in cols)
        target.executemany(
            f"INSERT OR IGNORE INTO drama_archive ({', '.join(cols)}) "
            f"VALUES ({placeholders})",
            rows,
        )
        target.commit()
        return len(rows)
    finally:
        legacy.close()
        target.close()


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
        _migrate_source_key(conn)
        conn.commit()
    finally:
        conn.close()
    # 首次切到统一库时搬入旧档案，不删除旧库，便于回滚。
    if os.path.abspath(DB_PATH) == os.path.abspath(DEFAULT_DB_PATH):
        try:
            migrate_legacy_archive(DB_PATH, LEGACY_DB_PATH)
        except Exception as e:
            print(f'WARN: 历史剧档案迁移失败: {e}')


def _cols(conn):
    return [d[0] for d in conn.execute('SELECT * FROM drama_archive LIMIT 0').description]


def upsert_archive(drama_name, **fields):
    """按 drama_name 唯一键插入或更新。fields 为列名→值。

    新行自动补 source_key（链接唯一键）；已存在的行不改写 source_key，
    避免两条来源不同的行互相抢占唯一索引。
    """
    if not fields:
        return
    if 'source_key' not in fields:
        key = source_key_for(fields.get('source_url'), drama_name)
        if key:
            fields['source_key'] = key
    conn = _conn()
    try:
        cols = list(fields.keys())
        sets = ', '.join(f'{c}=excluded.{c}' for c in cols
                         if c != 'source_key')
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


def resolve_archive_name(source_url=None, preferred_name=None):
    """按来源唯一键找规范剧名，避免同一条链接写出第二条档案行。

    优先返回“非分享码形态”的行（真实剧名），其次任意已有行，最后才用
    调用方给的派生名。
    """
    key = source_key_for(source_url, preferred_name)
    if not key:
        return preferred_name
    conn = _conn()
    try:
        names = [r[0] for r in conn.execute(
            'SELECT drama_name FROM drama_archive WHERE source_key=?',
            (key,)).fetchall() if r and r[0]]
    finally:
        conn.close()
    real = [n for n in names if not _looks_like_link_id_name(n)]
    if real:
        return real[0]
    if names:
        return names[0]
    return preferred_name


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


def get_archive_by_source(source_url):
    """按来源链接返回最新"已完成下载"的剧档案（drama_name/local_dir/download_state）。

    供下载防重分支找回真实剧名与素材目录：调用方只有 link_id（分享码）猜的路径，
    与真实目录名（剧名）不一致时会指向空目录。无记录返回 None。
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT drama_name, local_dir, download_state, downloaded_at FROM drama_archive "
            "WHERE source_url=? AND download_state='done' ORDER BY id DESC LIMIT 1",
            (source_url,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return dict(zip(('drama_name', 'local_dir', 'download_state', 'downloaded_at'), row))


def get_archive_name_by_source(source_url):
    """按来源链接返回任一状态档案行的 drama_name（最新一条）。

    供下载失败回填找回「规范行」：失败路径默认用 link_id/link 派生名 upsert，
    会新建一条 link_id 键的重复行，而更新不到按真实剧名/source_url 的规范行。
    无记录返回 None。
    """
    if not source_url:
        return None
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT drama_name FROM drama_archive WHERE source_url=? "
            "ORDER BY id DESC LIMIT 1", (source_url,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def get_archive_rows_by_source(source_url):
    """按来源链接返回全部档案行（最新在前）。

    供重复转存/失败回填时判断该链接对应的真实剧名，调用方需自行过滤
    被历史写入污染的完成行。
    """
    source_url = str(source_url or '').strip()
    if not source_url:
        return []
    conn = _conn()
    try:
        cols = _cols(conn)
        rows = conn.execute(
            "SELECT * FROM drama_archive WHERE source_url=? ORDER BY id DESC",
            (source_url,)).fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, row)) for row in rows]


def merge_auto_download_archive(old_name, new_name, source_url, local_dir=None):
    """Merge the legacy downloader's cloud-folder archive into candidate title."""
    old_name = str(old_name or '').strip()
    new_name = str(new_name or '').strip()
    source_url = str(source_url or '').strip()
    if not old_name or not new_name or not source_url:
        return False
    conn = _conn()
    try:
        source = conn.execute(
            'SELECT * FROM drama_archive WHERE drama_name=? AND source_url=?',
            (old_name, source_url)).fetchone()
        destination = conn.execute(
            'SELECT * FROM drama_archive WHERE drama_name=?', (new_name,)).fetchone()
        if old_name == new_name:
            if not source or source['download_state'] != 'done':
                return False
            conn.execute('''UPDATE drama_archive SET local_dir=COALESCE(?, local_dir),
                           updated_at=CURRENT_TIMESTAMP WHERE drama_name=?''',
                         (local_dir, new_name))
            conn.commit()
            return True
        if not source or source['download_state'] != 'done' or not destination:
            return False
        if (destination['download_state'] != 'downloading' or
                destination['source_url'] != source_url):
            return False
        conn.execute('''UPDATE drama_archive SET
                           local_dir=COALESCE(?, ?),
                           download_size=COALESCE(?, download_size),
                           download_files=COALESCE(?, download_files),
                           episode_count=COALESCE(?, episode_count),
                           downloaded_at=COALESCE(?, downloaded_at),
                           updated_at=CURRENT_TIMESTAMP
                         WHERE drama_name=?''',
                     (local_dir, source['local_dir'], source['download_size'],
                      source['download_files'], source['episode_count'],
                      source['downloaded_at'], new_name))
        conn.execute('DELETE FROM drama_archive WHERE drama_name=? AND source_url=?',
                     (old_name, source_url))
        conn.commit()
        return True
    finally:
        conn.close()


def rename_download_archive(old_name, new_name, source_url=None):
    """把下载器按网盘目录名写下的档案行归并到候选标题目录。

    下载失败时下载器可能已按网盘文件夹名（provisional）写了独立档案行；
    该行与候选标题行（本次下载前预留为 downloading）指同一批素材。
    安全合并：仅当候选行属于同来源且仍处 downloading 预留态时才写入，
    避免覆盖其它候选/正式档案。返回 True=已归并或无需处理；False=不安全，
    保持网盘目录名档案不动（调用方会以候选标题另记失败，二者互不覆盖）。
    """
    old_name = str(old_name or '').strip()
    new_name = str(new_name or '').strip()
    source_url = str(source_url or '').strip()
    if not old_name or not new_name:
        return False
    if old_name == new_name:
        return True
    conn = _conn()
    try:
        cols = _cols(conn)
        source = conn.execute(
            'SELECT * FROM drama_archive WHERE drama_name=?', (old_name,)).fetchone()
        if not source:
            return True  # 网盘目录名没有独立档案行，无需处理
        destination = conn.execute(
            'SELECT * FROM drama_archive WHERE drama_name=?', (new_name,)).fetchone()
        if destination:
            if source_url and destination['source_url'] != source_url:
                return False  # 候选行属于其它来源，不能覆盖
            if destination['download_state'] != 'downloading':
                return False  # 候选行已是终态/正式态，不动它
            # 把网盘行的状态信息带到候选行（候选行保留自己预留的 rating/category 等）。
            merge_cols = ('download_state', 'download_error', 'download_size',
                          'download_files', 'episode_count', 'downloaded_at', 'local_dir')
            sets = [(c, source[c]) for c in merge_cols
                    if c in cols and source[c] is not None]
            if not sets:
                return False
            for col, val in sets:
                conn.execute(
                    'UPDATE drama_archive SET %s=?, updated_at=CURRENT_TIMESTAMP '
                    'WHERE drama_name=?' % col, (val, new_name))
            conn.execute('DELETE FROM drama_archive WHERE drama_name=?', (old_name,))
            conn.commit()
            return True
        # 候选标题行不存在：网盘行整体改名（保 source_url 防重复）。
        conn.execute(
            'UPDATE drama_archive SET drama_name=?, updated_at=CURRENT_TIMESTAMP '
            'WHERE drama_name=?', (new_name, old_name))
        conn.commit()
        return True
    finally:
        conn.close()


# 素材目录内不算“可切片素材”的文件标记（成片/封面/半截下载）。
_MATERIAL_EXCLUDE_MARKERS = ('_去重版', '_REJECTED', '封面', '.tmp',
                             '.BaiduPCS-Go-downloading')


def count_material_mp4(dir_path):
    """统计目录内可用于切片的 mp4 集数（递归，排除成片/封面/半截文件）。

    防重的唯一权威是「磁盘上真的有素材」：archive 状态和目录存在性都可能
    是历史残留（清理过素材但记录仍是 done、目录里只剩 .incoming 半截文件）。
    """
    if not dir_path or not os.path.isdir(dir_path):
        return 0
    count = 0
    try:
        for root, _dirs, files in os.walk(dir_path):
            for fn in files:
                if not fn.lower().endswith('.mp4'):
                    continue
                if any(m in fn for m in _MATERIAL_EXCLUDE_MARKERS):
                    continue
                count += 1
    except OSError:
        return 0
    return count


def should_download(drama_name, local_dir, source_url=None, min_files=2):
    """防重下载检查。返回 (是否可下载, 原因)。

    同一来源或历史 download_state=done → 只在**素材实体仍存在**时拦截；
    记录说完成但目录已空/只剩半截文件 → 允许重新下载（否则“假完成”会
    让后续切片报“没有素材文件”）。
    上次失败/未完成 → 允许重试/续传（优先于完成态）。
    min_files：判定“素材存在”的最少集数。
    """
    row = get_archive(drama_name)
    material_dir = local_dir
    if row and row.get('local_dir'):
        material_dir = row['local_dir']
    same_source = None
    if source_url:
        conn = _conn()
        try:
            same_source = conn.execute(
                "SELECT downloaded_at, local_dir FROM drama_archive "
                "WHERE source_url=? AND download_state='done' LIMIT 1",
                (source_url,),
            ).fetchone()
        finally:
            conn.close()
        if same_source and same_source['local_dir']:
            material_dir = same_source['local_dir']
    have = count_material_mp4(material_dir)
    if same_source:
        if have >= min_files:
            return False, f'同一来源已下载成功（{same_source["downloaded_at"] or ""}）'
        return True, (f'同一来源有完成记录但素材已缺失'
                      f'（{have}集<{min_files}集），重新下载')
    if row and row['download_state'] in ('failed', 'downloading'):
        return True, ('上次下载失败，可重试'
                      if row['download_state'] == 'failed'
                      else '上次下载未完成，可续传')
    if row and row['download_state'] == 'done':
        if have >= min_files:
            return False, f'历史记录已下载成功（{row.get("downloaded_at") or ""}）'
        return True, (f'历史记录为已完成但本地素材缺失'
                      f'（{have}集<{min_files}集），重新下载')
    if os.path.isdir(material_dir):
        if have >= min_files:
            return False, f'本地目录已存在: {material_dir}'
        return True, (f'本地目录仅有残留（{have}集<{min_files}集，'
                      f'可能是 .incoming 半截文件），重新下载')
    return True, '可下载'


def aggregate_upload_state(drama_name):
    """查询时从 upload_records 聚合上传状态。返回 (state, success_count, failed_count)。

    none（无记录）/ done（全成功）/ partial（部分成功）/ failed（全失败）。

    按 (account, version) 分组取代表状态：组内任一 completed → 该版本算成功；
    否则有 pending/uploading → 进行中；全部 failed → 失败。
    不依赖 MAX(id)——否则"失败后重传成功"被旧 failed 压成 partial、
    或"成功后被人工标 failed（如重复上传阻止）"会覆盖 completed 真相
    （2026-11-10 人鱼赴归潮 V5：id339 completed + id345 重复上传标 failed，
    若取最新行会误判为 failed 而非 done）。
    """
    conn = _conn()
    try:
        # upload_records 表尚未建（upload_manager 未初始化）——视为无记录
        row = conn.execute(
            '''WITH grp AS (
                   SELECT account, version,
                          CASE WHEN SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) > 0
                               THEN 'completed'
                               WHEN SUM(CASE WHEN status IN ('pending','uploading') THEN 1 ELSE 0 END) > 0
                               THEN 'active'
                               ELSE 'failed' END AS rep
                   FROM upload_records
                   WHERE drama_name=?
                   GROUP BY account, version
               )
               SELECT COUNT(*) AS total,
                      SUM(CASE WHEN rep='completed' THEN 1 ELSE 0 END) AS ok,
                      SUM(CASE WHEN rep='failed' THEN 1 ELSE 0 END) AS bad,
                      SUM(CASE WHEN rep='active' THEN 1 ELSE 0 END) AS active
               FROM grp''', (drama_name,)).fetchone()
    except sqlite3.OperationalError:
        return 'none', 0, 0
    finally:
        conn.close()
    total, ok, bad, active = (
        row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0)
    if total == 0:
        return 'none', 0, 0
    if ok == total:
        return 'done', ok, 0
    if ok > 0 or active > 0:
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
        # upload_state 来自跨表聚合，先筛选再截断，避免只检查前 N 条造成漏结果。
        sql += ' ORDER BY updated_at DESC'
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
    return results[:max(0, int(limit))]
