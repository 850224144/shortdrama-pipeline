# -*- coding: utf-8 -*-
"""主消息处理任务控制器：单任务、真实停止、统一状态。"""

import threading
from datetime import datetime

from job_runtime import JobCancelled, JobContext, execution_gate


class ProcessJobController:
    def __init__(self, processor, on_status=None):
        self.processor = processor
        self.on_status = on_status
        self.lock = threading.Lock()
        self.thread = None
        self.context = None
        self.state = {
            'job_id': None,
            'current_task': None,
            'processing': False,
            'stopping': False,
            'progress': 0,
            'message': '',
            'start_time': None,
        }

    def start(self, message):
        """提交主处理任务。返回 (payload, http_status)。"""
        with self.lock:
            if self.state['processing']:
                return {'status': 'failed', 'error': '已有处理任务进行中，请等待完成'}, 409
            if not execution_gate.try_acquire_pipeline():
                return {'status': 'failed', 'error': '切片任务正在运行，请等待完成'}, 409

            context = JobContext(on_stage=self._on_stage)
            self.context = context
            self.state.update({
                'job_id': context.job_id,
                'current_task': '消息流水线',
                'processing': True,
                'stopping': False,
                'progress': 0,
                'message': '开始处理',
                'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
            self.thread = threading.Thread(
                target=self._run,
                args=(message, context),
                name=f'process-job-{context.job_id[:8]}',
                daemon=True,
            )
            try:
                self.thread.start()
            except Exception:
                self.context = None
                self.state['processing'] = False
                execution_gate.release_pipeline()
                raise
            self._emit_locked()
            return {'status': 'started', 'job_id': context.job_id, 'message': '开始处理'}, 202

    def stop(self):
        """请求停止；worker 完成子进程清理后自行结束。"""
        with self.lock:
            if not self.state['processing'] or self.context is None:
                return {'status': 'failed', 'error': '当前没有正在处理的任务'}, 409
            self.context.cancel()
            self.state['stopping'] = True
            self.state['message'] = '正在停止当前任务'
            self._emit_locked()
            return {
                'status': 'success',
                'stopping': True,
                'job_id': self.context.job_id,
                'message': '停止请求已发送，正在清理子进程',
            }, 202

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _run(self, message, context):
        try:
            result = self.processor.process_message(
                message, cancel_event=context.cancel_event)
        except JobCancelled:
            result = {'status': 'cancelled', 'error': '用户已停止任务'}
        except Exception as exc:
            result = {'status': 'failed', 'error': f'处理异常: {exc}'}
        finally:
            execution_gate.release_pipeline()

        with self.lock:
            status = result.get('status')
            self.state['processing'] = False
            self.state['stopping'] = False
            self.state['current_task'] = None
            if status == 'cancelled':
                self.state['progress'] = 0
                self.state['message'] = '任务已停止'
            elif status == 'failed':
                self.state['progress'] = 0
                self.state['message'] = result.get('error', '失败')
            else:
                self.state['progress'] = 100
                self.state['message'] = (
                    f"完成：{result.get('cut', 0)}个成功，"
                    f"{result.get('failed_cuts', 0)}个失败")
            self.context = None
            self._emit_locked()
        return result

    def _on_stage(self, context, name, progress, metadata):
        with self.lock:
            if self.context is not context:
                return
            self.state['progress'] = progress
            self.state['message'] = name
            self._emit_locked()

    def _emit_locked(self):
        if not self.on_status:
            return
        try:
            self.on_status(dict(self.state))
        except Exception:
            pass
