# -*- coding: utf-8 -*-
"""成品内容指纹：帧级感知哈希序列 + 相似度 + 内部重复 + 指纹存储。

内容重复规避的核心计算层（设计见 docs/内容重复规避设计.md）：

- 指纹：ffmpeg 按 1fps 采样灰度帧（64x64），每帧算 56-bit 差分哈希（dHash）。
  对转码/缩放/轻微噪声/水印/裁剪鲁棒（抗查重链后仍可跨环节比对）。
- 相似度：短序列滑过长序列，逐位置算「帧 Hamming 距离 ≤ HAMM_THRESH 的帧占比」，
  取最大 = 成片内容重合度（平台判"内容重复"的度量）。
- 内部重复：10s 块与其后间隔 ≥ min_gap 的块两两算相似度，≥ match_ratio 判重复。
- 存储：SQLite `content_fingerprints` 表（BLOB 哈希序列），支持同账号相似查询。

不依赖 processor 顶层 import（延迟导入 CONFIG，避免循环导入）。
"""
import os
import sqlite3
import struct
import subprocess
import threading
from datetime import datetime

import numpy as np

# 每帧 dHash 位数（8 行 x 7 差分）
DHASH_BITS = 56
# 帧匹配阈值：Hamming 距离 ≤ 该值（约 25%）视为同一画面
HAMM_THRESH = 0.25
# 采样帧率 / 灰度尺寸
FPS = 1
GRAY_SIZE = 64

# popcount 查找表（uint16 -> 位数），向量化 Hamming 距离
_POPCOUNT = np.array(
    [bin(i).count('1') for i in range(65536)], dtype=np.uint8)


def _ffmpeg_path():
    """延迟取 ffmpeg 路径（避免模块加载时依赖 processor）。"""
    try:
        from processor import CONFIG
        p = CONFIG.get('ffmpeg_path') or (
            os.path.join(CONFIG.get('ffmpeg_dir', ''), 'ffmpeg.exe'))
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    # 常见兜底
    for cand in (
        r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe',
        'ffmpeg',
    ):
        if os.path.isfile(cand) or cand == 'ffmpeg':
            return cand
    return 'ffmpeg'


def extract_gray_frames(video_path, fps=FPS, size=GRAY_SIZE,
                        ffmpeg_path=None):
    """一次性导出视频灰度帧流，返回 numpy 数组 (N, size, size) uint8。

    失败返回 None（调用方保守放行）。"""
    if not video_path or not os.path.isfile(video_path):
        return None
    ff = ffmpeg_path or _ffmpeg_path()
    cmd = [ff, '-v', 'error', '-i', video_path,
           '-vf', f'fps={fps},scale={size}:{size}',
           '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    n = len(proc.stdout)
    if n == 0 or n % (size * size) != 0:
        return None
    return np.frombuffer(proc.stdout, dtype=np.uint8).reshape(
        -1, size, size)


def frame_dhash(gray):
    """单帧 56-bit 差分哈希：8x8 块均值 → 每行相邻差分。

    gray: (64,64) uint8。返回 int（56 位）。"""
    h = gray.reshape(8, 8, 8, 8).mean(axis=(1, 3))  # (8,8) 块均值
    diff = h[:, 1:] > h[:, :-1]  # (8,7)
    bits = diff.astype(np.uint8).flatten()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return val


def video_fingerprint(video_path, fps=FPS, size=GRAY_SIZE,
                      ffmpeg_path=None):
    """计算成片内容指纹：list[int]（每帧一个 56-bit 哈希）。

    失败返回 None（调用方保守放行，不因检测故障阻塞交付/上传）。"""
    frames = extract_gray_frames(video_path, fps=fps, size=size,
                                 ffmpeg_path=ffmpeg_path)
    if frames is None:
        return None
    return [int(frame_dhash(f)) for f in frames]


def _hamming_batch(a, b):
    """两 numpy uint64 数组逐元素 Hamming 距离（向量化查表）。"""
    x = a ^ b
    return (_POPCOUNT[x & 0xFFFF]
            + _POPCOUNT[(x >> 16) & 0xFFFF]
            + _POPCOUNT[(x >> 32) & 0xFFFF]
            + _POPCOUNT[(x >> 48) & 0xFFFF])


def _seq_to_u64(seq):
    return np.array(seq, dtype=np.uint64)


def similarity(a, b, ham_thresh=HAMM_THRESH):
    """两帧哈希序列的内容重合度：短序列滑过长序列，取最大匹配率 [0,1]。

    语义：结果高 = b 中存在连续区间与 a 视觉高度重合（使用相同素材）。"""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    n, m = len(a), len(b)
    A = _seq_to_u64(a)
    B = _seq_to_u64(b)
    max_ham = int(ham_thresh * DHASH_BITS)
    best = 0.0
    # 粗对齐（步长 5）再细对齐（±5），避免全窗口逐滑动
    for start in range(0, m - n + 1, 5):
        window = B[start:start + n]
        match = (_hamming_batch(A, window) <= max_ham).mean()
        if match > best:
            best = float(match)
            if best >= 1.0:
                return 1.0
    # 细对齐：在 best 位置 ±5 内步长 1 精调
    if best > 0:
        # 找 best 对应 start（重算定位）
        for start in range(0, m - n + 1):
            if start % 5 == 0:
                window = B[start:start + n]
                match = (_hamming_batch(A, window) <= max_ham).mean()
                if match >= best - 1e-9:
                    # 精调邻域
                    for s2 in range(max(0, start - 5),
                                    min(m - n + 1, start + 6)):
                        w2 = B[s2:s2 + n]
                        r2 = float((_hamming_batch(A, w2) <= max_ham).mean())
                        if r2 > best:
                            best = r2
                    break
    return best


def internal_duplicate(seq, window=10, min_gap=15, match_ratio=0.9,
                       ham_thresh=HAMM_THRESH):
    """成片内部重复段检测：window 秒块与其后间隔 ≥ min_gap 的块两两比对。

    返回 [{start_a, start_b, similarity}]。默认仅告警（短剧合法回环）。"""
    n = len(seq)
    if n < window * 2 + min_gap:
        return []
    blocks = [seq[i:i + window] for i in range(0, n - window + 1, window)]
    res = []
    for i in range(len(blocks)):
        a = blocks[i]
        start_a = i * window
        for j in range(i + 1, len(blocks)):
            b_start = j * window
            if b_start - start_a < min_gap:
                continue
            sim = similarity(a, blocks[j], ham_thresh=ham_thresh)
            if sim >= match_ratio:
                res.append({'start_a': start_a, 'start_b': b_start,
                            'similarity': round(sim, 3)})
    return res


class FingerprintStore:
    """content_fingerprints 表读写（与 upload.db 同库）。

    每操作新建连接（WAL + busy_timeout），与 upload_manager 约定一致。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        return conn

    def _init(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS content_fingerprints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        drama_name TEXT NOT NULL,
                        version INTEGER,
                        account TEXT,
                        video_path TEXT NOT NULL,
                        filename TEXT,
                        hash_seq BLOB NOT NULL,
                        duration REAL,
                        size_bytes INTEGER,
                        source TEXT DEFAULT 'cut',
                        uploaded INTEGER DEFAULT 0,
                        uploaded_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(video_path)
                    )
                ''')
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _seq_to_blob(seq):
        return b''.join(struct.pack('>Q', v) for v in seq)

    @staticmethod
    def _blob_to_seq(blob):
        if not blob:
            return []
        return [struct.unpack('>Q', blob[i:i + 8])[0]
                for i in range(0, len(blob), 8)]

    def upsert(self, drama_name, video_path, seq, version=None,
               account=None, filename=None, duration=None,
               size_bytes=None, source='cut'):
        """写入/更新指纹（同一 video_path 覆盖，供重切后刷新）。"""
        if not seq:
            return False
        with self._lock:
            conn = self._connect()
            try:
                conn.execute('''
                    INSERT INTO content_fingerprints
                    (drama_name, version, account, video_path, filename,
                     hash_seq, duration, size_bytes, source, uploaded, uploaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                    ON CONFLICT(video_path) DO UPDATE SET
                        drama_name=excluded.drama_name,
                        version=excluded.version,
                        account=excluded.account,
                        filename=excluded.filename,
                        hash_seq=excluded.hash_seq,
                        duration=excluded.duration,
                        size_bytes=excluded.size_bytes,
                        source=excluded.source,
                        uploaded=0,
                        uploaded_at=NULL
                ''', (drama_name, version, account, video_path, filename,
                      self._seq_to_blob(seq), duration, size_bytes, source))
                conn.commit()
                return True
            finally:
                conn.close()

    def mark_uploaded(self, video_path, account=None):
        """上传成功后登记 uploaded=1。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute('''
                    UPDATE content_fingerprints
                    SET uploaded=1, uploaded_at=?
                    WHERE video_path=?
                ''', (datetime.now().isoformat(), video_path))
                conn.commit()
            finally:
                conn.close()

    def get_by_path(self, video_path):
        """按路径取指纹行（dict）或 None。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute('''
                    SELECT * FROM content_fingerprints WHERE video_path=?
                ''', (video_path,)).fetchone()
                if row is None:
                    return None
                d = dict(row)
                d['hash_seq'] = self._blob_to_seq(d['hash_seq'])
                return d
            finally:
                conn.close()

    def find_similar(self, account, seq, threshold, exclude_path=None,
                     include_pending=False):
        """查找与 seq 内容相似（≥threshold）的已上传/已入队指纹。

        account=None 时跨全部账号查询，用于防止同一作品换账号重复发布；
        传入账号时保持原有同账号语义。

        include_pending: 是否包含未上传（pending/uploading）记录（入队查重用）。
        返回 [{drama_name, version, account, video_path, filename, uploaded,
                similarity}]，按相似度降序。"""
        if not seq:
            return []
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                where = []
                values = []
                if account is not None:
                    where.append('account=?')
                    values.append(account)
                if exclude_path:
                    where.append('video_path != ?')
                    values.append(exclude_path)
                sql = 'SELECT * FROM content_fingerprints'
                if where:
                    sql += ' WHERE ' + ' AND '.join(where)
                rows = conn.execute(sql, tuple(values)).fetchall()
            finally:
                conn.close()
        out = []
        for r in rows:
            d = dict(r)
            if not include_pending and not d['uploaded']:
                continue
            other = self._blob_to_seq(d['hash_seq'])
            sim = similarity(seq, other)
            if sim >= threshold:
                out.append({
                    'drama_name': d['drama_name'],
                    'version': d['version'],
                    'account': d['account'],
                    'video_path': d['video_path'],
                    'filename': d['filename'],
                    'uploaded': d['uploaded'],
                    'similarity': round(sim, 3),
                })
        out.sort(key=lambda x: -x['similarity'])
        return out


# 默认阈值（设计 2.4；CONFIG 可覆盖）
def dedup_thresholds(config=None):
    """读取内容去重阈值（CONFIG['content_dedup']，缺省保守默认）。"""
    cfg = {}
    if config:
        cfg = config.get('content_dedup', {}) or {}
    return {
        'hard': float(cfg.get('hard', 0.75)),     # 生成侧硬剔除
        'soft': float(cfg.get('soft', 0.5)),      # 生成侧疑似告警
        'upload': float(cfg.get('upload', 0.6)),  # 上传侧拒收
    }
