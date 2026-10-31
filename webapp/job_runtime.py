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
    """进程内资源闸门。"""

    def __init__(self):
        self._pipeline_lock = threading.Lock()
        self._asr_lock = threading.Lock()
        self._render_lock = threading.Lock()

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
        with self._wait_slot(self._render_lock, '渲染', cancel_event):
            yield

    @contextmanager
    def _wait_slot(self, lock, label, cancel_event):
        while not lock.acquire(timeout=0.25):
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled('用户已停止任务')
        try:
            yield
        finally:
            lock.release()


execution_gate = ExecutionGate()
