# -*- coding: utf-8 -*-
"""任务取消、阶段上下文与跨入口资源闸门。"""

import threading
import time
import uuid
from contextlib import contextmanager


class JobCancelled(RuntimeError):
    """任务被用户主动停止，不是业务失败。"""


class ResourceBusy(RuntimeError):
    """所需执行资源已被其他任务占用。"""


class JobContext:
    """一个可取消任务的运行时上下文。"""

    def __init__(self, job_id=None, cancel_event=None, on_stage=None):
        self.job_id = job_id or uuid.uuid4().hex
        self.cancel_event = cancel_event or threading.Event()
        self.on_stage = on_stage
        self.stage_name = None
        self.progress = 0
        self.started_at = time.monotonic()

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def cancel(self):
        self.cancel_event.set()

    def check_cancelled(self):
        if self.cancelled:
            raise JobCancelled('用户已停止任务')

    def set_stage(self, name, progress=None, **metadata):
        """记录当前阶段；回调异常不得中断任务。"""
        self.check_cancelled()
        self.stage_name = name
        if progress is not None:
            self.progress = max(0, min(100, int(progress)))
        if self.on_stage:
            try:
                self.on_stage(self, name, self.progress, metadata)
            except Exception:
                pass


class ExecutionGate:
    """进程内资源闸门。

    render_slots > 1 时允许并行渲染（P1-6）：渲染槽位是计数信号量，
    容量与 CONFIG['cut']['render_workers'] 一致，保证“并行路数 ≤ 配置路数”，
    下载/ASR/上传等其他入口仍被 pipeline 锁串行保护。
    """

    def __init__(self, render_slots=1):
        self._pipeline_lock = threading.Lock()
        self._asr_lock = threading.Lock()
        self._render_slots = threading.Semaphore(max(1, int(render_slots or 1)))

    def try_acquire_pipeline(self):
        return self._pipeline_lock.acquire(blocking=False)

    def release_pipeline(self):
        if self._pipeline_lock.locked():
            try:
                self._pipeline_lock.release()
            except RuntimeError:
                pass

    @contextmanager
    def asr_slot(self, cancel_event=None):
        with self._wait_slot(self._asr_lock, 'ASR', cancel_event):
            yield

    @contextmanager
    def render_slot(self, cancel_event=None):
        self._wait_semaphore(self._render_slots, '渲染', cancel_event)
        try:
            yield
        finally:
            self._render_slots.release()

    @contextmanager
    def _wait_slot(self, lock, label, cancel_event):
        while not lock.acquire(timeout=0.25):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled('用户已停止任务')
        try:
            yield
        finally:
            lock.release()

    def _wait_semaphore(self, sem, label, cancel_event):
        while not sem.acquire(timeout=0.25):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled('用户已停止任务')


def _render_slot_count():
    """从配置读并行渲染路数（配置不可用时退回 1 = 原串行行为）。"""
    try:
        from config import CONFIG
        return max(1, int(((CONFIG.get('cut') or {}).get('render_workers')) or 1))
    except Exception:
        return 1


execution_gate = ExecutionGate(render_slots=_render_slot_count())
