# -*- coding: utf-8 -*-
"""
上传队列管理器
功能：管理批量上传任务、异步处理、状态查询、日志记录
"""
import os
import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import sqlite3


def _connect_db(path):
    """打开 SQLite 连接并启用 WAL + busy_timeout，降低并发写 'database is locked'。

    WAL 允许读写并发（默认 rollback journal 读写互斥，worker/清理/API 多线程
    并发写同一 db 时互锁抛 OperationalError）；busy_timeout 让写冲突等待而非
    立即失败。PRAGMA 不跨连接持久，故每次新建连接都要设置。
    """
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass  # WAL 失败不致命（如只读介质），回落默认 rollback journal
    return conn

# 导入上传模块
from wechat_uploader import WeChatChannelsUploader

# 导入配置
from config import CONFIG
from log_utils import make_entry, context_from_task, get_logger
from base_queue import BaseQueueManager

# DrissionPage 已随 wechat_uploader 加载；此处模块级导入便于 mock 与复用
from DrissionPage import ChromiumPage, ChromiumOptions


def _account_login_state(uploader, account=''):
    """三态登录校验，兼容只实现 check_login() 的旧替身/脚本。

    返回 'logged_in' / 'logged_out' / 'busy'。任何异常按 'busy' 处理——
    “测不了”不等于“未登录”，调用方应保持队列而不是把任务标失败。
    """
    try:
        fn = getattr(uploader, 'login_state', None)
        if callable(fn):
            st = fn()
            if st in ('logged_in', 'logged_out', 'busy'):
                return st
        return 'logged_in' if uploader.check_login() else 'logged_out'
    except Exception as e:
        get_logger('upload').warning(f'登录校验异常 {account}: {e}')
        return 'busy'

class UploadManager(BaseQueueManager):
    """上传队列管理器"""
    
    def __init__(self, data_dir: str = None):
        super().__init__()
        self.data_dir = data_dir or CONFIG.get('data_dir', 'data')
        self.upload_log_file = os.path.join(self.data_dir, 'upload_log.json')

        # 数据库路径
        self.db_path = os.path.join(self.data_dir, 'upload.db')
        self._init_database()

        # 子类特有运行标志（公共 queue/current_task/paused/completed/failed/logs/lock 由基类初始化）
        self.uploading = False  # 是否正在上传
        self.prechecking = False  # 是否正在预检登录
        self.upload_thread = None
        self.accounts = []
        self.current_account = None

        # 账号级登录验证时间戳（account → time.time()）：批次预检/上传成功后刷新。
        # 供 _execute_upload 决定是否跳过 upload_to_draft 内的重复登录校验
        # （原实现把该缓存挂在上传器实例上，而每任务都新建实例，缓存从未生效）。
        self._login_verified = {}
        # 批量提交的内容指纹计算进度（供 /api/upload/status 展示）：
        # {'phase': 'fingerprint', 'done': n, 'total': N} 或 None
        self.batch_progress = None
        
        # 草稿箱浏览器实例（account → ChromiumPage）
        self.draft_pages = {}
        
        # 后端触发的自动清理：本次运行是否启用、本批次是否人工停止、
        # 以及批次完成回调（由 app.py 注入 artifact_cleanup.execute_cleanup）
        # _run_epoch: 每次 start_upload 自增，标识一次运行；_stopped/_stop_epoch
        # 标记该次运行被人工停止。旧 worker 结束时用自己运行的 epoch 判定，
        # 不会读到新运行的标志（并发窗口 P1 修复）。
        self._run_auto_cleanup = False
        self._scheduled_publish = False
        self._stopped = False
        self._cleanup_callback = None
        self._run_epoch = 0
        self._stop_epoch = -1

        # 共享资料库客户端（保存草稿前填剧集链接用）；由 app.py 注入
        self._dramaid_client = None

        # 服务重启后把未完成记录恢复到内存队列；否则数据库有任务但界面为空，
        # start_upload 会长期报“队列为空”。正在上传的记录统一降回 pending。
        self._restore_unfinished_queue()

    def set_cleanup_callback(self, cb):
        """注入批次完成回调（batch_id → None）。app.py 绑定 artifact_cleanup。"""
        self._cleanup_callback = cb

    def set_dramaid_client(self, client):
        """注入共享资料库客户端（dramaid.lol 查询 + 剧集 ID 绑定）。

        保存草稿前填剧集链接用：任务有绑定 ID 用之，否则上传时自动查。
        """
        self._dramaid_client = client

    def _bound_id(self, drama_name):
        """入队时取该剧已绑定的剧集 ID 快照（用于展示与上传）。

        无 client / 未绑定 → (None, 'none')，上传时再自动查首条（A3 混合语义）。
        """
        client = getattr(self, '_dramaid_client', None)
        if client is None:
            return None, 'none'
        try:
            b = client.get_bound(drama_name)
            if b and b.get('drama_id'):
                return b['drama_id'], 'bound'
        except Exception:
            pass
        return None, 'none'

    def _resolve_task_drama_id(self, task):
        """上传前解析剧集 ID（A3 混合语义）：绑定优先，未绑定自动查首条。

        统一返回 dict（自描述，避免位置元组元数不一致）：
          {drama_id: str|None, source: str, matched_name: str|None, error: str|None}
          source ∈ bound|autolookup|none|error；matched_name/error 仅日志展示用。
        任何失败不抛异常——调用方跳过填链接、照常保存草稿。

        注：dramaid_client.resolve() 返回 4 元组 (drama_id, source, matched_name,
        error)；本函数显式解包并归一成 dict，防止上游改元数时静默错位。
        """
        if task.get('drama_id'):
            return {
                'drama_id': task['drama_id'],
                'source': task.get('drama_id_source') or 'bound',
                'matched_name': task.get('drama_name'),
                'error': None,
            }
        client = getattr(self, '_dramaid_client', None)
        if client is None:
            return {'drama_id': None, 'source': 'none',
                    'matched_name': None, 'error': None}
        name = task.get('drama_name') or ''
        # 网络/上游瞬时失败重试（退避 2s/4s/6s），避免把「能查到的剧集 ID」因
        # 抖动误判成查不到而跳过填链接；真查不到（source='none'）不重试。
        last = {'drama_id': None, 'source': 'none', 'matched_name': None, 'error': None}
        for attempt in range(4):
            try:
                drama_id, source, matched_name, error = client.resolve(name)
                last = {'drama_id': drama_id, 'source': source,
                        'matched_name': matched_name, 'error': error}
                if source != 'error':
                    break
            except Exception as exc:
                last = {'drama_id': None, 'source': f'error:{exc}',
                        'matched_name': None, 'error': str(exc)}
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
        return last

    def _restore_unfinished_queue(self):
        """从 upload_records 恢复 pending/uploading 任务。"""
        conn = _connect_db(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM upload_records "
                "WHERE status IN ('pending','uploading') ORDER BY id"
            ).fetchall()
            if not rows:
                return 0
            stale_ids = [r['id'] for r in rows if r['status'] == 'uploading']
            if stale_ids:
                marks = ','.join('?' for _ in stale_ids)
                conn.execute(
                    f"UPDATE upload_records SET status='pending', started_at=NULL, "
                    f"error_message='服务重启，已恢复到待上传队列' WHERE id IN ({marks})",
                    stale_ids,
                )
                conn.commit()

            for row in rows:
                try:
                    topics = json.loads(row['topics']) if row['topics'] else []
                except (TypeError, ValueError):
                    topics = []
                task = {
                    'id': row['id'],
                    'drama_name': row['drama_name'],
                    'version': row['version'],
                    'video_path': row['video_path'],
                    'cover_path': row['cover_path'],
                    'title': row['title'],
                    'description': row['description'],
                    'topics': topics,
                    'account': row['account'],
                    'status': 'pending',
                    'retry_count': row['retry_count'] or 0,
                    'batch_id': row['batch_id'],
                    'source_dir': row['source_dir'],
                    'output_dir': row['output_dir'],
                    'publish_time': row['publish_time'],
                    'cover_status': row['cover_status'] or 'not_requested',
                    'draft_saved': bool(row['draft_saved']),
                    'drama_id': row['drama_id'],
                    'drama_id_source': row['drama_id_source'] or 'none',
                    'restored_after_restart': True,
                }
                self.queue.append(task)
            self.current_account = self.queue[0].get('account')
            self._log('info', f'已恢复 {len(self.queue)} 个未完成上传任务')
            return len(self.queue)
        finally:
            conn.close()
        
    def _init_database(self):
        """初始化数据库"""
        conn = _connect_db(self.db_path)
        cursor = conn.cursor()
        
        # 创建上传记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS upload_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drama_name TEXT NOT NULL,
                version INTEGER NOT NULL,
                video_path TEXT NOT NULL,
                cover_path TEXT,
                title TEXT,
                description TEXT,
                topics TEXT,
                account TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')
        
        # 迁移：为既有表补充封面/清理相关列（幂等，列已存在则跳过）
        new_columns = {
            'batch_id': 'TEXT',
            'source_dir': 'TEXT',
            'output_dir': 'TEXT',
            'cover_status': "TEXT DEFAULT 'not_requested'",
            'draft_saved': 'INTEGER DEFAULT 0',
            'draft_verified_at': 'TIMESTAMP',
            'cleanup_status': "TEXT DEFAULT 'none'",
            'cleanup_error': 'TEXT',
            'publish_time': 'TEXT',  # 计划发表时间（当天+day_offset 日，槽位按版本号循环）
            'drama_id': 'TEXT',  # 剧集 ID（event/... 格式，保存草稿前填剧集链接用）
            'drama_id_source': "TEXT DEFAULT 'none'",  # bound|autolookup|none|error
            'published': 'INTEGER DEFAULT 0',  # 定时发表（选定时→点发表）成功标志
        }
        existing_cols = {row[1] for row in cursor.execute('PRAGMA table_info(upload_records)').fetchall()}
        for col, ddl in new_columns.items():
            if col not in existing_cols:
                try:
                    cursor.execute(f'ALTER TABLE upload_records ADD COLUMN {col} {ddl}')
                except sqlite3.OperationalError as e:
                    # 列已存在（并发）——忽略
                    if 'duplicate column' not in str(e).lower():
                        raise
        
        # 创建上传批次表（一次批量上传一个 batch_id，聚合多账号/多版本）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS upload_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                account TEXT,
                drama_name TEXT NOT NULL,
                source_dir TEXT,
                output_dir TEXT,
                version_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                cover_applied_count INTEGER DEFAULT 0,
                cover_failed_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                auto_cleanup INTEGER DEFAULT 0,   -- 批次自然结束后是否由后端触发自动清理
                scheduled INTEGER DEFAULT 0,      -- 本批次是否定时发表（False=保存草稿）
                cleanup_status TEXT,              -- 清理整体状态：success/partial/failed
                cleanup_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')
        
        # 迁移：为既有批次表补充 auto_cleanup / scheduled / cleanup_status 列（幂等）
        batch_cols = {row[1] for row in cursor.execute('PRAGMA table_info(upload_batches)').fetchall()}
        if 'auto_cleanup' not in batch_cols:
            try:
                cursor.execute('ALTER TABLE upload_batches ADD COLUMN auto_cleanup INTEGER DEFAULT 0')
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e).lower():
                    raise
        if 'scheduled' not in batch_cols:
            # L3：定时发表模式随批次落库，resume/重启后从批次行恢复，
            # 不依赖 upload_manager 单例的内存实例属性（否则跨批次串号）。
            try:
                cursor.execute('ALTER TABLE upload_batches ADD COLUMN scheduled INTEGER DEFAULT 0')
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e).lower():
                    raise
        for _c, _ddl in (('cleanup_status', 'TEXT'), ('cleanup_error', 'TEXT')):
            if _c not in batch_cols:
                try:
                    cursor.execute(f'ALTER TABLE upload_batches ADD COLUMN {_c} {_ddl}')
                except sqlite3.OperationalError as e:
                    if 'duplicate column' not in str(e).lower():
                        raise

        # 创建清理任务表（持久化，服务重启后仍能续跑）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS artifact_cleanup_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                account TEXT,
                drama_name TEXT NOT NULL,
                job_type TEXT NOT NULL,       -- version | drama
                version INTEGER,
                target_path TEXT NOT NULL,    -- 要清理的路径（文件或目录）
                recycle_path TEXT,            -- 移动目标（回收站内）
                manifest_path TEXT,           -- 清理前写的 manifest
                status TEXT DEFAULT 'pending',  -- pending | moved | failed | skipped
                strategy TEXT DEFAULT 'recycle_on_full_batch',
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')

        # 批次应上传版本清单（按 批次/账号/剧名/目录 分组记录提交意图）。
        # 目录级清理前必须确认该组全部目标版本都成功，缺版本则禁止清理。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS upload_batch_expected (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                account TEXT,
                drama_name TEXT NOT NULL,
                source_dir TEXT,
                output_dir TEXT,
                expected_versions TEXT,       -- JSON 数组：该组应上传的全部版本
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (batch_id, account, drama_name, source_dir, output_dir)
            )
        ''')
        
        # 创建索引
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_drama_version 
            ON upload_records(drama_name, version)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_status 
            ON upload_records(status)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_batch_id 
            ON upload_records(batch_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_cleanup_batch 
            ON artifact_cleanup_jobs(batch_id)
        ''')
        
        conn.commit()
        conn.close()

        
    def _duplicate_by_path(self, video_path, account):
        """P2-10 幂等：同账号同一成片文件是否已入队/已上传。

        (剧,版本,账号) 查重挡不住“同一文件换了版本号/剧名再入队”，
        这里以文件路径为幂等键兜底，防止重复发布被判重或同稿二次发布。
        """
        if not video_path:
            return None
        try:
            with _connect_db(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT id, drama_name, version, status FROM upload_records
                    WHERE account=? AND video_path=?
                      AND status IN ('pending','uploading','completed')
                    ORDER BY id DESC LIMIT 1
                ''', (account, video_path))
                return cur.fetchone()
        except Exception:
            return None

    def _check_content_duplicate(self, video_path, account, seq=None):
        """上传前内容查重：跨账号的已上传/已入队成片相似时返回冲突 dict。

        查重范围不再局限于目标账号：同一作品换账号发布同样可能被平台判重。
        跨剧同样命中（堵同素材被多剧复用 / 同一作品重传）。指纹计算失败
        返回 None（保守放行，不因检测故障阻塞上传）。

        只拦截「真正上传过（uploaded=1 或 upload_records 有 completed 记录）
        或当前入队中（pending/uploading）」的内容。纯生成期指纹（从未入队/
        上传）不拦截——否则从未上传的成片会被自己生成期的指纹自匹配
        （相似度 1.0）误拒，表现为「点了上传却不开始」。

        seq: 已预先算好的视频指纹（add_batch 在锁外批量预计算，避免在锁内跑
             ffmpeg 把 status 接口一起堵死）；None 时现场计算。
        """
        if not video_path or not os.path.isfile(video_path):
            return None
        try:
            from content_fingerprint import (video_fingerprint,
                                             FingerprintStore,
                                             dedup_thresholds)
            if seq is None:
                seq = video_fingerprint(video_path)
            if not seq:
                return None
            th = dedup_thresholds(CONFIG)
            store = FingerprintStore(
                os.path.join(CONFIG.get('data_dir', 'data'), 'upload.db'))
            # account=None：同时检查本机两个发布账号，避免 A 账号发过、B 账号
            # 再发同一内容仍通过本地查重。
            hits = store.find_similar(None, seq, th['upload'],
                                      exclude_path=video_path,
                                      include_pending=True)
            for h in hits:
                if self._fingerprint_blocks(h):
                    return h
        except Exception as e:
            self._log('warn', f'内容查重异常，放行：{e}')
        return None

    def _fingerprint_blocks(self, hit):
        """指纹是否代表「已上传或已入队」的内容（纯生成期指纹不拦截）。

        upload_records 是「已传/入队」的可靠来源（指纹表的 uploaded 标志
        因路径不一致长期为 0，不可依赖）；查询失败保守放行（返回 False）。
        """
        if hit.get('uploaded'):
            return True
        try:
            with _connect_db(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT 1 FROM upload_records
                    WHERE drama_name=? AND version=? AND account=?
                      AND status IN ('pending','uploading','completed')
                    LIMIT 1
                ''', (hit.get('drama_name'), hit.get('version'),
                      hit.get('account')))
                return cur.fetchone() is not None
        except Exception:
            return False

    def _mark_fingerprint_uploaded(self, video_path):
        """R9 上传成功后登记指纹 uploaded=1（失败不阻断流程）。"""
        try:
            from content_fingerprint import FingerprintStore
            FingerprintStore(
                os.path.join(CONFIG.get('data_dir', 'data'),
                             'upload.db')).mark_uploaded(video_path)
        except Exception:
            pass

    def _sync_drama_archive(self, task, outcome):
        """上传完成后联动剧级档案（drama_archive）。

        手动上传的剧可能从未进入剧级档案（无 download/slice 记录），
        上传成功/失败时 upsert 确保行存在，让剧级档案能看到该剧及其上传状态。
        upload_state 由 archive.aggregate_upload_state 查询时实时聚合（无 upload_state 列）。
        outcome: 'done'（上传成功）/ 'failed'（上传失败）——仅写日志，状态来自聚合。
        """
        try:
            from archive import upsert_archive
            upsert_archive(
                task['drama_name'],
                account=task.get('account'),
                local_dir=task.get('output_dir') or task.get('source_dir'))
        except Exception as e:
            self._log('warning', f'剧级档案联动失败：{task["drama_name"]} - {e}')

    @staticmethod
    def _compute_publish_time(version) -> str:
        """【兼容保留】按版本号取槽的旧算法：当天+day_offset 日的 times 槽位循环。

        只按版本号取槽会让「多部剧同版本」撞同一时刻（已由 plan_publish_times
        取代）；保留它是为了旧调用/测试兼容，新入队一律走批次级分配。
        配置异常回退 当天+1 日、21:00。
        """
        try:
            from config import get_publish_schedule
            sched = get_publish_schedule()
        except Exception:
            sched = {}
        day_offset = int(sched.get('day_offset') or 1)
        times = sched.get('times') or ['21:00']
        try:
            slot = times[(int(version) - 1) % len(times)]
        except Exception:
            slot = times[0]
        target = (datetime.now() + timedelta(days=day_offset)).date()
        return f'{target.isoformat()} {slot}'

    # ── 批次级定时发表时间分配 ─────────────────────────────────────────
    # 旧逻辑（_compute_publish_time）按版本号取槽：5 部剧的 V1 全部拿到 times[0]，
    # 同一天同一刻齐发。这里改为「起始时间 + 固定间隔 + 小抖动 + 避让已占用」，
    # 同一账号内逐条顺延；跨账号各自排各的序列（不同账号同时发不冲突）。
    MAX_PUBLISH_LEAD_DAYS = 14      # 平台日历可选范围按 14 天预留，超界日格会禁用
    MIN_PUBLISH_GAP_MINUTES = 30    # 同账号两条定时发表的最小间隔

    @staticmethod
    def _publish_defaults() -> Dict:
        """发布计划缺省值：日期=今天+day_offset，起点=config 首个槽位。"""
        sched = {}
        try:
            from config import get_publish_schedule
            sched = get_publish_schedule() or {}
        except Exception:
            pass
        times = [str(t).strip() for t in (sched.get('times') or []) if str(t).strip()]
        jitter = sched.get('jitter_minutes')
        try:
            jitter = 10 if jitter is None else int(jitter)   # 0 是合法值（关闭抖动），不能用 or
        except (TypeError, ValueError):
            jitter = 10
        return {
            'day_offset': int(sched.get('day_offset') or 1),
            'start_time': times[0] if times else '07:00',
            'window_end': str(sched.get('window_end') or (times[-1] if times else '23:00')),
            'interval_minutes': int(sched.get('interval_minutes') or 120),
            'jitter_minutes': jitter,
        }

    @classmethod
    def _normalize_publish_options(cls, options) -> Dict:
        """校验并补全发布计划；任何非法值回退缺省，绝不抛异常。

        options: {date:'YYYY-MM-DD', start_time:'HH:MM', window_end:'HH:MM',
                  interval_minutes:int, jitter_minutes:int}（都可缺省）
        """
        defaults = cls._publish_defaults()
        opt = options if isinstance(options, dict) else {}
        now = datetime.now()
        today = now.date()

        date_str = str(opt.get('date') or '').strip()
        try:
            day = datetime.strptime(date_str, '%Y-%m-%d').date()
        except Exception:
            day = today + timedelta(days=defaults['day_offset'])
        if day < today:
            day = today
        max_day = today + timedelta(days=cls.MAX_PUBLISH_LEAD_DAYS)
        if day > max_day:
            day = max_day

        start = str(opt.get('start_time') or defaults['start_time']).strip()
        try:
            datetime.strptime('2000-01-01 ' + start, '%Y-%m-%d %H:%M')
        except Exception:
            start = defaults['start_time']

        # 每日发布窗口终点：超出就顺延到次日起始时间（避免凌晨 1 点、3 点还在发）
        end = str(opt.get('window_end') or defaults['window_end']).strip()
        try:
            datetime.strptime('2000-01-01 ' + end, '%Y-%m-%d %H:%M')
        except Exception:
            end = defaults['window_end']
        if end <= start:   # 字符串 HH:MM 可直接比大小（等长补零）
            end = defaults['window_end']
            if end <= start:
                end = '23:59'

        try:
            interval = int(opt.get('interval_minutes') or defaults['interval_minutes'])
        except Exception:
            interval = defaults['interval_minutes']
        interval = max(1, min(1440, interval))

        jitter_raw = opt.get('jitter_minutes')
        try:
            jitter = int(defaults['jitter_minutes']) if jitter_raw is None else int(jitter_raw)
        except Exception:
            jitter = defaults['jitter_minutes']
        # 抖动上限 interval/3：抖动大于半个间隔会把顺序打乱、甚至跨天
        jitter = max(0, min(interval // 3, jitter))
        jitter = min(jitter, 120)

        # 选了今天但起始时间已过 → 整批顺延到明天（与 _ensure_future_publish_time 口径一致，
        # 否则首条会被夹到「现在+5分钟」，后面各条反而排到首条之前）
        if day == today:
            try:
                start_dt = datetime.strptime(f'{day.isoformat()} {start}', '%Y-%m-%d %H:%M')
                if start_dt <= now + timedelta(minutes=5):
                    day = day + timedelta(days=1)
            except Exception:
                pass

        return {'date': day, 'start_time': start, 'window_end': end,
                'mode': (str(opt.get('mode') or 'sequence')
                         if str(opt.get('mode') or 'sequence') in
                         ('sequence', 'per_drama_day') else 'sequence'),
                'interval_minutes': interval, 'jitter_minutes': jitter}

    @classmethod
    def plan_publish_times(cls, entries, options=None, taken_by_account=None,
                           rng=None) -> List[str]:
        """为整批任务分配定时发表时间，返回与 entries 等长的 'YYYY-MM-DD HH:MM'。

        entries: [{'account': 账号, 'drama': 剧名}]，列表顺序 = 发表顺序
        taken_by_account: {账号: [datetime, ...]} 已占用时刻（库里的计划时间）

        两种排期方式（options.mode）：
        - sequence（默认）：同一账号从起始时间起按 interval 连续排，跨天顺延；
        - per_drama_day：每部剧占一天——剧内按 interval 排，排满当天窗口顺延
          次日继续（同一部剧保持连续），下一部剧从再下一天起始时间开始。

        通用规则：
        1) 每条在 ±jitter 分钟内随机（jitter ≤ interval/3，顺序不乱）；
        2) 与已占用时刻相差 < MIN_PUBLISH_GAP 分钟则后移一个间隔，直到空出；
        3) 只在每日窗口 [start_time, window_end] 内发，超出顺延到次日起始时间；
        4) 结果必然晚于「现在+5分钟」。
        """
        rng = rng or random
        opts = cls._normalize_publish_options(options)
        entries = list(entries or [])
        gap = cls.MIN_PUBLISH_GAP_MINUTES
        interval = opts['interval_minutes']
        jitter = opts['jitter_minutes']
        mode = opts['mode']
        day0 = opts['date']
        start_h, start_m = (int(x) for x in opts['start_time'].split(':'))
        end_h, end_m = (int(x) for x in opts['window_end'].split(':'))
        start_min = start_h * 60 + start_m
        end_min = end_h * 60 + end_m
        earliest = datetime.now() + timedelta(minutes=5)
        step = max(interval, gap)

        occupied = {}
        lasts = {}   # 账号 -> 上一条已排定的时刻（保证同账号严格递增）
        for acc, times in (taken_by_account or {}).items():
            occupied[acc or ''] = [t for t in (times or []) if isinstance(t, datetime)]

        def resolve(cand, busy):
            """撞点则按固定间隔后移，并重新夹回每日窗口。"""
            for _ in range(400):
                if not any(abs((cand - t).total_seconds()) < gap * 60 for t in busy):
                    return cand
                cand = cls._roll_into_window(
                    cand + timedelta(minutes=step), start_h, start_m, end_h, end_m)
            return cand

        results = []
        if mode == 'per_drama_day':
            # 每个账号独立：同一部剧连续排，换剧 +1 天，剧内超出窗口也顺延
            states = {}
            for entry in entries:
                entry = entry or {}
                account = entry.get('account') or ''
                drama = entry.get('drama') or ''
                st = states.get(account)
                if st is None:
                    st = {'day': day0, 'slot': 0, 'drama': drama}
                    states[account] = st
                elif drama and drama != st['drama']:
                    st['day'] = st['day'] + timedelta(days=1)
                    st['slot'] = 0
                    st['drama'] = drama

                while True:
                    base = (datetime.combine(st['day'], datetime.min.time())
                            + timedelta(minutes=start_min + st['slot'] * interval))
                    if base.hour * 60 + base.minute > end_min:
                        st['day'] += timedelta(days=1)
                        st['slot'] = 0
                        continue
                    break
                cand = base
                if jitter:
                    cand += timedelta(minutes=rng.randint(-jitter, jitter))
                cand = max(cand, earliest)
                cand = cls._roll_into_window(cand, start_h, start_m, end_h, end_m)
                busy = occupied.setdefault(account, [])
                cand = resolve(cand, busy)
                # 保证同一账号的发表顺序严格递增：上一版被窗口顺延/避让推后时，
                # 本版不能再排到它前面（否则 V2 会抢在 V1 之前发）。
                last = lasts.get(account)
                if last is not None and cand <= last:
                    cand = cls._roll_into_window(
                        last + timedelta(minutes=interval),
                        start_h, start_m, end_h, end_m)
                    cand = resolve(cand, busy)

                busy.append(cand)
                lasts[account] = cand
                st['day'] = cand.date()
                st['slot'] = max(0, ((cand.hour * 60 + cand.minute) - start_min)
                                 // interval) + 1
                results.append(cand.strftime('%Y-%m-%d %H:%M'))
            return results

        slots = {}   # 账号 -> 已排条数（网格位置）
        for entry in entries:
            entry = entry or {}
            account = entry.get('account') or ''
            idx = slots.get(account, 0)
            slots[account] = idx + 1

            # 固定在「起始时间 + idx*间隔」的网格上，抖动只作用于本条，
            # 不累加到下一条（否则随机游走会越漂越远）。
            base = (datetime.combine(day0, datetime.min.time())
                    + timedelta(hours=start_h, minutes=start_m)
                    + timedelta(minutes=idx * interval))
            if jitter:
                base += timedelta(minutes=rng.randint(-jitter, jitter))
            cand = max(base, earliest)
            cand = cls._roll_into_window(cand, start_h, start_m, end_h, end_m)

            busy = occupied.setdefault(account, [])
            cand = resolve(cand, busy)
            # 同账号严格递增：窗口顺延/避让可能把前一条推到很晚，
            # 后一条不能因此排到它前面。
            last = lasts.get(account)
            if last is not None and cand <= last:
                cand = cls._roll_into_window(
                    last + timedelta(minutes=interval),
                    start_h, start_m, end_h, end_m)
                cand = resolve(cand, busy)
            busy.append(cand)
            lasts[account] = cand
            results.append(cand.strftime('%Y-%m-%d %H:%M'))
        return results

    @staticmethod
    def _roll_into_window(dt, start_h, start_m, end_h, end_m):
        """把时刻夹进每日发布窗口；晚于终点则顺延到次日起始时间。

        目的：多剧/多版本一批排到第 10 条以后时，不会出现凌晨 01:00、03:00
        这种没人看的时间——排满当天窗口就顺延次日 07:00 继续。
        """
        start_min = start_h * 60 + start_m
        end_min = end_h * 60 + end_m
        for _ in range(30):
            tmin = dt.hour * 60 + dt.minute
            if tmin < start_min:
                dt = dt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
                continue
            if tmin <= end_min:
                return dt
            dt = (dt + timedelta(days=1)).replace(
                hour=start_h, minute=start_m, second=0, microsecond=0)
        return dt

    def _taken_publish_times(self, accounts, day, span_days=3) -> Dict[str, List[datetime]]:
        """查同账号在 [day, day+span_days) 内已占用的计划时间。

        pending/uploading/completed 都算：completed 里定时已设但尚未到点的视频
        会按该时刻发表，再排同一刻就是用户抱怨的「同一时间齐发」。
        失败记录不占位（会被重传重新排期）。跨天查询是必需的——一批排满当天
        窗口会顺延到次日，次日已有的排期同样要避让。
        """
        accounts = [a for a in (accounts or []) if a]
        out: Dict[str, List[datetime]] = {}
        if not accounts:
            return out
        start = day.isoformat()
        end = (day + timedelta(days=max(1, span_days))).isoformat()
        try:
            with _connect_db(self.db_path) as conn:
                marks = ','.join('?' for _ in accounts)
                rows = conn.execute(
                    f"SELECT account, publish_time FROM upload_records "
                    f"WHERE account IN ({marks}) AND publish_time >= ? "
                    f"AND publish_time < ? "
                    f"AND status IN ('pending','uploading','completed')",
                    list(accounts) + [start, end]).fetchall()
            for account, pt in rows:
                try:
                    out.setdefault(account or '', []).append(
                        datetime.strptime(str(pt)[:16], '%Y-%m-%d %H:%M'))
                except Exception:
                    continue
        except Exception as e:
            # 查库失败不让入队失败：退化为「本批内部不撞点」
            logging.getLogger(__name__).warning(f'读取已占用发布时间失败: {e}')
        return out

    def _next_publish_time(self, account, drama=None) -> str:
        """单条入队的缺省计划时间：取该账号同日最近的空档。"""
        opts = self._normalize_publish_options(None)
        taken = self._taken_publish_times([account], opts['date'])
        return self.plan_publish_times(
            [{'account': account, 'drama': drama or ''}], opts, taken)[0]

    @staticmethod
    def _read_edl_title(video_path, version):
        """从同目录 edl_config_v{version}.json 读取 AI 生成标题；无则返回 None。

        标题嵌在 config['videos'][0]['title']（旧扁平结构回退 config['title']）。
        """
        try:
            config_path = os.path.join(
                os.path.dirname(video_path), f'edl_config_v{version}.json')
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                if isinstance(config, dict):
                    vi = config.get('videos') or [{}]
                    title = (vi[0] if isinstance(vi, list) and vi else {}).get('title') \
                        or config.get('title')
                    if title:
                        return str(title).strip()
        except Exception:
            pass
        return None

    @staticmethod
    def _fallback_title(drama_name, version) -> str:
        """edl_config 缺失时的兜底标题：剧名(去标点截断)+第N集，6-16 字无标点。"""
        import re as _re
        base = _re.sub(r'[^\w\u4e00-\u9fff]', '', str(drama_name or ''))
        title = f'{base[:9]}第{version}集'
        return title[:16] if len(title) > 16 else (title if len(title) >= 6 else f'第{version}集精彩片段')

    def add_to_queue(self, drama_name: str, video_path: str, cover_path: str = None,
                     title: str = None, description: str = None, topics: str = None,
                     account: str = None, version: int = 1,
                     batch_id: str = None, source_dir: str = None,
                     output_dir: str = None,
                     publish_time: str = None,
                     expected_versions: Optional[list] = None) -> Dict:
        """添加到上传队列

        expected_versions: 该组本次应上传的全部版本（透传进任务字典，
        供批次完成标记/清理门槛使用；默认 None 表示未提供）。
        publish_time: 计划发表时间（YYYY-MM-DD HH:MM）。缺省时按该账号当日
        已占用时间自动排下一个空档（起始时间/间隔/抖动见 _normalize_publish_options）。
        """
        # 标题缺省时从 edl_config_v{version}.json 读取（切片时 AI 生成的文案）
        if not title:
            title = self._read_edl_title(video_path, version)
        with self.lock:
            # 检查是否已存在（按 account 去重，两个号可以发同一部剧）
            _account = account or '福桃说剧'
            with _connect_db(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, status FROM upload_records 
                    WHERE drama_name=? AND version=? AND account=? AND status IN ('pending', 'uploading', 'completed')
                ''', (drama_name, version, _account))
                existing = cursor.fetchone()
            
            if existing:
                if existing[1] == 'completed':
                    # 2026-11-10 人鱼赴归潮 V5 实测：同剧同版本同账号已上传完成，
                    # 因指纹表无该行 + uploaded 标志失效，纯指纹查重漏拦。
                    # 精确查重（不依赖指纹）直接拒绝。
                    return {'status': 'failed',
                            'error': f'[{_account}] {drama_name} V{version} 已上传完成'
                                     f'（记录 #{existing[0]}），拒绝重复上传'}
                return {'status': 'failed', 'error': f'[{_account}] V{version}已在队列中'}

            # P2-10 幂等：同一成片文件（即使换了版本号）不再重复入队
            dup_by_path = self._duplicate_by_path(video_path, _account)
            if dup_by_path:
                return {'status': 'failed',
                        'error': (f'[{_account}] 该成片已入队/已上传（记录 '
                                  f'#{dup_by_path[0]} {dup_by_path[1]} '
                                  f'V{dup_by_path[2]} {dup_by_path[3]}），跳过重复入队')}

            # R9 内容级查重：同账号已传/已入队内容相似成片 → 拒绝（防平台判重）
            cdup = self._check_content_duplicate(video_path, _account)
            if cdup:
                up_th = CONFIG.get('content_dedup', {}).get('upload', 0.6)
                return {'status': 'failed',
                        'error': (f'内容查重拒绝：与已传/已入队的'
                                  f'[{cdup.get("account")}] '
                                  f'{cdup["drama_name"]} V{cdup["version"]} '
                                  f'相似度 {cdup["similarity"]} ≥ {up_th}，'
                                  f'拒绝重复上传（宁可少传不可封号）')}

            # topics 统一成 list 存内存，db 里 json.dumps 存（避免 list 被 str 化成 "[...]"）
            if isinstance(topics, str):
                topics_list = [t.strip() for t in topics.split(',') if t.strip()]
            elif isinstance(topics, list):
                topics_list = topics
            else:
                topics_list = []
            topics_db = json.dumps(topics_list, ensure_ascii=False)

            # 剧集 ID 快照：入队时若该剧已绑定则带上（上传时未绑定则自动查）
            bound_id, bound_src = self._bound_id(drama_name)

            # 计划发表时间缺省：按账号当日已排队时间顺延，避免不同剧同版本撞同一时刻
            # （放在锁内计算+入库，避免两个并发入队拿到同一个空档）
            if not publish_time:
                publish_time = self._next_publish_time(_account, drama_name)

            # 插入数据库
            with _connect_db(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO upload_records 
                    (drama_name, version, video_path, cover_path, title, description, topics, account, status,
                     batch_id, source_dir, output_dir, publish_time, drama_id, drama_id_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                ''', (drama_name, version, video_path, cover_path, title, description, topics_db, _account,
                      batch_id, source_dir, output_dir, publish_time, bound_id, bound_src))
                record_id = cursor.lastrowid
            
            # 添加到内存队列
            task = {
                'id': record_id,
                'drama_name': drama_name,
                'version': version,
                'video_path': video_path,
                'cover_path': cover_path,
                'title': title,
                'description': description,
                'topics': topics_list,
                'account': _account,
                'status': 'pending',
                'retry_count': 0,
                'batch_id': batch_id,
                'source_dir': source_dir,
                'output_dir': output_dir,
                'publish_time': publish_time,
                'expected_versions': expected_versions,
                'cover_status': 'not_requested',
                'draft_saved': 0,
                'drama_id': bound_id,
                'drama_id_source': bound_src,
            }
            self.queue.append(task)
            
            self._log('info', f'添加任务：{drama_name} V{version}')
            
            return {'status': 'success', 'id': record_id}

    def add_batch(self, items: List[Dict], default_expected_versions=None,
                  publish: Dict = None) -> Dict:
        """原子批量入队（R4）：一次校验 + 一个事务创建整批上传记录。

        解决「前端逐条提交可能产生半批次」：任何一条校验失败（缺字段/文件
        不存在/重复）都不会把已通过校验的其它条留在队列里——要么整批成功，
        要么成功条目在一个事务内整体落库。返回结构化明细：
        {status, accepted, duplicates, missing, rejected, ids}。

        items: 每条至少含 {drama_name, video_path, version}；
               可选 account/cover_path/title/description/topics/batch_id/
               source_dir/output_dir/expected_versions/publish_time。
        publish: 批次级定时发表计划 {date, start_time, interval_minutes,
                 jitter_minutes}；缺省=次日 + config 首槽位 + 120 分钟间隔 + ±10。
                 显式带了 publish_time 的条目以条目为准，其余按计划顺序分配。
        """
        # 内容指纹（ffmpeg 逐视频抽帧）很慢：必须放在锁外预计算。
        # 原实现整个 add_batch 都持有 self.lock，指纹算几分钟期间
        # /api/upload/status 也被阻塞，前端轮询全部超时——页面表现为
        # 「一直没进展」。预计算期间用 batch_progress 暴露进度。
        fingerprints = {}
        total = len(items or [])
        if total:
            self.batch_progress = {'phase': 'fingerprint', 'done': 0, 'total': total}
            paths = []
            for item in items:
                path = ((item or {}).get('video_path') or '').strip()
                if path and path not in paths and os.path.isfile(path):
                    paths.append(path)
            try:
                from content_fingerprint import video_fingerprint
                import concurrent.futures as _futures
                # ffmpeg 抽帧是 I/O+CPU 混合，4 路并行能把几十条视频的
                # 校验时间压到 1/3 左右（单路一条约 20-40 秒）。
                workers = max(1, min(4, len(paths) or 1))

                def _fp(p):
                    try:
                        return p, video_fingerprint(p)
                    except Exception as e:
                        self._log('warn', f'指纹计算失败（放行）: {p} - {e}')
                        return p, None

                done = 0
                with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    for p, seq in pool.map(_fp, paths):
                        done += 1
                        if seq:
                            fingerprints[p] = seq
                        self.batch_progress = {'phase': 'fingerprint',
                                               'done': done, 'total': total}
            except Exception as e:
                self._log('warn', f'批量指纹预计算异常（回退锁内计算）: {e}')
            finally:
                self.batch_progress = None

        with self.lock:
            duplicates, missing, rejected = [], [], []
            accepted = []  # (item, drama, video_path, version, account, topics_list)

            for idx, item in enumerate(items):
                drama = (item.get('drama_name') or '').strip()
                video_path = (item.get('video_path') or '').strip()
                version = item.get('version')
                account = (item.get('account') or '福桃说剧').strip()
                label = f'{drama} V{version}' if drama else f'#{idx + 1}'

                if not drama or not video_path or version is None:
                    rejected.append({'index': idx, 'drama_name': drama,
                                     'version': version,
                                     'reason': '缺少必填字段（drama_name/video_path/version）'})
                    continue

                if not os.path.isfile(video_path):
                    missing.append({'index': idx, 'drama_name': drama,
                                    'version': version, 'video_path': video_path})
                    continue

                # 重复检查：同账号同剧同版本已在 pending/uploading 或已上传完成
                with _connect_db(self.db_path) as conn:
                    cur = conn.cursor()
                    cur.execute('''
                        SELECT id, status FROM upload_records
                        WHERE drama_name=? AND version=? AND account=?
                          AND status IN ('pending', 'uploading', 'completed')
                    ''', (drama, version, account))
                    _dup = cur.fetchone()
                    if _dup:
                        if _dup[1] == 'completed':
                            duplicates.append({'index': idx, 'drama_name': drama,
                                               'version': version, 'account': account,
                                               'reason': 'already_uploaded',
                                               'conflict': f'已上传完成（记录 #{_dup[0]}）'})
                        else:
                            duplicates.append({'index': idx, 'drama_name': drama,
                                               'version': version, 'account': account})
                        continue

                # P2-10 幂等：同一成片文件重复入队（换版本号/改名）直接拦下
                _dup_path = self._duplicate_by_path(video_path, account)
                if _dup_path:
                    duplicates.append({'index': idx, 'drama_name': drama,
                                       'version': version, 'account': account,
                                       'reason': 'duplicate_path',
                                       'conflict': (f'该成片已入队/已上传（记录 #{_dup_path[0]} '
                                                    f'{_dup_path[1]} V{_dup_path[2]} '
                                                    f'{_dup_path[3]}）')})
                    continue

                # R9 内容级查重：同账号已传/已入队内容相似 → 拒收（宁可少传不可封号）
                cdup = self._check_content_duplicate(
                    video_path, account, seq=fingerprints.get(video_path))
                if cdup:
                    up_th = CONFIG.get('content_dedup', {}).get('upload', 0.6)
                    duplicates.append({
                        'index': idx, 'drama_name': drama,
                        'version': version, 'account': account,
                        'reason': 'content_duplicate',
                        'conflict': (f'[{cdup.get("account")}] '
                                     f'{cdup["drama_name"]} V{cdup["version"]}'),
                        'similarity': cdup['similarity'],
                        'threshold': up_th,
                    })
                    self._log('warn',
                              f'内容查重拒收：{account} {drama} V{version} 与 '
                              f'{cdup.get("account")} '
                              f'{cdup["drama_name"]} V{cdup["version"]} '
                              f'相似度 {cdup["similarity"]} ≥ {up_th}')
                    continue

                # topics 归一化（与 add_to_queue 一致）
                topics = item.get('topics')
                if isinstance(topics, str):
                    topics_list = [t.strip() for t in topics.split(',') if t.strip()]
                elif isinstance(topics, list):
                    topics_list = topics
                else:
                    topics_list = []

                accepted.append((item, drama, video_path, version, account,
                                 topics_list))

            if not accepted:
                return {'status': 'success', 'accepted': 0,
                        'duplicates': duplicates, 'missing': missing,
                        'rejected': rejected, 'ids': []}

            # 计划发表：整批统一分配（同账号顺延，跨账号各排各的），
            # 显式 publish_time 的条目保留原值，其余按计划顺序补齐。
            plan_opts = self._normalize_publish_options(publish)
            sh, sm = (int(x) for x in plan_opts['start_time'].split(':'))
            eh, em = (int(x) for x in plan_opts['window_end'].split(':'))
            per_day = max(1, ((eh * 60 + em) - (sh * 60 + sm))
                          // plan_opts['interval_minutes'] + 1)
            # 跨天查询窗口：按剧分天时看「剧数」，顺序排时看「总条数/每天容量」
            if plan_opts['mode'] == 'per_drama_day':
                span_days = len({a[1] for a in accepted}) + 3
            else:
                span_days = len(accepted) // per_day + 3
            plan_taken = self._taken_publish_times(
                sorted({a[4] for a in accepted}), plan_opts['date'],
                span_days=span_days)
            plan = self.plan_publish_times(
                [{'account': a[4], 'drama': a[1]} for a in accepted],
                plan_opts, plan_taken)
            for i, (item, _drama, _vp, _ver, _acc, _topics) in enumerate(accepted):
                if not item.get('publish_time'):
                    item['publish_time'] = plan[i]

            # 一个事务插入全部通过校验的记录；任何失败整体回滚，不留半批次
            ids = []
            new_tasks = []
            with _connect_db(self.db_path) as conn:
                for (item, drama, video_path, version, account,
                     topics_list) in accepted:
                    topics_db = json.dumps(topics_list, ensure_ascii=False)
                    # 标题缺省读 edl_config，计划时间缺省按版本号自动计算
                    title = item.get('title') or self._read_edl_title(video_path, version)
                    publish_time = item.get('publish_time') or self._compute_publish_time(version)
                    # 剧集 ID 快照：入队时若该剧已绑定则带上
                    bound_id, bound_src = self._bound_id(drama)
                    cur = conn.cursor()
                    cur.execute('''
                        INSERT INTO upload_records
                        (drama_name, version, video_path, cover_path, title,
                         description, topics, account, status,
                         batch_id, source_dir, output_dir, publish_time,
                         drama_id, drama_id_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                    ''', (drama, version, video_path, item.get('cover_path'),
                          title, item.get('description'), topics_db,
                          account, item.get('batch_id'),
                          item.get('source_dir'), item.get('output_dir'),
                          publish_time, bound_id, bound_src))
                    record_id = cur.lastrowid
                    ids.append(record_id)

                    exp_versions = item.get('expected_versions',
                                            default_expected_versions)
                    new_tasks.append({
                        'id': record_id,
                        'drama_name': drama,
                        'version': version,
                        'video_path': video_path,
                        'cover_path': item.get('cover_path'),
                        'title': title,
                        'description': item.get('description'),
                        'topics': topics_list,
                        'account': account,
                        'status': 'pending',
                        'retry_count': 0,
                        'batch_id': item.get('batch_id'),
                        'source_dir': item.get('source_dir'),
                        'output_dir': item.get('output_dir'),
                        'publish_time': publish_time,
                        'expected_versions': exp_versions,
                        'cover_status': 'not_requested',
                        'draft_saved': 0,
                        'drama_id': bound_id,
                        'drama_id_source': bound_src,
                    })

            self.queue.extend(new_tasks)
            self._log('info', f'批量添加 {len(accepted)} 个上传任务')

            accepted_details = [{
                'drama_name': a[1], 'account': a[4],
                'source_dir': a[0].get('source_dir'),
                'output_dir': a[0].get('output_dir'),
                'batch_id': a[0].get('batch_id'),
                'expected_versions': a[0].get('expected_versions',
                                             default_expected_versions),
            } for a in accepted]

            return {'status': 'success', 'accepted': len(accepted),
                    'duplicates': duplicates, 'missing': missing,
                    'rejected': rejected, 'ids': ids,
                    'publish_plan': [{
                        'drama_name': a[1], 'version': a[3], 'account': a[4],
                        'publish_time': a[0].get('publish_time'),
                    } for a in accepted],
                    'accepted_details': accepted_details}
    
    def _batch_ids_in_queue(self):
        """队列里出现的批次号（去重排序）。"""
        return sorted({t.get('batch_id') for t in self.queue if t.get('batch_id')})

    def _persist_run_mode(self):
        """把本次运行的定时/清理模式写到批次行（review L3，供 resume/重启恢复）。

        `_scheduled_publish` / `_run_auto_cleanup` 是实例属性，而
        `upload_manager` 是模块级单例：resume 入口不带模式参数，恢复 worker 若只
        看内存属性就会沿用上一批次的模式。落库到 upload_batches（一批次一次运行）
        后，resume 从批次行恢复即可。返回更新的批次行数。
        """
        batch_ids = self._batch_ids_in_queue()
        if not batch_ids:
            self._log('warning', '队列无批次号，本次运行模式未落库（resume 将回退保存草稿）')
            return 0
        try:
            with _connect_db(self.db_path) as conn:
                marks = ','.join('?' for _ in batch_ids)
                cur = conn.execute(
                    f'UPDATE upload_batches SET scheduled=?, auto_cleanup=? '
                    f'WHERE batch_id IN ({marks})',
                    [1 if self._scheduled_publish else 0,
                     1 if self._run_auto_cleanup else 0] + batch_ids)
                conn.commit()
                return cur.rowcount
        except Exception as e:
            self._log('warning', f'运行模式落库失败（resume 将回退保存草稿）: {e}')
            return 0

    def _restore_run_mode(self):
        """从队列首批次行恢复定时/清理模式（review L3：恢复即延续原批次）。

        批次行缺失（旧库未落库）时保守回退「保存草稿 + 不自动清理」并留 WARN：
        误按定时发表发布未确认内容、或误触发清理删文件，代价都高于多存一份草稿。
        绝不沿用单例里上一批次的内存标志——那正是跨批次串号的根因。
        """
        batch_ids = self._batch_ids_in_queue()
        head = batch_ids[0] if batch_ids else None
        scheduled = auto_cleanup = None
        if head:
            try:
                with _connect_db(self.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        'SELECT scheduled, auto_cleanup FROM upload_batches '
                        'WHERE batch_id=?', (head,)).fetchone()
                if row is not None:
                    scheduled = bool(row['scheduled'])
                    auto_cleanup = bool(row['auto_cleanup'])
            except Exception as e:
                self._log('warning', f'读取批次运行模式失败: {e}')
        if scheduled is None:
            self._log('warning',
                      f'批次 {head} 无模式记录，恢复上传回退为保存草稿/不自动清理')
        self._scheduled_publish = bool(scheduled)
        self._run_auto_cleanup = bool(auto_cleanup)
        self._log('info',
                  f'恢复上传：批次 {head} 模式 scheduled={self._scheduled_publish} '
                  f'auto_cleanup={self._run_auto_cleanup}')
        if len(batch_ids) > 1:
            self._log('warning',
                      f'恢复会话含多批次 {batch_ids}，按首批次 {head} 的模式运行')

    def start_upload(self, auto_cleanup: bool = False, scheduled: bool = False):
        """开始上传（先预检登录，全部有效才真正开传）。

        auto_cleanup: 本次运行是否启用后端自动清理（批次自然结束后触发）。
        scheduled: True=定时发表（选定时→点发表）；False=保存草稿（默认）。
        """
        with self.lock:
            if self.uploading or self.prechecking:
                return {'status': 'failed', 'error': '正在上传或预检中'}
            # 旧 worker 仍存活（暂停/停止后未退出）时拒绝启动，避免双 worker 并发操作队列
            if self.upload_thread is not None and self.upload_thread.is_alive():
                return {'status': 'failed', 'error': '上一轮上传 worker 仍在运行，请等待其退出或重启服务'}

            if not self.queue:
                return {'status': 'failed', 'error': '队列为空'}

            self.uploading = True
            self.prechecking = True
            self.paused = False
            self._stopped = False
            self._run_auto_cleanup = bool(auto_cleanup)
            self._scheduled_publish = bool(scheduled)
            # L3：模式随批次落库，resume/重启后从批次行恢复（不依赖单例内存属性）
            self._persist_run_mode()
            self._run_epoch += 1
            self.current_account = self.queue[0].get('account')

            # 启动上传线程（线程内第一步是登录预检）
            self.upload_thread = threading.Thread(target=self._upload_worker, daemon=True)
            self.upload_thread.start()

            self._log('info', f'开始上传：{len(self.queue)}个任务（先预检登录）')

            return {'status': 'success', 'message': f'开始预检并上传{len(self.queue)}个任务'}
    
    def pause_upload(self):
        """暂停上传"""
        with self.lock:
            if not self.uploading:
                return {'status': 'failed', 'error': '未在上传'}
            
            self.paused = True
            self._log('info', '上传已暂停')
            return {'status': 'success'}
    
    def resume_upload(self):
        """恢复上传。启动 worker 前先同步检测账号登录态：
        未登录 → 返回 login_required（**不标 failed**，保持 pending 等登录后重试）；
        已登录 → 启动 worker 处理队列里已有的 pending（不重新选文件，天然不重复上传）。"""
        with self.lock:
            if self.uploading:
                self.paused = False
                self._log('info', '上传已恢复')
                return {'status': 'success'}
            # 服务重启后：queue 已从 DB 恢复但 worker 线程未启动（uploading=False）。
            # 此时 resume 应重新启动 worker 处理剩余队列，而不是报"未在上传"。
            if self.upload_thread is not None and self.upload_thread.is_alive():
                return {'status': 'failed', 'error': '上传 worker 仍在运行，请稍后再试'}
            if not self.queue:
                return {'status': 'failed', 'error': '队列为空'}
            accounts = []
            for t in self.queue:
                if t.get('account') and t.get('account') not in accounts:
                    accounts.append(t.get('account'))
        # 锁外做登录检测（开浏览器较慢，避免长持 self.lock 阻塞队列操作）
        for acct in accounts:
            u = WeChatChannelsUploader(acct)
            st = _account_login_state(u, acct)
            if st == 'busy':
                return {'status': 'failed',
                        'error': f'账号 {acct} 正在登录或浏览器被占用，暂无法校验，请稍后重试',
                        'login_required': True}
            if st != 'logged_in':
                return {'status': 'failed',
                        'error': f'账号 {acct} 未登录，请先在微信视频号登录后重试',
                        'login_required': True}
            # 记下验证时间：worker 起来后不必对每个任务重复开浏览器校验
            self._login_verified[acct] = time.time()
        # 检测通过，重新进锁启动 worker（二次校验状态未变）
        with self.lock:
            if self.uploading:
                return {'status': 'failed', 'error': '状态已变化，请重试'}
            if self.upload_thread is not None and self.upload_thread.is_alive():
                return {'status': 'failed', 'error': '上传 worker 仍在运行，请稍后再试'}
            if not self.queue:
                return {'status': 'failed', 'error': '队列为空'}
            self.uploading = True
            self.prechecking = True
            self.paused = False
            self._stopped = False
            # L3：恢复即延续原批次——模式从批次行取回，不继承上一批次的内存标志
            self._restore_run_mode()
            self._run_epoch += 1
            self.current_account = self.queue[0].get('account')
            # 登录态刚逐账号校验过（_login_verified 已写入），worker 跳过重复预检
            self.upload_thread = threading.Thread(
                target=self._upload_worker, args=(True,), daemon=True)
            self.upload_thread.start()
            self._log('info', f'恢复上传：{len(self.queue)}个任务（登录校验通过）')
            return {'status': 'success'}
    
    def stop_upload(self):
        """停止上传"""
        with self.lock:
            if not self.uploading:
                return {'status': 'failed', 'error': '未在上传'}
            
            self.paused = True
            self.uploading = False  # 重置uploading状态
            self._stopped = True   # 标记人工停止：自然结束路径不得触发清理
            self._stop_epoch = self._run_epoch  # 仅标记本次运行的停止
            self.current_task = None  # 清除当前任务
            self._log('info', '上传已停止')
            return {'status': 'success'}
    
    def clear_queue(self, status: str = 'pending'):
        """清除队列"""
        with self.lock:
            if status == 'all':
                cleared = len(self.queue) + len(self.failed) + len(self.completed)
                self.queue.clear()
                self.completed.clear()
                self.failed.clear()
                
                # 清除数据库记录
                conn = _connect_db(self.db_path)
                cursor = conn.cursor()
                cursor.execute('DELETE FROM upload_records')
                # P2-7: 'all' 也清空批次/清理任务/期望表，防孤儿行残留
                # （upload_batches / artifact_cleanup_jobs / upload_batch_expected）
                for tbl in ('upload_batches', 'artifact_cleanup_jobs', 'upload_batch_expected'):
                    try:
                        cursor.execute(f'DELETE FROM {tbl}')
                    except sqlite3.OperationalError:
                        pass  # 表可能不存在（旧库），跳过
                conn.commit()
                conn.close()
            else:
                # 从对应的内存列表中清除
                if status == 'failed':
                    cleared = len(self.failed)
                    self.failed.clear()
                elif status == 'completed':
                    cleared = len(self.completed)
                    self.completed.clear()
                else:
                    # pending / uploading 等状态从队列清除
                    cleared = len([t for t in self.queue if t.get('status') == status])
                    self.queue = [t for t in self.queue if t.get('status') != status]
                
                # 更新数据库
                conn = _connect_db(self.db_path)
                cursor = conn.cursor()
                cursor.execute('DELETE FROM upload_records WHERE status=?', (status,))
                conn.commit()
                conn.close()
            
            self._log('info', f'清除{status}任务：{cleared}个')
            return {'status': 'success', 'cleared': cleared}
    
    def get_status(self) -> Dict:
        """获取上传状态。

        锁被长任务占用（批量指纹计算/正在上传）时不等锁，直接返回无锁快照：
        /api/upload/status 是前端 5 秒一次的轮询接口，等锁会把页面拖成假死。
        无锁读取的字段都是简单赋值/列表切片，GIL 下不会读到中间态。
        """
        return {
            'uploading': self.uploading,
            'prechecking': self.prechecking,
            'paused': self.paused,
            'queue_length': len(self.queue),
            'completed_count': len(self.completed),
            'failed_count': len(self.failed),
            'current_task': self.current_task,
            'current_account': self.current_account,
            'queue': self.queue[:],
            'completed': self.completed[-20:],  # 最近20个
            'failed': self.failed[-20:],  # 最近20个
            'logs': self.logs[-50:],  # 最近50条日志
            'batch_progress': self.batch_progress,
        }
    
    def update_task_title(self, record_id: int, title: str) -> Dict:
        """手动覆盖任务短标题（dashboard 上传前编辑）。

        同步内存队列（queue/completed/failed 中的任务对象）与 upload_records，
        使覆盖值在 _execute_upload 生效并入库留痕。仅对 pending 任务允许覆盖；
        已在上传/已完成的任务不修改（上传值已在浏览器页面中，改内存无意义）。
        """
        title = (title or '').strip()
        if not title:
            return {'status': 'failed', 'error': '标题不能为空'}
        with self.lock:
            target = None
            for t in list(self.queue) + list(self.completed) + list(self.failed):
                if t.get('id') == record_id:
                    target = t
                    break
            if target is None:
                return {'status': 'failed', 'error': f'任务 {record_id} 不存在'}
            if target.get('status') not in ('pending',):
                return {'status': 'failed',
                        'error': f'任务 {record_id} 状态为 {target.get("status")}，仅待上传可改标题'}
            target['title'] = title
        try:
            with _connect_db(self.db_path) as conn:
                conn.execute(
                    "UPDATE upload_records SET title=? WHERE id=?",
                    (title, record_id))
                conn.commit()
        except Exception as e:
            return {'status': 'failed', 'error': f'更新数据库失败: {e}'}
        self._log('info', f'任务 {record_id} 标题已改为：{title}')
        return {'status': 'success', 'id': record_id, 'title': title}
    
    def get_history(self, limit: int = 100) -> List[Dict]:
        """获取上传历史"""
        conn = _connect_db(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM upload_records 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (limit,))
        records = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return records

    def list_completed_records(self, account: str = None) -> List[Dict]:
        """返回已上传（status='completed'）的记录，供可上传列表标状态/一键删除用。

        只返回真正上传成功的记录（不返回 pending/uploading/failed）。
        可指定账号过滤；返回字段：id, drama_name, version, account, video_path, completed_at。
        """
        conn = _connect_db(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if account:
            cursor.execute('''
                SELECT id, drama_name, version, account, video_path, completed_at
                FROM upload_records
                WHERE status='completed' AND account=?
                ORDER BY drama_name, version
            ''', (account,))
        else:
            cursor.execute('''
                SELECT id, drama_name, version, account, video_path, completed_at
                FROM upload_records
                WHERE status='completed'
                ORDER BY drama_name, version
            ''')
        records = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return records
    
    def _upload_side_effect_done(self, task: Dict) -> bool:
        """本轮是否已对平台产生不可回滚的副作用（决定能否安全重试）。

        draft_saved=草稿已写入草稿箱；published=已发表；pending_confirm=定时发表
        未确认（作品可能已在已发布列表里）。任一为真时重传=重复上传/重复发表。
        """
        return bool(task.get('draft_saved') or task.get('published')
                    or task.get('pending_confirm'))

    def _retry_block_reason(self, task: Dict, timed_out: bool) -> str:
        """返回「不可自动重试」的原因；空串表示可以重排队。"""
        if timed_out:
            return f'单任务执行超时（>{self.task_timeout}s），副作用未确认，转人工'
        if self._upload_side_effect_done(task):
            return '已产生草稿/发表副作用，未自动重试以免重复上传'
        err = task.get('error_message') or ''
        if any(k in err for k in ('登录状态已失效', '登录失效', '未登录')):
            # 登录掉线重试只是再开两次浏览器、再失败两次：必须人工重新登录
            return '登录态失效，需重新登录后再传'
        if '基线' in err:
            # 草稿箱基线拿不到 → 无法确认「本次新增」；重传只会多一条重复草稿
            return '草稿箱基线缺失，无法确认是否产生新增草稿，不自动重试，请人工检查草稿箱'
        if not self._should_retry(task):
            return f'重试已达上限（{self.max_retries} 次）'
        return ''

    def _precheck_or_abort(self) -> bool:
        """预检待传账号登录态；返回 False 表示本轮不应继续（状态已复位）。

        不预检的代价：队列里每个任务都要单独开一次 headless 浏览器校验
        （每次 10-20s），登录过期时整条队列白跑、全部标失败。
        """
        self._log('info', '预检登录状态...')
        try:
            pre = self._precheck_logins()
        except Exception as e:
            # 预检抛异常不能击穿 worker：否则 uploading 永久 True，后续 start_upload
            # 一律被“正在上传或预检中”拒绝，只能重启服务。此处复位后直接退出。
            with self.lock:
                self.uploading = False
                self.prechecking = False
                self.current_task = None
            self._log('error', f'预检登录异常，本轮未启动上传：{e}')
            return False

        if pre == 'blocked':
            # 账号浏览器被登录/上传占用：无法判定登录态。不标失败、不结算批次，
            # 队列原样保留 pending，用户处理完占用后可直接再点开始。
            with self.lock:
                self.uploading = False
                self.prechecking = False
                self.current_task = None
                pending = len([t for t in self.queue if t.get('status') == 'pending'])
            self._log('warning', f'预检中止（账号占用），{pending} 个任务保持待传')
            return False

        if not pre:
            # P2-9: 预检失败=登录态不可用，队列任务标 failed（防永久 pending），
            # 并 finalize 已记录批次（防批次行永久 running）。auto_cleanup=False：
            # 登录失败≠上传完成，触发清理会误删未上传的成片。
            with self.lock:
                self.uploading = False
                self.prechecking = False
                self.current_task = None
                failed_tasks = list(self.queue)
                self.queue.clear()
                for t in failed_tasks:
                    t['status'] = 'failed'
                    t['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    t['error_message'] = t.get('error_message') or '登录态预检失败'
                self.failed.extend(failed_tasks)
                failed_batches = {t.get('batch_id') for t in failed_tasks if t.get('batch_id')}
            for t in failed_tasks:
                try:
                    with _connect_db(self.db_path) as conn:
                        conn.execute(
                            "UPDATE upload_records SET status='failed', completed_at=?, error_message=? WHERE id=?",
                            (t['completed_at'], t['error_message'], t['id']))
                        conn.commit()
                except Exception as e:
                    self._log('warning', f'预检失败更新记录失败: {e}')
            try:
                self._finalize_batches(failed_batches, auto_cleanup=False)
            except Exception as e:
                self._log('error', f'预检失败finalize批次失败: {e}')
            self._log('warning', f'预检失败，{len(failed_tasks)} 个任务标失败')
            return False
        return True

    def _upload_worker(self, skip_precheck: bool = False):
        """上传工作线程 - 缩小锁范围，避免死锁

        skip_precheck=True 只用于 resume 路径：登录态刚由 resume_upload 同步校验
        并写入 _login_verified，再跑一遍预检等于每个账号白开一次浏览器。
        """
        if skip_precheck:
            self._log('info', '恢复上传：登录态已在校验阶段确认，跳过重复预检')
        elif not self._precheck_or_abort():
            return

        seen_batches = set()
        my_epoch = self._run_epoch
        my_auto_cleanup = self._run_auto_cleanup
        try:
            while True:
                # 获取任务（短暂持锁）
                with self.lock:
                    if not self.queue:
                        break
                
                    if self.paused:
                        # 人工停止：退出循环让 worker 结束，释放线程供下次 start
                        if self._stopped:
                            break
                        time.sleep(1)
                        continue
                
                    task = self.queue.pop(0)
                    self.current_task = task
                    task['status'] = 'uploading'
                    task['started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if task.get('batch_id'):
                        seen_batches.add(task['batch_id'])
            
                # 更新数据库（不持锁）
                with _connect_db(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE upload_records 
                        SET status='uploading', started_at=?
                        WHERE id=?
                    ''', (task['started_at'], task['id']))
            
                self._log('info', f'开始上传：{task["drama_name"]} V{task["version"]}')
            
                # 执行上传（不持锁，避免阻塞其他线程）
                started_at = task.get('started_at')
                success = self._execute_upload(task)
                timed_out = bool(started_at) and \
                    self._elapsed_seconds(started_at) > self.task_timeout
                requeued = False
            
                # 更新任务状态（短暂持锁）
                with self.lock:
                    if success:
                        task['status'] = 'completed'
                        task['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        self.completed.append(task)
                        self._log('success', f'上传成功：{task["drama_name"]} V{task["version"]}')
                        # R9：上传成功后登记内容指纹 uploaded=1（跨批/跨剧查重依据）
                        self._mark_fingerprint_uploaded(task.get('video_path'))
                        # 剧级档案联动：确保剧级档案行存在（手动上传的剧可能从未入档）。
                        # upload_state 由 archive.aggregate_upload_state 实时聚合（无 upload_state 列）。
                        self._sync_drama_archive(task, 'done')
                    else:
                        # 有限重试（base_queue 上限 2 次）：只有「确定没有产生平台副作用」
                        # 才重排队。draft_saved / published / pending_confirm 任一为真，
                        # 说明草稿或作品可能已经在视频号里了，重传=重复上传，转人工。
                        blocked_by = self._retry_block_reason(task, timed_out)
                        if not blocked_by:
                            self._record_retry(task)
                            task['status'] = 'pending'
                            task['last_error'] = task.get('error_message')
                            task['error_message'] = ''
                            task['completed_at'] = None
                            self.queue.append(task)
                            requeued = True
                            self._log('warning',
                                      f'上传失败，重排队第 {task["retry_count"]}/'
                                      f'{self.max_retries} 次：{task["drama_name"]} '
                                      f'V{task["version"]} - {task["last_error"]}')
                        else:
                            task['status'] = 'failed'
                            task['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            task['error_message'] = (
                                f'{task.get("error_message") or "上传失败"}｜{blocked_by}')
                            self.failed.append(task)
                            self._log('error', f'上传失败：{task["drama_name"]} '
                                               f'V{task["version"]} - {task.get("error_message")}')
                            self._sync_drama_archive(task, 'failed')
            
                if requeued:
                    # 重排队：状态回 pending、清 completed_at，错误留痕到 error_message
                    with _connect_db(self.db_path) as conn:
                        conn.execute(
                            "UPDATE upload_records SET status='pending', "
                            "completed_at=NULL, error_message=? WHERE id=?",
                            (task.get('last_error'), task['id']))
                        conn.commit()
                    time.sleep(2)  # 重试前同样留风控间隔
                    continue

                # 更新数据库（不持锁）—— 同步实际使用的文案与封面状态，留审计痕迹
                _topics_db = json.dumps(task.get('topics', []), ensure_ascii=False) if task.get('topics') else None
                _cover_status = task.get('cover_status', 'not_requested')
                _draft_saved = 1 if task.get('draft_saved') else 0
                _published = 1 if task.get('published') else 0
                _draft_verified_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if _draft_saved else None
                with _connect_db(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE upload_records 
                        SET status=?, completed_at=?, error_message=?,
                            title=?, description=?, topics=?, cover_path=?,
                            cover_status=?, draft_saved=?, draft_verified_at=?,
                            source_dir=?, output_dir=?, publish_time=?, published=?
                        WHERE id=?
                    ''', (task['status'], task['completed_at'], task.get('error_message'),
                          task.get('title'), task.get('description'), _topics_db,
                          task.get('cover_path'), _cover_status, _draft_saved,
                          _draft_verified_at, task.get('source_dir'), task.get('output_dir'),
                          task.get('publish_time'), _published, task['id']))
            
                # 检查账号切换（短暂持锁）
                with self.lock:
                    if self.queue:
                        next_account = self.queue[0].get('account')
                        if next_account != self.current_account:
                            self.current_account = next_account
                            self._log('info', f'切换账号：{self.current_account}')
            
                # 等待间隔（避免风控）
                time.sleep(2)
        finally:
            with self.lock:
                self.uploading = False
                self.prechecking = False
                self.current_task = None
            self._log('info', '上传线程结束（含异常路径复位）')
            # 自然结束：标记批次完成并触发后端自动清理。
            # 异常路径也尝试 finalize（安全：会校验无 pending/uploading 才标记完成）。
            if self._stop_epoch != my_epoch:
                try:
                    self._finalize_batches(seen_batches, auto_cleanup=my_auto_cleanup)
                except Exception as e:
                    self._log('error', f'worker结束finalize批次失败: {e}')
    def _finalize_batches(self, batch_ids, auto_cleanup=False):
        """批次自然结束：标记 upload_batches 完成；启用自动清理则触发回调。

        每个批次只有当其所有记录都不再是 pending/uploading 时才标记完成，
        避免中途有任务被搁置时误标完成。auto_cleanup 为本次运行的快照值。
        """
        for batch_id in batch_ids:
            try:
                with _connect_db(self.db_path) as conn:
                    pending = conn.execute(
                        "SELECT COUNT(*) FROM upload_records "
                        "WHERE batch_id=? AND status IN ('pending','uploading')",
                        (batch_id,)).fetchone()[0]
                    do_cleanup = bool(auto_cleanup)
                    if pending == 0 and do_cleanup:
                        # 自动清理硬门槛：批次内必须全部 completed。出现 failed /
                        # pending_confirm 说明有内容没确认落地，清理会误删未上传的成片。
                        unfinished = conn.execute(
                            "SELECT COUNT(*) FROM upload_records "
                            "WHERE batch_id=? AND status <> 'completed'",
                            (batch_id,)).fetchone()[0]
                        if unfinished:
                            do_cleanup = False
                            self._log(
                                'warning',
                                f'批次 {batch_id} 有 {unfinished} 条非 completed，'
                                f'跳过自动清理（防误删未上传成片）')
                    if pending == 0:
                        conn.execute(
                            "UPDATE upload_batches SET status='completed', "
                            "auto_cleanup=?, completed_at=? WHERE batch_id=?",
                            (1 if do_cleanup else 0,
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'), batch_id))
                        conn.commit()
                        self._log('info', f'批次 {batch_id} 已标记完成')
                        if do_cleanup and self._cleanup_callback:
                            try:
                                self._cleanup_callback(batch_id)
                                self._log('info', f'批次 {batch_id} 触发自动清理')
                            except Exception as e:
                                self._log('error', f'批次 {batch_id} 自动清理失败: {e}')
            except Exception as e:
                self._log('error', f'批次 {batch_id} 完成标记失败: {e}')
    
    def _precheck_logins(self):
        """上传前校验队列里每个账号的登录态。

        返回三种结果：
          True      —— 预检放行（至少一个账号登录有效；确认未登录账号的任务已标失败）；
          False     —— 全部待传账号都确认未登录（任务已标失败，不启动上传）；
          'blocked' —— 有账号 profile 正被登录/上传占用（busy），本轮**无法判定**：
                       任务一律不标失败、队列保持 pending，等占用结束后重试。
        """
        self.prechecking = True
        try:
            with self.lock:
                accounts = []
                for t in self.queue:
                    acct = t.get('account')
                    if acct and acct not in accounts:
                        accounts.append(acct)

            # 上传要独占账号 profile：先收掉该账号开着的草稿箱窗口，
            # 否则登录校验会撞同一个 user_data_dir（附着/抢占用户正在用的窗口）。
            for acct in accounts:
                if acct in self.draft_pages:
                    self.close_draft_box(acct)
                    self._log('info', f'{acct} 草稿箱已关闭（上传需独占账号浏览器）')

            bad, ok, busy = [], [], []
            for acct in accounts:
                u = WeChatChannelsUploader(acct)
                st = _account_login_state(u, acct)
                if st == 'logged_in':
                    ok.append(acct)
                    self._login_verified[acct] = time.time()
                    self._log('info', f'登录校验通过：{acct}')
                elif st == 'busy':
                    busy.append(acct)
                    self._log('warning', f'{acct} 正在登录/浏览器被占用，暂无法校验')
                else:
                    bad.append(acct)
                    self._log('error', f'登录校验失败：{acct}')

            if busy:
                # busy 是“测不了”，不是“掉线”：整批保持 pending，不标失败、不结算批次
                self._log('error',
                          f'预检中止：账号 {", ".join(busy)} 正在登录/被占用，'
                          f'队列保持待传，请稍后重试')
                return 'blocked'

            if not bad:
                self._log('info', f'预检通过（{len(accounts)}个账号）')
                return True

            self._log('error', f'预检未通过，未登录账号：{", ".join(bad)}')
            failed_now = []
            with self.lock:
                # 注意：边遍历边 remove 会跳元素，必须遍历副本。
                for t in list(self.queue):
                    if t.get('account') in bad:
                        t['status'] = 'failed'
                        t['error_message'] = f'账号 {t.get("account")} 未登录，请先完成登录再上传'
                        t['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        self.failed.append(t)
                        failed_now.append(t)
                        self.queue.remove(t)

            for t in failed_now:
                try:
                    with _connect_db(self.db_path) as conn:
                        conn.execute(
                            "UPDATE upload_records SET status='failed', completed_at=?, "
                            "error_message=? WHERE id=?",
                            (t['completed_at'], t['error_message'], t['id']))
                except Exception as e:
                    self._log('error', f'写失败记录出错：{e}')

            # 掉线账号的批次单独结算（登录失败≠上传完成，不触发自动清理）。
            failed_batches = {t.get('batch_id') for t in failed_now if t.get('batch_id')}
            if failed_batches:
                try:
                    self._finalize_batches(failed_batches, auto_cleanup=False)
                except Exception as e:
                    self._log('error', f'预检失败批次结算失败：{e}')

            # 仍有登录正常的账号 → 继续上传其任务（不再整批返回 False）。
            with self.lock:
                remaining = [t for t in self.queue if t.get('account') not in bad]
            if remaining:
                self._log('warning',
                          f'部分账号未登录（{", ".join(bad)}），已跳过其任务；'
                          f'继续上传 {len(remaining)} 个任务')
                return True
            self._log('error', '全部待传账号均未登录，上传终止')
            return False
        finally:
            self.prechecking = False

    def _login_recently_verified(self, account, ttl: int = 600) -> bool:
        """该账号是否在 ttl 秒内刚通过登录校验（批次预检/上次上传成功）。

        一次登录校验要起一个完整 Chrome（10-20s）。同批次内逐任务重复校验
        没有收益：真掉线由 upload_to_draft 的发布页 URL 检测兜住。
        """
        ts = self._login_verified.get(account or '', 0)
        return bool(ts) and (time.time() - ts) < ttl

    @staticmethod
    def _page_is_alive(page) -> bool:
        """DrissionPage 页面存活探测：CDP Page.getLayoutMetrics 成功即存活。

        用户手动关掉 Chrome 窗口（点 X）后，draft_pages 里残留的 page 对象
        并不自动移除——它仍在字典里，但底层连接已断。用 states.is_alive
        （捕获 PageDisconnectedError 返回 False）识别死页面。
        """
        if page is None:
            return False
        try:
            return bool(page.states.is_alive)
        except Exception:
            return False

    def _open_draft_box_browser(self, account: str) -> 'ChromiumPage':
        """新建草稿箱浏览器（headed，复用上传登录态）。

        手动关窗后旧 Chrome 进程可能还在释放 profile 锁，立即重开会撞锁；
        短重试几次。返回的页面对象须由调用方管理。
        """
        from wechat_uploader import WeChatChannelsUploader

        uploader = WeChatChannelsUploader(account)
        user_data_path = uploader.user_data_path
        os.makedirs(user_data_path, exist_ok=True)

        co = ChromiumOptions()
        co.set_paths(user_data_path=user_data_path)
        co.set_argument('--disable-blink-features=AutomationControlled')

        last_err = None
        # DrissionPage 构造期存在 _dl_mgr 竞态（冷启动连接完成得比 __init__
        # 赋 _dl_mgr 更快，访问到未赋值属性）；失败尝试留下的浏览器进程会被
        # 后续 ChromiumPage(co) 复用（同一 debug 端口自动接管），一旦浏览器
        # 完全就绪即成功。给足冷启动窗口：5 次 × 3s。
        for attempt in range(5):
            page = None
            try:
                page = ChromiumPage(co)
                page.get('https://channels.weixin.qq.com/platform/post/draftListManager')
                return page
            except Exception as e:
                last_err = e
                # P2-8: 失败尝试若已建页面/进程，主动 quit，防 Chrome 进程遗留
                if page is not None:
                    try:
                        page.quit()
                    except Exception:
                        pass
                self._log('warning',
                          f'{account} 打开草稿箱第 {attempt + 1} 次失败：{e}，'
                          f'等待浏览器就绪/释放 profile 锁...')
                time.sleep(3)
        raise last_err if last_err else RuntimeError('打开草稿箱失败')

    def open_draft_box(self, account: str) -> Dict:
        """打开草稿箱浏览器窗口（复用上传时的登录态）。

        支持多次打开：若旧页面已死（用户手动关窗/崩溃），自动清理并重开，
        而不是残留死页面导致后续点击返回「已打开」但实际无窗口。
        """
        if account in self.draft_pages:
            page = self.draft_pages[account]
            if self._page_is_alive(page):
                return {'status': 'success', 'message': f'{account} 草稿箱已打开'}
            # 旧页面已死，清理后重开
            self.draft_pages.pop(account, None)
            self._log('warning', f'{account} 草稿箱旧页面已失效，重新打开')

        # 检查是否正在上传（profile 锁互斥）
        if self.uploading and self.current_account == account:
            return {'status': 'failed', 'error': f'{account} 正在上传，请等上传完成后再打开草稿箱'}

        try:
            page = self._open_draft_box_browser(account)
            self.draft_pages[account] = page
            self._log('info', f'{account} 草稿箱已打开')
            return {'status': 'success', 'message': f'{account} 草稿箱已打开，请在浏览器中编辑发布'}
        except Exception as e:
            self._log('error', f'打开草稿箱失败：{e}')
            return {'status': 'failed', 'error': str(e)}

    def close_draft_box(self, account: str) -> Dict:
        """关闭草稿箱浏览器窗口"""
        if account not in self.draft_pages:
            return {'status': 'failed', 'error': f'{account} 草稿箱未打开'}

        try:
            page = self.draft_pages.pop(account)
            try:
                page.quit()
            except Exception as e:
                self._log('warning', f'{account} 关闭草稿箱时页面已失效：{e}')
            self._log('info', f'{account} 草稿箱已关闭')
            return {'status': 'success', 'message': f'{account} 草稿箱已关闭'}
        except Exception as e:
            self.draft_pages.pop(account, None)
            return {'status': 'failed', 'error': str(e)}

    def close_all_draft_boxes(self) -> int:
        """批量关闭所有草稿箱浏览器（应用退出/重启前调用，防 Chrome 进程遗留）。"""
        closed = 0
        for account, page in list(self.draft_pages.items()):
            try:
                page.quit()
                closed += 1
            except Exception as e:
                self._log('warning', f'{account} 关闭草稿箱失败（可忽略）: {e}')
            finally:
                self.draft_pages.pop(account, None)
        if closed:
            self._log('info', f'已关闭 {closed} 个草稿箱浏览器')
        return closed
    
    def get_draft_status(self) -> Dict:
        """获取草稿箱打开状态（过滤已失效页面，反映真实窗口存在性）。"""
        alive = []
        for acct, page in list(self.draft_pages.items()):
            if self._page_is_alive(page):
                alive.append(acct)
            else:
                # 手动关窗/崩溃后残留的死页面：清理，让前端按钮回到「打开草稿箱」
                self.draft_pages.pop(acct, None)
                self._log('warning', f'{acct} 草稿箱页面已失效，从状态中移除')
        return {'open_accounts': alive}
    
    def _execute_upload(self, task: Dict) -> bool:
        """执行单个上传任务 - 从 edl_config_v{version}.json 读取 AI 生成的文案"""
        try:
            # 检查视频文件
            if not os.path.exists(task['video_path']):
                task['error_message'] = f'视频文件不存在：{task["video_path"]}'
                return False
            
            # ── 从 edl_config_v{version}.json 读取文案（切片时 AI 生成，存同目录）──
            video_dir = os.path.dirname(task['video_path'])
            config_path = os.path.join(video_dir, f"edl_config_v{task['version']}.json")
            
            title = task.get('title')
            description = task.get('description')
            topics = task.get('topics')
            cover_path = task.get('cover_path')
            
            # ── 优先从 edl_config 的 artifacts 读取明确路径（不再靠猜）──
            config = None
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                    video_info = config.get('videos', [{}])[0]
                    title = title or video_info.get('title')
                    description = description or video_info.get('description')
                    topics = topics or video_info.get('hashtags', [])
                    # 同步更新 task（供调用方和日志使用）
                    task['title'] = title
                    task['description'] = description
                    task['topics'] = topics
                    self._log('info', f'V{task["version"]} 文案：{title}')
                except Exception as e:
                    self._log('warn', f'V{task["version"]} 读取edl_config失败：{e}')

            # 标题仍未确定（无任务覆盖 + 无 edl_config）→ 用剧名+版本生成兜底标题，
            # 不再退化为笼统的「短剧精彩片段」。
            if not title:
                title = self._fallback_title(task.get('drama_name'), task.get('version'))
                task['title'] = title
                self._log('info', f'V{task["version"]} 使用兜底标题：{title}')

            # 从 artifacts/根级补齐 source_dir、output_dir（供清理计划器聚合）
            if isinstance(config, dict):
                artifacts = config.get('artifacts') or {}
                if not task.get('source_dir'):
                    task['source_dir'] = artifacts.get('source_dir') or config.get('source_dir')
                if not task.get('output_dir'):
                    task['output_dir'] = artifacts.get('output_dir') or config.get('output_dir') or video_dir
                if task.get('source_dir'):
                    self._log('info', f'V{task["version"]} 素材目录：{task["source_dir"]}')
                if task.get('output_dir'):
                    self._log('info', f'V{task["version"]} 成品目录：{task["output_dir"]}')

            # ── 推断封面路径：先看 artifacts，再回退到 covers/{剧名}_{版本}_封面.jpg ──
            if not cover_path and isinstance(config, dict):
                artifacts = config.get('artifacts') or {}
                if artifacts.get('cover_path') and os.path.exists(artifacts['cover_path']):
                    cover_path = artifacts['cover_path']
                    task['cover_path'] = cover_path
                    self._log('info', f'V{task["version"]} 从 artifacts 读取封面：{cover_path}')

            if not cover_path:
                # 回退：_generate_cover 用原始剧名写 covers/{剧名}_{版本}_封面.jpg。
                # 上传管理器此前用 [^\w\u4e00-\u9fff]→_ 的宽清洗，含逗号/全角标点的
                # 剧名会猜错。现在按实际写入规则生成候选，逐一探测。
                import re as _re
                candidates = []
                raw = task['drama_name']
                candidates.append(raw)
                candidates.append(raw.replace('，', '_').replace(',', '_').replace(' ', '_'))
                candidates.append(_re.sub(r'[^\w\u4e00-\u9fff]', '_', raw))
                for cand in candidates:
                    guess = os.path.join(video_dir, 'covers', f'{cand}_{task["version"]}_封面.jpg')
                    if os.path.exists(guess):
                        cover_path = guess
                        task['cover_path'] = guess
                        self._log('info', f'V{task["version"]} 封面（回退探测）：{guess}')
                        break
                if not cover_path:
                    cover_path = None  # 不存在就不传，让上传器用默认封面

            
            # 初始化上传器
            account = task.get('account')
            uploader = WeChatChannelsUploader(account)
            # 上传进度日志转发到 status.logs（前端 uploadLog 可见）。
            # 必须在 upload_to_draft 之前设置，否则浏览器启动/
            # 视频上传等进度只走 stderr，用户看不到上传进展。
            uploader.set_log_hook(self._log)

            # 登录态由批次预检负责（_precheck_logins 已把未登录账号的任务结算掉），
            # 这里不再为每个任务单开一次浏览器做 check_login：那是同一批次里的
            # 第二次全量校验，纯耗时且会和 profile 锁互相等待。
            # upload_to_draft 内部仍是权威校验（发布页 URL + 登录页跳转检测）。
            skip_login_check = self._login_recently_verified(account)

            # 剧集 ID（保存草稿前填「链接 → 视频号剧集」用，A3 混合语义）：
            # 任务有绑定 ID 用之；未绑定则自动查上游首条；查不到/失败则不填，
            # 照常保存草稿（不阻断发布）。结果写回 task 供日志与前端展示。
            resolved = self._resolve_task_drama_id(task)
            drama_id = resolved['drama_id']
            drama_src = resolved['source']
            task['drama_id'] = drama_id
            task['drama_id_source'] = drama_src
            if drama_id:
                matched = resolved.get('matched_name')
                extra = f'（匹配 {matched}）' if matched and matched != task['drama_name'] else ''
                self._log('info',
                          f'V{task["version"]} 剧集链接将用 [{drama_src}] ID：'
                          f'{drama_id}{extra}（剧名 {task["drama_name"]}）')
            else:
                detail = f'：{resolved["error"]}' if resolved.get('error') else ''
                self._log('warn',
                          f'V{task["version"]} 未取得剧集 ID（{drama_src}）{detail}，'
                          f'跳过填链接，改为保存草稿（不发表/不定时）')

            # 执行上传（传递位置参数）
            # upload_to_draft 现在返回结构化 dict，不再返回 bool：
            #   {'status': 'success'|'failed', 'draft_saved': bool,
            #    'cover': {'status': ..., 'error': ..., 'cover_path': ...},
            #    'error': str|None}
            # 查不到剧集 ID 时不发表/不定时，直接保存草稿（用户要求）：无法填
            # 「链接 → 视频号剧集」就不发布，避免发出后挂不上剧集链接。
            scheduled = bool(getattr(self, '_scheduled_publish', False))
            if not drama_id:
                scheduled = False
            outcome = uploader.upload_to_draft(
                video_path=task['video_path'],
                title=title or '短剧精彩片段',
                tags=topics or [],
                description=description or '',
                cover_path=cover_path,
                drama_name=task['drama_name'],
                drama_id=drama_id,
                publish_time=task.get('publish_time'),
                scheduled=scheduled,
                skip_login_check=skip_login_check,
            )

            if isinstance(outcome, dict):
                cover_info = outcome.get('cover') or {}
                task['cover_status'] = cover_info.get('status', 'not_requested')
                task['cover_path'] = cover_info.get('cover_path') or task.get('cover_path')
                task['draft_saved'] = bool(outcome.get('draft_saved'))
                task['published'] = bool(outcome.get('published'))
                if outcome.get('status') == 'success':
                    self._login_verified[account] = time.time()
                    return True
                elif outcome.get('status') == 'pending_confirm':
                    # 定时发表未确认：不假报失败（作品可能已发表），交人工复核。
                    # 标 failed 但错误信息明确指向已发布列表人工确认，防误重传重复发表。
                    task['error_message'] = (
                        outcome.get('error') or outcome.get('publish_note')
                        or '定时发表未确认，请到已发布列表人工复核')
                    task['pending_confirm'] = True
                    return False
                else:
                    task['error_message'] = outcome.get('error') or '上传失败'
                    if '登录' in (task['error_message'] or ''):
                        # 登录失效/账号被占用：作废本账号的“已验证”缓存，
                        # 后续任务会重新完整校验（而不是继续盲传）
                        self._login_verified.pop(account, None)
                    return False
            else:
                # 兼容旧返回 bool（不应再出现）
                task['cover_status'] = 'not_requested'
                if outcome:
                    return True
                else:
                    task['error_message'] = '上传失败（upload_to_draft 返回 False）'
                    return False
                
        except Exception as e:
            task['error_message'] = str(e)
            return False
    
    def _log(self, level: str, message: str):
        """记录日志：统一结构化条目，上下文取自当前任务；控制台走标准库 logger。"""
        ctx = context_from_task(self.current_task)
        log_entry = make_entry(level, message, **ctx)
        self.logs.append(log_entry)
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]
        py_level = getattr(logging, log_entry['level'], logging.INFO)
        get_logger('upload').log(py_level, message)
