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

# 导入上传模块
from wechat_uploader import WeChatChannelsUploader

# 导入配置
from processor import CONFIG

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
        
    def _init_database(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
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
        
        # 创建索引
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_drama_version 
            ON upload_records(drama_name, version)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_status 
            ON upload_records(status)
        ''')
        
        conn.commit()
        conn.close()
        
    def add_to_queue(self, drama_name: str, video_path: str, cover_path: str = None,
                     title: str = None, description: str = None, topics: str = None,
                     account: str = None, version: int = 1) -> Dict:
        """添加到上传队列"""
        with self.lock:
            # 检查是否已存在（按 account 去重，两个号可以发同一部剧）
            _account = account or '福桃说剧'
            with sqlite3.connect(self.db_path) as conn:
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
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO upload_records 
                    (drama_name, version, video_path, cover_path, title, description, topics, account, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                ''', (drama_name, version, video_path, cover_path, title, description, topics_db, _account))
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
                'retry_count': 0
            }
            self.queue.append(task)
            
            self._log('info', f'添加任务：{drama_name} V{version}')
            
            return {'status': 'success', 'id': record_id}
    
    def start_upload(self):
        """开始上传（先预检登录，全部有效才真正开传）"""
        with self.lock:
            if self.uploading or self.prechecking:
                return {'status': 'failed', 'error': '正在上传或预检中'}

            if not self.queue:
                return {'status': 'failed', 'error': '队列为空'}

            self.uploading = True
            self.prechecking = True
            self.paused = False
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
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute('DELETE FROM upload_records')
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
                conn = sqlite3.connect(self.db_path)
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
        conn = sqlite3.connect(self.db_path)
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
            with self.lock:
                self.uploading = False
                self.prechecking = False
                self.current_task = None
            return

        while True:
            # 获取任务（短暂持锁）
            with self.lock:
                if not self.queue:
                    break
                
                if self.paused:
                    time.sleep(1)
                    continue
                
                task = self.queue.pop(0)
                self.current_task = task
                task['status'] = 'uploading'
                task['started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 更新数据库（不持锁）
            with sqlite3.connect(self.db_path) as conn:
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
            
            # 更新数据库（不持锁）—— 同步实际使用的文案，留审计痕迹
            _topics_db = json.dumps(task.get('topics', []), ensure_ascii=False) if task.get('topics') else None
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE upload_records 
                    SET status=?, completed_at=?, error_message=?,
                        title=?, description=?, topics=?, cover_path=?
                    WHERE id=?
                ''', (task['status'], task['completed_at'], task.get('error_message'),
                      task.get('title'), task.get('description'), _topics_db,
                      task.get('cover_path'), task['id']))
            
            # 检查账号切换（短暂持锁）
            with self.lock:
                if self.queue:
                    next_account = self.queue[0].get('account')
                    if next_account != self.current_account:
                        self.current_account = next_account
                        self._log('info', f'切换账号：{self.current_account}')
            
            # 等待间隔（避免风控）
            time.sleep(2)
        
        with self.lock:
            self.uploading = False
            self.current_task = None
            self._log('info', '上传完成')
    
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
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute(
                            "UPDATE upload_records SET status='failed', completed_at=?, "
                            "error_message=? WHERE id=?",
                            (t['completed_at'], t['error_message'], t['id']))
                except Exception as e:
                    self._log('error', f'写失败记录出错：{e}')

            return False
        finally:
            self.prechecking = False

    def open_draft_box(self, account: str) -> Dict:
        """打开草稿箱浏览器窗口（复用上传时的登录态）"""
        if account in self.draft_pages:
            return {'status': 'success', 'message': f'{account} 草稿箱已打开'}
        
        # 检查是否正在上传（profile 锁互斥）
        if self.uploading and self.current_account == account:
            return {'status': 'failed', 'error': f'{account} 正在上传，请等上传完成后再打开草稿箱'}
        
        try:
            from DrissionPage import ChromiumPage, ChromiumOptions
            from wechat_uploader import WeChatChannelsUploader
            
            uploader = WeChatChannelsUploader(account)
            user_data_path = uploader.user_data_path
            os.makedirs(user_data_path, exist_ok=True)
            
            co = ChromiumOptions()
            co.set_paths(user_data_path=user_data_path)
            co.set_argument('--disable-blink-features=AutomationControlled')
            
            page = ChromiumPage(co)
            page.get('https://channels.weixin.qq.com/platform/post/draftListManager')
            
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
            page.quit()
            self._log('info', f'{account} 草稿箱已关闭')
            return {'status': 'success', 'message': f'{account} 草稿箱已关闭'}
        except Exception as e:
            self.draft_pages.pop(account, None)
            return {'status': 'failed', 'error': str(e)}
    
    def get_draft_status(self) -> Dict:
        """获取草稿箱打开状态"""
        return {'open_accounts': list(self.draft_pages.keys())}
    
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
            
            # ── 推断封面路径（covers/{剧名}_{版本}_封面.jpg）──
            if not cover_path:
                import re as _re
                drama_safe = _re.sub(r'[^\w\u4e00-\u9fff]', '_', task['drama_name'])
                cover_path = os.path.join(video_dir, 'covers', f'{drama_safe}_{task["version"]}_封面.jpg')
                if not os.path.exists(cover_path):
                    cover_path = None  # 不存在就不传，让上传器用默认封面
            
            # 初始化上传器
            uploader = WeChatChannelsUploader(task['account'])

            # 检查登录状态
            if not uploader.check_login():
                task['error_message'] = '登录失败'
                return False

            # 执行上传（传递位置参数）
            # upload_to_draft 返回 bool（True=成功，False=失败）
            success = uploader.upload_to_draft(
                video_path=task['video_path'],
                title=title or '短剧精彩片段',
                tags=topics or [],
                description=description or '',
                cover_path=cover_path
            )

            if success:
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