# -*- coding: utf-8 -*-
"""候选漫剧库与自动选剧运行记录。

候选资料由完整 CSV 更新；本地下载、切片和正式发布状态不由 CSV 覆盖。
"""
import csv
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'candidate_library.db')
CSV_PATH = os.path.join(PROJECT_ROOT, '素材', '候选库', '候选库.csv')
IMPORT_HISTORY_DIR = os.path.join(PROJECT_ROOT, 'data', 'candidate_imports')
RECYCLE_ROOT = os.path.join(PROJECT_ROOT, '回收站', '素材维护')

CSV_FIELDS = {
    'title': ('剧名',),
    'channel': ('频道',),
    'category': ('类目',),
    'topic': ('题材',),
    'listing_status': ('上架状态',),
    'listing_date': ('上架日期',),
    'rating': ('评级',),
    'source_url': ('素材链接', '链接'),
    'listing_accounts': ('上架账号',),
    'can_publish_accounts': ('可发视频号',),
    'published_accounts': ('已发视频号',),
    'unpublished_accounts': ('未发视频号',),
    'publish_status': ('发布状态',),
}


def normalize_title(value):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ''.join(ch for ch in value if ch.isalnum())


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=10000')
    except sqlite3.Error:
        pass
    return conn


def init_database():
    conn = _connect()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS candidates (
                title_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                channel TEXT,
                category TEXT,
                topic TEXT,
                listing_status TEXT,
                listing_date TEXT,
                rating TEXT,
                source_url TEXT,
                source_passwd TEXT,
                listing_accounts TEXT,
                can_publish_accounts TEXT,
                published_accounts TEXT,
                unpublished_accounts TEXT,
                publish_status TEXT,
                source_order INTEGER NOT NULL DEFAULT 0,
                present_in_latest INTEGER NOT NULL DEFAULT 0,
                weak_marked INTEGER NOT NULL DEFAULT 0,
                duplicate_marked INTEGER NOT NULL DEFAULT 0,
                recycled_at TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                import_source TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_date ON candidates(listing_date DESC);
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                rows_seen INTEGER NOT NULL,
                rows_upserted INTEGER NOT NULL,
                snapshot_path TEXT
            );
            CREATE TABLE IF NOT EXISTS auto_runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                requested_count INTEGER NOT NULL DEFAULT 2,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                summary TEXT,
                error TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS auto_run_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                slot INTEGER NOT NULL,
                account TEXT NOT NULL,
                candidate_title TEXT NOT NULL,
                candidate_key TEXT,
                listing_date TEXT,
                source_url TEXT,
                status TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                archive_name TEXT,
                cut_task_id TEXT,
                error TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES auto_runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_auto_run_items_run ON auto_run_items(run_id, id);
            CREATE TABLE IF NOT EXISTS source_fingerprints (
                title_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                video_path TEXT NOT NULL,
                hash_seq BLOB NOT NULL,
                duration REAL,
                size_bytes INTEGER,
                scanned_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS duplicate_pairs (
                left_title_key TEXT NOT NULL,
                right_title_key TEXT NOT NULL,
                left_title TEXT NOT NULL,
                right_title TEXT NOT NULL,
                similarity REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'suspected',
                detected_at TEXT NOT NULL,
                PRIMARY KEY(left_title_key, right_title_key)
            );
        ''')
        run_cols = {row[1] for row in conn.execute('PRAGMA table_info(auto_runs)')}
        if 'updated_at' not in run_cols:
            conn.execute('ALTER TABLE auto_runs ADD COLUMN updated_at TEXT')
        candidate_cols = {row[1] for row in conn.execute('PRAGMA table_info(candidates)')}
        if 'source_order' not in candidate_cols:
            conn.execute('ALTER TABLE candidates ADD COLUMN source_order INTEGER NOT NULL DEFAULT 0')
        if 'present_in_latest' not in candidate_cols:
            conn.execute('ALTER TABLE candidates ADD COLUMN present_in_latest INTEGER NOT NULL DEFAULT 0')
        run_item_cols = {row[1] for row in conn.execute('PRAGMA table_info(auto_run_items)')}
        if 'candidate_key' not in run_item_cols:
            conn.execute('ALTER TABLE auto_run_items ADD COLUMN candidate_key TEXT')
        conn.commit()
    finally:
        conn.close()


def _read_csv(path):
    rows = None
    last_error = None
    headers = set()
    for encoding in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            with open(path, 'r', encoding=encoding, newline='') as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise ValueError('CSV 缺少表头')
                headers = set(reader.fieldnames)
                rows = list(reader)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    if rows is None:
        raise ValueError(f'无法识别 CSV 编码：{last_error}')
    required = {'剧名', '上架日期', '评级', '频道', '类目', '发布状态'}
    if '素材链接' not in headers and '链接' not in headers:
        required.add('素材链接')
    missing = required - headers
    if missing:
        raise ValueError('CSV 缺少必需列：' + '、'.join(sorted(missing)))
    return rows


def _field(row, aliases):
    for alias in aliases:
        if alias in row and row.get(alias) is not None:
            return str(row.get(alias) or '').strip()
    return ''


def parse_share(url):
    url = str(url or '').strip()
    if not re.match(r'^https?://pan\.baidu\.com/s/[A-Za-z0-9_-]+(?:[/?#]|$)', url, re.I):
        return '', ''
    try:
        query = parse_qs(urlparse(url).query)
        pwd = next((x for x in query.get('pwd', []) if x), '')
    except Exception:
        pwd = ''
    if not pwd:
        match = re.search(r'(?:提取码|pwd)\s*[:：=]?\s*([A-Za-z0-9]{4})', url, re.I)
        pwd = match.group(1) if match else ''
    return url, pwd


def _normalize_date(value):
    value = str(value or '').strip()
    if not value:
        return ''
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            return datetime.strptime(value[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return value


def _parse_rows(path):
    parsed = {}
    source_rows = _read_csv(path)
    for row_order, row in enumerate(source_rows):
        title = _field(row, CSV_FIELDS['title'])
        key = normalize_title(title)
        if not key:
            continue
        item = {name: _field(row, aliases) for name, aliases in CSV_FIELDS.items()}
        item['title'] = title
        item['title_key'] = key
        item['source_order'] = row_order
        item['present_in_latest'] = 1
        item['listing_date'] = _normalize_date(item.get('listing_date'))
        item['source_url'], item['source_passwd'] = parse_share(item['source_url'])
        # 同一份全量表里若重复出现同剧，保留日期较新的那一行；同日保留后出现行。
        old = parsed.get(key)
        if old and (old.get('listing_date') or '') > (item.get('listing_date') or ''):
            continue
        parsed[key] = item
    if not parsed:
        raise ValueError('CSV 中没有可导入的剧名行')
    return parsed, len(source_rows)


def import_rows(rows, source_name='manual.csv', parsed_rows=None):
    """Transactionally import rows; `parsed_rows` reuses prior validation/dedup."""
    init_database()
    if parsed_rows is None:
        parsed = _parse_dict_rows(rows)
    else:
        parsed = dict(parsed_rows)
    if not parsed:
        raise ValueError('CSV 中没有可导入的剧名行')

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE candidates SET present_in_latest=0')
        for item in parsed.values():
            cols = list(item)
            values = [item[c] for c in cols]
            placeholders = ', '.join('?' for _ in cols)
            insert_cols = ', '.join(cols + ['first_seen_at', 'last_seen_at', 'import_source'])
            update_cols = [c for c in cols if c not in ('title_key', 'title')]
            # Incoming CSV is a full snapshot for presence, but blank cells do not
            # erase metadata already learned for this title. Operational states and
            # human maintenance flags live separately and are never part of this UPSERT.
            sets = ', '.join(
                f"{c}=CASE WHEN COALESCE(excluded.{c}, '')!='' "
                f"THEN excluded.{c} ELSE candidates.{c} END"
                for c in update_cols if c != 'source_passwd')
            sets += (', ' if sets else '') + '''source_passwd=CASE
                WHEN COALESCE(excluded.source_url, '')!=''
                     AND COALESCE(candidates.source_url, '')!=''
                     AND excluded.source_url!=candidates.source_url
                     AND COALESCE(excluded.source_passwd, '')=''
                    THEN NULL
                WHEN COALESCE(excluded.source_passwd, '')!=''
                    THEN excluded.source_passwd
                ELSE candidates.source_passwd END'''
            sets += ', source_order=excluded.source_order'
            sets += ', present_in_latest=1, last_seen_at=excluded.last_seen_at, import_source=excluded.import_source'
            conn.execute(
                f'''INSERT INTO candidates ({insert_cols})
                    VALUES ({placeholders}, ?, ?, ?)
                    ON CONFLICT(title_key) DO UPDATE SET {sets}''',
                values + [now, now, source_name])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {'rows_seen': len(rows), 'rows_upserted': len(parsed)}


def _atomic_copy(source, target):
    target = os.path.abspath(target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='.candidate-import-',
                                     suffix='.tmp', dir=os.path.dirname(target))
    os.close(fd)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, target)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _parse_dict_rows(rows):
    parsed = {}
    for row_order, row in enumerate(rows):
        title = _field(row, CSV_FIELDS['title'])
        key = normalize_title(title)
        if not key:
            continue
        item = {name: _field(row, aliases) for name, aliases in CSV_FIELDS.items()}
        item['title'] = title
        item['title_key'] = key
        item['source_order'] = row_order
        item['present_in_latest'] = 1
        item['listing_date'] = _normalize_date(item.get('listing_date'))
        item['source_url'], item['source_passwd'] = parse_share(item['source_url'])
        old = parsed.get(key)
        if old and (old.get('listing_date') or '') > (item.get('listing_date') or ''):
            continue
        parsed[key] = item
    if not parsed:
        raise ValueError('CSV 中没有可导入的剧名行')
    return parsed


def import_csv(path, fixed_path=None):
    """Validate, persist a snapshot, then incrementally update the candidate DB."""
    init_database()
    # Validate and deduplicate before changing the fixed copy or latest-snapshot flags.
    source_rows = _read_csv(path)
    parsed, rows_seen = _parse_rows(path)
    if not parsed:
        raise ValueError('CSV 中没有可导入的剧名行')
    fixed_path = os.path.abspath(fixed_path or CSV_PATH)
    os.makedirs(IMPORT_HISTORY_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    snapshot = os.path.join(IMPORT_HISTORY_DIR, f'{stamp}_{os.path.basename(path)}')
    _atomic_copy(path, snapshot)
    _atomic_copy(path, fixed_path)
    result = import_rows(source_rows, os.path.basename(path), parsed_rows=parsed)
    conn = _connect()
    try:
        conn.execute('''INSERT INTO imports
                        (filename,imported_at,rows_seen,rows_upserted,snapshot_path)
                        VALUES(?,?,?,?,?)''',
                     (os.path.basename(path), datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      result['rows_seen'], result['rows_upserted'], snapshot))
        conn.commit()
    finally:
        conn.close()
    result.update({'snapshot_path': snapshot, 'fixed_path': fixed_path})
    return result


def get_import_status():
    init_database()
    conn = _connect()
    try:
        row = conn.execute('SELECT * FROM imports ORDER BY id DESC LIMIT 1').fetchone()
        total = conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
        return {'latest_import': dict(row) if row else None, 'candidate_count': total,
                'fixed_path': CSV_PATH}
    finally:
        conn.close()


def list_candidates(q=None, channel=None, category=None, state=None, page=1, limit=50):
    init_database()
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 50), 200))
    conn = _connect()
    try:
        where, params = [], []
        if q:
            where.append('(title LIKE ? OR topic LIKE ? OR rating LIKE ?)')
            params.extend([f'%{q}%'] * 3)
        if channel:
            where.append('channel=?')
            params.append(channel)
        if category:
            where.append('category=?')
            params.append(category)
        if state == 'weak':
            where.append('weak_marked=1')
        elif state == 'duplicate':
            where.append('duplicate_marked=1')
        elif state == 'active':
            where.append('recycled_at IS NULL')
        elif state == 'recycled':
            where.append('recycled_at IS NOT NULL')
        elif state == 'stale':
            where.append('present_in_latest=0')
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        total = conn.execute('SELECT COUNT(*) FROM candidates' + clause, params).fetchone()[0]
        rows = conn.execute(
            'SELECT * FROM candidates' + clause +
            ' ORDER BY listing_date DESC, source_order ASC LIMIT ? OFFSET ?',
            params + [limit, (page - 1) * limit]).fetchall()
        categories = [r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM candidates WHERE category IS NOT NULL AND category!='' ORDER BY category")]
        channels = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel FROM candidates WHERE channel IS NOT NULL AND channel!='' ORDER BY channel")]
        return {'items': [dict(r) for r in rows], 'total': total, 'page': page,
                'limit': limit, 'categories': categories, 'channels': channels}
    finally:
        conn.close()


def all_candidates():
    init_database()
    conn = _connect()
    try:
        return [dict(row) for row in conn.execute(
            'SELECT * FROM candidates ORDER BY listing_date DESC, source_order ASC')]
    finally:
        conn.close()


def save_source_fingerprint(title, video_path, seq, duration=None, size_bytes=None):
    init_database()
    key = normalize_title(title)
    if not seq or not key:
        return False
    from content_fingerprint import FingerprintStore
    blob = FingerprintStore._seq_to_blob(seq)
    conn = _connect()
    try:
        conn.execute('''INSERT INTO source_fingerprints
            (title_key,title,video_path,hash_seq,duration,size_bytes,scanned_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(title_key) DO UPDATE SET title=excluded.title,
                video_path=excluded.video_path,hash_seq=excluded.hash_seq,
                duration=excluded.duration,size_bytes=excluded.size_bytes,
                scanned_at=excluded.scanned_at''',
                     (key, title, video_path, blob, duration, size_bytes,
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        return True
    finally:
        conn.close()


def find_source_matches(seq, title=None, threshold=0.90):
    if not seq:
        return []
    from content_fingerprint import FingerprintStore, similarity
    key = normalize_title(title)
    conn = _connect()
    try:
        rows = conn.execute('SELECT title_key,title,video_path,hash_seq FROM source_fingerprints').fetchall()
    finally:
        conn.close()
    matches = []
    for row in rows:
        if row['title_key'] == key:
            continue
        score = similarity(seq, FingerprintStore._blob_to_seq(row['hash_seq']))
        if score >= threshold:
            matches.append({'title': row['title'], 'video_path': row['video_path'],
                            'similarity': round(score, 3)})
    return sorted(matches, key=lambda x: -x['similarity'])


def save_duplicate_pair(left_title, right_title, similarity, status='suspected'):
    init_database()
    left_key, right_key = sorted((normalize_title(left_title), normalize_title(right_title)))
    names = {normalize_title(left_title): left_title,
             normalize_title(right_title): right_title}
    conn = _connect()
    try:
        conn.execute('''INSERT INTO duplicate_pairs
            (left_title_key,right_title_key,left_title,right_title,similarity,status,detected_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(left_title_key,right_title_key) DO UPDATE SET
                similarity=excluded.similarity,status=excluded.status,detected_at=excluded.detected_at''',
                     (left_key, right_key, names[left_key], names[right_key], similarity,
                      status, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
    finally:
        conn.close()


def source_fingerprints():
    init_database()
    conn = _connect()
    try:
        return [dict(row) for row in conn.execute(
            'SELECT title,video_path,duration,size_bytes,scanned_at FROM source_fingerprints ORDER BY scanned_at DESC')]
    finally:
        conn.close()


def eligible_candidates(today=None):
    """按上架时间新到旧选；评级只保留显示，不参与排序。"""
    init_database()
    today = today or date.today()
    from datetime import timedelta
    cutoff = today - timedelta(days=30)
    conn = _connect()
    try:
        rows = conn.execute('''SELECT * FROM candidates
            WHERE recycled_at IS NULL AND weak_marked=0 AND duplicate_marked=0
              AND present_in_latest=1
              AND channel IN ('女','男','通用')
              AND listing_status='已上架'
            ORDER BY listing_date DESC, source_order ASC''').fetchall()
        eligible = []
        for row in rows:
            item = dict(row)
            try:
                raw_date = (item.get('listing_date') or '')[:10]
                try:
                    listed = date.fromisoformat(raw_date)
                except ValueError:
                    listed = datetime.strptime(raw_date, '%Y/%m/%d').date()
            except ValueError:
                continue
            if listed < cutoff or listed > today:
                continue
            if any(marker in (item.get(field) or '') for marker in ('重上', '重上架')
                   for field in ('rating', 'listing_status', 'publish_status')):
                continue
            if not item.get('source_url') or not item.get('source_passwd'):
                continue
            # 只有明确未发布/今日可发，且没有已发账号记录的条目参与自动选剧。
            pub = (item.get('publish_status') or '').strip()
            published = (item.get('published_accounts') or '').strip()
            if published or pub not in ('未发', '未发布'):
                continue
            eligible.append(item)
        return eligible
    finally:
        conn.close()


def update_candidate_flags(title, weak_marked=None, duplicate_marked=None):
    init_database()
    key = normalize_title(title)
    if not key:
        return False
    sets, values = [], []
    if weak_marked is not None:
        sets.append('weak_marked=?')
        values.append(1 if weak_marked else 0)
    if duplicate_marked is not None:
        sets.append('duplicate_marked=?')
        values.append(1 if duplicate_marked else 0)
    if not sets:
        return False
    values.append(key)
    conn = _connect()
    try:
        cur = conn.execute('UPDATE candidates SET ' + ', '.join(sets) +
                           ' WHERE title_key=?', values)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_candidate_flag_by_key(key, weak_marked=None, duplicate_marked=None):
    init_database()
    conn = _connect()
    try:
        sets, values = [], []
        if weak_marked is not None:
            sets.append('weak_marked=?')
            values.append(int(bool(weak_marked)))
        if duplicate_marked is not None:
            sets.append('duplicate_marked=?')
            values.append(int(bool(duplicate_marked)))
        if not sets:
            return False
        cur = conn.execute('UPDATE candidates SET ' + ', '.join(sets) +
                           ' WHERE title_key=?', values + [key])
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def candidate_by_title(title):
    init_database()
    conn = _connect()
    try:
        row = conn.execute('SELECT * FROM candidates WHERE title_key=?',
                           (normalize_title(title),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def titles_by_source(url):
    """按分享链接返回候选库里的剧名（去重、按标题排序）。

    供下载重复转存时定位网盘目录：候选库标题是「链接→剧名」的权威来源。
    """
    url = str(url or '').strip()
    if not url:
        return []
    base = url.split('?')[0]
    init_database()
    conn = _connect()
    try:
        rows = conn.execute(
            'SELECT title FROM candidates WHERE source_url=? OR source_url LIKE ? '
            'ORDER BY title', (url, base + '%')).fetchall()
    finally:
        conn.close()
    seen, out = set(), []
    for row in rows:
        title = str(row['title'] or '').strip()
        if title and title not in seen:
            seen.add(title)
            out.append(title)
    return out


def add_auto_run(run_id, requested_count=2):
    init_database()
    conn = _connect()
    try:
        conn.execute('INSERT INTO auto_runs(id,status,requested_count,started_at) VALUES(?,?,?,?)',
                     (run_id, 'selecting', requested_count,
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
    finally:
        conn.close()


def update_auto_run(run_id, **fields):
    if not fields:
        return
    init_database()
    conn = _connect()
    try:
        fields['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # updated_at exists on the parent table by migration below.
        cols = ', '.join(f'{key}=?' for key in fields)
        conn.execute(f'UPDATE auto_runs SET {cols} WHERE id=?',
                     list(fields.values()) + [run_id])
        conn.commit()
    finally:
        conn.close()


def add_run_item(run_id, slot, account, candidate, status='selected', **fields):
    init_database()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    values = {
        'run_id': run_id, 'slot': slot, 'account': account,
        'candidate_title': candidate.get('title', ''),
        'candidate_key': candidate.get('title_key') or normalize_title(candidate.get('title')),
        'listing_date': candidate.get('listing_date'),
        'source_url': candidate.get('source_url'), 'status': status,
        'attempts': fields.get('attempts', 0),
        'archive_name': fields.get('archive_name'),
        'cut_task_id': fields.get('cut_task_id'),
        'error': fields.get('error'), 'updated_at': now,
    }
    conn = _connect()
    try:
        cur = conn.execute('''INSERT INTO auto_run_items
            (run_id,slot,account,candidate_title,candidate_key,listing_date,source_url,status,attempts,
             archive_name,cut_task_id,error,updated_at)
            VALUES (:run_id,:slot,:account,:candidate_title,:candidate_key,:listing_date,:source_url,:status,
                    :attempts,:archive_name,:cut_task_id,:error,:updated_at)''', values)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_run_item(item_id, **fields):
    if not fields:
        return
    init_database()
    fields['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = _connect()
    try:
        conn.execute('UPDATE auto_run_items SET ' + ', '.join(f'{k}=?' for k in fields) +
                     ' WHERE id=?', list(fields.values()) + [item_id])
        conn.commit()
    finally:
        conn.close()


def get_auto_run(run_id=None):
    init_database()
    conn = _connect()
    try:
        row = conn.execute('SELECT * FROM auto_runs WHERE id=?' if run_id else
                           'SELECT * FROM auto_runs ORDER BY started_at DESC LIMIT 1',
                           (run_id,) if run_id else ()).fetchone()
        if not row:
            return None
        result = dict(row)
        result['items'] = [dict(x) for x in conn.execute(
            'SELECT * FROM auto_run_items WHERE run_id=? ORDER BY id', (result['id'],))]
        return result
    finally:
        conn.close()


def recycle_material(title, source_dir):
    """把明确选中的本地素材目录移入回收站，保留档案与候选记录。"""
    init_database()
    item = candidate_by_title(title)
    if not item:
        raise ValueError('候选不存在')
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        raise ValueError('素材目录不存在')
    root = os.path.abspath(os.path.dirname(source_dir))
    try:
        from config import CONFIG
        configured_root = CONFIG.get('download_base_dir')
    except Exception:
        configured_root = None
    allowed_root = os.path.abspath(os.environ.get(
        'SHORTDRAMA_DOWNLOAD_ROOT') or configured_root or r'E:\短剧素材')
    if root != allowed_root:
        raise ValueError('素材目录不在允许回收的素材根目录中')
    os.makedirs(RECYCLE_ROOT, exist_ok=True)
    target_parent = os.path.join(RECYCLE_ROOT,
                                 datetime.now().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(target_parent, exist_ok=True)
    target = os.path.join(target_parent, os.path.basename(source_dir))
    if os.path.exists(target):
        raise ValueError('回收站中已存在同名目录')
    shutil.move(source_dir, target)
    conn = _connect()
    try:
        conn.execute('UPDATE candidates SET recycled_at=? WHERE title_key=?',
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), item['title_key']))
        conn.commit()
    finally:
        conn.close()
    return target


def list_recycle_items():
    os.makedirs(RECYCLE_ROOT, exist_ok=True)
    rows = []
    for root, dirs, _files in os.walk(RECYCLE_ROOT):
        for name in dirs:
            path = os.path.join(root, name)
            rows.append({'name': name, 'path': path,
                         'recycled_at': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S')})
    return sorted(rows, key=lambda x: x['recycled_at'], reverse=True)


def empty_recycle_bin():
    """永久清空素材维护回收站；调用方必须先经 UI 二次确认。"""
    root = os.path.abspath(RECYCLE_ROOT)
    if root != os.path.abspath(os.path.join(PROJECT_ROOT, '回收站', '素材维护')):
        raise ValueError('回收站路径校验失败')
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    mark_recycled_candidates_from_archive()
    return True


def delete_recycle_item(path):
    root = os.path.abspath(RECYCLE_ROOT)
    target = os.path.abspath(path)
    if os.path.commonpath((root, target)) != root or target == root:
        raise ValueError('回收站路径校验失败')
    if not os.path.exists(target):
        raise ValueError('回收项不存在')
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    mark_recycled_candidates_from_archive()
    return True


def mark_recycled_candidates_from_archive():
    """After physically emptying the recycle bin, retain exclusion for archived materials."""
    init_database()
    conn = _connect()
    try:
        rows = conn.execute('''SELECT title_key,title FROM candidates
                               WHERE recycled_at IS NOT NULL''').fetchall()
    finally:
        conn.close()
    try:
        from archive import get_archive
        from config import CONFIG
    except Exception:
        return 0
    keep = []
    for row in rows:
        archive = get_archive(row['title']) or {}
        local_dir = archive.get('local_dir') or os.path.join(
            CONFIG.get('download_base_dir', ''), row['title'])
        if archive.get('download_state') == 'done' or os.path.exists(local_dir):
            keep.append(row['title_key'])
    conn = _connect()
    try:
        conn.execute('UPDATE candidates SET recycled_at=NULL WHERE recycled_at IS NOT NULL')
        conn.executemany('UPDATE candidates SET weak_marked=1, recycled_at=? WHERE title_key=?',
                         [(datetime.now().strftime('%Y-%m-%d %H:%M:%S'), key)
                          for key in keep])
        conn.commit()
        return len(keep)
    finally:
        conn.close()
