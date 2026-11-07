# -*- coding: utf-8 -*-
"""队列管理器公共基类（阶段一 4.3）。

只抽两套队列共有的内存状态与通用能力；持久化存储、执行流程、前端快照结构
差异保留在子类：

- 切片走 JobStore（jobs.db，job_type='cut'），上传走 upload.db 直连（upload_records
  等表），两套表结构不同构，基类不统一存储；
- 执行钩子 ``_execute_task(task)`` 由子类实现；
- 重试上限 / 超时熔断提供判定工具与归位约定，本轮不擅自改变现有自动行为
  （现有两队列失败即入 failed，无自动重试；接入点留待后续评审）。
"""
import threading
from datetime import datetime

# 默认重试上限与单任务超时（秒）；子类可覆盖。
DEFAULT_MAX_RETRIES = 2
DEFAULT_TASK_TIMEOUT = 1800  # 30 分钟


class BaseQueueManager:
    """上传 / 切片队列的公共状态机骨架。"""

    def __init__(self):
        # ── 公共内存状态 ──────────────────────────────
        self.queue = []              # 待处理任务列表
        self.current_task = None     # 当前执行任务
        self.running = False         # 通用运行标志（子类各自映射 cutting / uploading）
        self.paused = False          # 当前任务完成后暂停
        self.stopping = False        # 停止请求标志
        self.completed = []          # 已完成集合（元素类型由子类约定）
        self.failed = []             # 失败集合（元素类型由子类约定）
        self.logs = []               # 内存日志（结构见 log_utils.make_entry）
        self.lock = threading.Lock()

        # ── 重试 / 熔断基线 ────────────────────────────
        self.max_retries = DEFAULT_MAX_RETRIES
        self.task_timeout = DEFAULT_TASK_TIMEOUT

    # ── 任务归位（通用工具，子类可选用；不替换子类现有归位路径）──
    def _mark_completed(self, task):
        with self.lock:
            self.completed.append(task['id'])
            task['status'] = 'completed'
            task['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _mark_failed(self, task, error=''):
        with self.lock:
            self.failed.append(task['id'])
            task['status'] = 'failed'
            task['error'] = error
            task['failed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ── 重试上限 ─────────────────────────────────────
    def _record_retry(self, task):
        """累加任务重试次数并返回当前次数。"""
        task['retry_count'] = int(task.get('retry_count') or 0) + 1
        return task['retry_count']

    def _should_retry(self, task):
        """是否仍在重试上限内。"""
        return int(task.get('retry_count') or 0) < self.max_retries

    # ── 超时熔断 ─────────────────────────────────────
    @staticmethod
    def _elapsed_seconds(started_at):
        """started_at 距今秒数；无起点或格式非法返回 0。"""
        if not started_at:
            return 0
        try:
            start = datetime.strptime(started_at, '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            return 0
        return max(0, int((datetime.now() - start).total_seconds()))

    def _timeout_exceeded(self, task):
        """当前任务是否超过单任务超时阈值。"""
        if not task:
            return False
        return self._elapsed_seconds(task.get('started_at')) > self.task_timeout

    # ── 执行钩子（子类实现）───────────────────────────
    def _execute_task(self, task):
        raise NotImplementedError

    # ── 公共快照计数（子类组装各自前端 dict 时复用）─────
    def _common_counts(self):
        with self.lock:
            active = [t for t in self.queue
                      if t.get('status') in ('pending', 'cutting', 'uploading')]
            return {
                'queue_length': len(active),
                'completed_count': len(self.completed),
                'failed_count': len(self.failed),
            }
