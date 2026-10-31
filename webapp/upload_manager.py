# -*- coding: utf-8 -*-
"""
上传队列管理器
功能：管理批量上传任务、异步处理、状态查询、日志记录
"""
import os
import json
import threading
import time
from datetime import datetime
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
from processor import CONFIG

# DrissionPage 已随 wechat_uploader 加载；此处模块级导入便于 mock 与复用
from DrissionPage import ChromiumPage, ChromiumOptions

class UploadManager:
    """上传队列管理器"""
    
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or CONFIG.get('data_dir', 'data')
        self.upload_log_file = os.path.join(self.data_dir, 'upload_log.json')
        
        # 数据库路径
        self.db_path = os.path.join(self.data_dir, 'upload.db')
        self._init_database()
        
        # 队列状态
        self.queue = []  # 待上传队列
        self.current_task = None  # 当前上传任务
        self.uploading = False  # 是否正在上传
        self.prechecking = False  # 是否正在预检登录
        self.paused = False  # 是否暂停
        self.completed = []  # 已完成任务
        self.failed = []  # 失败任务
        
        # 日志
        self.logs = []
        
        # 线程锁
        self.lock = threading.Lock()
        
        # 上传线程
        self.upload_thread = None
        
        # 账号列表
        self.accounts = []
        self.current_account = None
        
        # 草稿箱浏览器实例（account → ChromiumPage）
        self.draft_pages = {}
        
        # 后端触发的自动清理：本次运行是否启用、本批次是否人工停止、
        # 以及批次完成回调（由 app.py 注入 artifact_cleanup.execute_cleanup）
        # _run_epoch: 每次 start_upload 自增，标识一次运行；_stopped/_stop_epoch
        # 标记该次运行被人工停止。旧 worker 结束时用自己运行的 epoch 判定，
        # 不会读到新运行的标志（并发窗口 P1 修复）。
        self._run_auto_cleanup = False
        self._stopped = False
        self._cleanup_callback = None
        self._run_epoch = 0
        self._stop_epoch = -1

        # 服务重启后把未完成记录恢复到内存队列；否则数据库有任务但界面为空，
        # start_upload 会长期报“队列为空”。正在上传的记录统一降回 pending。
        self._restore_unfinished_queue()

    def set_cleanup_callback(self, cb):
        """注入批次完成回调（batch_id → None）。app.py 绑定 artifact_cleanup。"""
        self._cleanup_callback = cb

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
                    'cover_status': row['cover_status'] or 'not_requested',
                    'draft_saved': bool(row['draft_saved']),
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
                cleanup_status TEXT,              -- 清理整体状态：success/partial/failed
                cleanup_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')
        
        # 迁移：为既有批次表补充 auto_cleanup / cleanup_status 列（幂等）
        batch_cols = {row[1] for row in cursor.execute('PRAGMA table_info(upload_batches)').fetchall()}
        if 'auto_cleanup' not in batch_cols:
            try:
                cursor.execute('ALTER TABLE upload_batches ADD COLUMN auto_cleanup INTEGER DEFAULT 0')
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

        
    def add_to_queue(self, drama_name: str, video_path: str, cover_path: str = None,
                     title: str = None, description: str = None, topics: str = None,
                     account: str = None, version: int = 1,
                     batch_id: str = None, source_dir: str = None,
                     output_dir: str = None,
                     expected_versions: Optional[list] = None) -> Dict:
        """添加到上传队列

        expected_versions: 该组本次应上传的全部版本（透传进任务字典，
        供批次完成标记/清理门槛使用；默认 None 表示未提供）。
        """
        with self.lock:
            # 检查是否已存在（按 account 去重，两个号可以发同一部剧）
            _account = account or '福桃说剧'
            with _connect_db(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id FROM upload_records 
                    WHERE drama_name=? AND version=? AND account=? AND status IN ('pending', 'uploading')
                ''', (drama_name, version, _account))
                existing = cursor.fetchone()
            
            if existing:
                return {'status': 'failed', 'error': f'[{_account}] V{version}已在队列中'}
            
            # topics 统一成 list 存内存，db 里 json.dumps 存（避免 list 被 str 化成 "[...]"）
            if isinstance(topics, str):
                topics_list = [t.strip() for t in topics.split(',') if t.strip()]
            elif isinstance(topics, list):
                topics_list = topics
            else:
                topics_list = []
            topics_db = json.dumps(topics_list, ensure_ascii=False)

            # 插入数据库
            with _connect_db(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO upload_records 
                    (drama_name, version, video_path, cover_path, title, description, topics, account, status,
                     batch_id, source_dir, output_dir)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                ''', (drama_name, version, video_path, cover_path, title, description, topics_db, _account,
                      batch_id, source_dir, output_dir))
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
                'expected_versions': expected_versions,
                'cover_status': 'not_requested',
                'draft_saved': 0,
            }
            self.queue.append(task)
            
            self._log('info', f'添加任务：{drama_name} V{version}')
            
            return {'status': 'success', 'id': record_id}

    def add_batch(self, items: List[Dict], default_expected_versions=None) -> Dict:
        """原子批量入队（R4）：一次校验 + 一个事务创建整批上传记录。

        解决「前端逐条提交可能产生半批次」：任何一条校验失败（缺字段/文件
        不存在/重复）都不会把已通过校验的其它条留在队列里——要么整批成功，
        要么成功条目在一个事务内整体落库。返回结构化明细：
        {status, accepted, duplicates, missing, rejected, ids}。

        items: 每条至少含 {drama_name, video_path, version}；
               可选 account/cover_path/title/description/topics/batch_id/
               source_dir/output_dir/expected_versions。
        """
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

                # 重复检查：同账号同剧同版本已在 pending/uploading
                with _connect_db(self.db_path) as conn:
                    cur = conn.cursor()
                    cur.execute('''
                        SELECT id FROM upload_records
                        WHERE drama_name=? AND version=? AND account=?
                          AND status IN ('pending', 'uploading')
                    ''', (drama, version, account))
                    if cur.fetchone():
                        duplicates.append({'index': idx, 'drama_name': drama,
                                           'version': version, 'account': account})
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

            # 一个事务插入全部通过校验的记录；任何失败整体回滚，不留半批次
            ids = []
            new_tasks = []
            with _connect_db(self.db_path) as conn:
                for (item, drama, video_path, version, account,
                     topics_list) in accepted:
                    topics_db = json.dumps(topics_list, ensure_ascii=False)
                    cur = conn.cursor()
                    cur.execute('''
                        INSERT INTO upload_records
                        (drama_name, version, video_path, cover_path, title,
                         description, topics, account, status,
                         batch_id, source_dir, output_dir)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    ''', (drama, version, video_path, item.get('cover_path'),
                          item.get('title'), item.get('description'), topics_db,
                          account, item.get('batch_id'),
                          item.get('source_dir'), item.get('output_dir')))
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
                        'title': item.get('title'),
                        'description': item.get('description'),
                        'topics': topics_list,
                        'account': account,
                        'status': 'pending',
                        'retry_count': 0,
                        'batch_id': item.get('batch_id'),
                        'source_dir': item.get('source_dir'),
                        'output_dir': item.get('output_dir'),
                        'expected_versions': exp_versions,
                        'cover_status': 'not_requested',
                        'draft_saved': 0,
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
                    'accepted_details': accepted_details}
    
    def start_upload(self, auto_cleanup: bool = False):
        """开始上传（先预检登录，全部有效才真正开传）。

        auto_cleanup: 本次运行是否启用后端自动清理（批次自然结束后触发）。
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
        """恢复上传"""
        with self.lock:
            if not self.uploading:
                return {'status': 'failed', 'error': '未在上传'}
            
            self.paused = False
            self._log('info', '上传已恢复')
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
        """获取上传状态"""
        with self.lock:
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
                'logs': self.logs[-50:]  # 最近50条日志
            }
    
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
    
    def _upload_worker(self):
        """上传工作线程 - 缩小锁范围，避免死锁"""
        # 先预检所有待上传账号的登录态。
        # 不预检的代价：队列里每个任务都要单独开一次 headless 浏览器校验
        # （每次 10-20s），登录过期时整条队列白跑、全部标失败。
        self._log('info', '预检登录状态...')
        if not self._precheck_logins():
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
                success = self._execute_upload(task)
            
                # 更新任务状态（短暂持锁）
                with self.lock:
                    if success:
                        task['status'] = 'completed'
                        task['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        self.completed.append(task)
                        self._log('success', f'上传成功：{task["drama_name"]} V{task["version"]}')
                    else:
                        task['status'] = 'failed'
                        task['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        self.failed.append(task)
                        self._log('error', f'上传失败：{task["drama_name"]} V{task["version"]} - {task.get("error_message")}')
            
                # 更新数据库（不持锁）—— 同步实际使用的文案与封面状态，留审计痕迹
                _topics_db = json.dumps(task.get('topics', []), ensure_ascii=False) if task.get('topics') else None
                _cover_status = task.get('cover_status', 'not_requested')
                _draft_saved = 1 if task.get('draft_saved') else 0
                _draft_verified_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if _draft_saved else None
                with _connect_db(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE upload_records 
                        SET status=?, completed_at=?, error_message=?,
                            title=?, description=?, topics=?, cover_path=?,
                            cover_status=?, draft_saved=?, draft_verified_at=?,
                            source_dir=?, output_dir=?
                        WHERE id=?
                    ''', (task['status'], task['completed_at'], task.get('error_message'),
                          task.get('title'), task.get('description'), _topics_db,
                          task.get('cover_path'), _cover_status, _draft_saved,
                          _draft_verified_at, task.get('source_dir'), task.get('output_dir'),
                          task['id']))
            
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
                    if pending == 0:
                        conn.execute(
                            "UPDATE upload_batches SET status='completed', "
                            "auto_cleanup=?, completed_at=? WHERE batch_id=?",
                            (1 if auto_cleanup else 0,
                             datetime.now().strftime('%Y-%m-%d %H:%M:%S'), batch_id))
                        conn.commit()
                        self._log('info', f'批次 {batch_id} 已标记完成')
                        if auto_cleanup and self._cleanup_callback:
                            try:
                                self._cleanup_callback(batch_id)
                                self._log('info', f'批次 {batch_id} 触发自动清理')
                            except Exception as e:
                                self._log('error', f'批次 {batch_id} 自动清理失败: {e}')
            except Exception as e:
                self._log('error', f'批次 {batch_id} 完成标记失败: {e}')
    
    def _precheck_logins(self) -> bool:
        """上传前校验队列里每个账号的登录态。全通过返回 True；
        有未登录的账号则把整条队列标失败并返回 False（不启动上传）。"""
        self.prechecking = True
        try:
            with self.lock:
                accounts = []
                for t in self.queue:
                    if t.get('account') not in accounts:
                        accounts.append(t.get('account'))

            bad, ok = [], []
            for acct in accounts:
                u = WeChatChannelsUploader(acct)
                if u.check_login():
                    ok.append(acct)
                    self._log('info', f'登录校验通过：{acct}')
                else:
                    bad.append(acct)
                    self._log('error', f'登录校验失败：{acct}')

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
                # 其余账号这次也不传：登录失效通常意味着整批都要先处理，
                # 留着会在 prechecking=False 后无人消费，挂在队列里。
                for t in list(self.queue):
                    t['status'] = 'failed'
                    t['error_message'] = '预检未通过（其他账号登录失效），请重新提交'
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

            return False
        finally:
            self.prechecking = False

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
            uploader = WeChatChannelsUploader(task['account'])

            # 检查登录状态
            if not uploader.check_login():
                task['error_message'] = '登录失败'
                return False

            # 执行上传（传递位置参数）
            # upload_to_draft 现在返回结构化 dict，不再返回 bool：
            #   {'status': 'success'|'failed', 'draft_saved': bool,
            #    'cover': {'status': ..., 'error': ..., 'cover_path': ...},
            #    'error': str|None}
            outcome = uploader.upload_to_draft(
                video_path=task['video_path'],
                title=title or '短剧精彩片段',
                tags=topics or [],
                description=description or '',
                cover_path=cover_path
            )

            if isinstance(outcome, dict):
                cover_info = outcome.get('cover') or {}
                task['cover_status'] = cover_info.get('status', 'not_requested')
                task['cover_path'] = cover_info.get('cover_path') or task.get('cover_path')
                task['draft_saved'] = bool(outcome.get('draft_saved'))
                if outcome.get('status') == 'success':
                    return True
                else:
                    task['error_message'] = outcome.get('error') or '上传失败'
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
        """记录日志"""
        log_entry = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'level': level,
            'message': message
        }
        self.logs.append(log_entry)
        print(f'[{log_entry["timestamp"]}] [{level.upper()}] {message}')
