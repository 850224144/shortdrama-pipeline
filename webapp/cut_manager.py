# -*- coding: utf-8 -*-
"""批量切片队列与任务生命周期管理。

R2：队列持久化。所有任务状态迁移同时写入 JobStore（data/jobs.db），
服务重启后重建 manager 时从库里恢复 pending/cutting（cutting 归位为
pending），停止的任务保持 pending，重启后可继续。事件可通过
get_events() / /api/cut/events 查询。
"""

import os
import threading
import time
import uuid
from datetime import datetime

from job_runtime import execution_gate
from job_store import JobStore


class BatchCutManager:
    """批量切片管理器。

    停止是协作式取消：管理器向 processor 传 threading.Event，processor 在
    ASR/渲染等阶段轮询并终止子进程树。只有 worker 真正退出后，下一次启动才
    会被允许，避免旧任务和新任务同时操作同一剧目录。
    """

    HEARTBEAT_WINDOW = 30   # 秒：心跳超过该窗口判僵死
    HEARTBEAT_INTERVAL = 5  # 秒：心跳线程刷新周期

    def __init__(self, processor, data_dir=None, store=None, output_root=None):
        self.processor = processor
        self.data_dir = data_dir
        self.output_root = output_root
        if store is not None:
            self.store = store
        else:
            db_path = f'{data_dir}/jobs.db' if data_dir else ':memory:'
            self.store = JobStore(db_path)
        self.queue = []
        self.current_task = None
        self.cutting = False
        self.paused = False
        self.stopping = False
        self.completed = []
        self.failed = []
        self.lock = threading.Lock()
        self.cut_thread = None
        self.logs = []
        self._cancel_event = None
        self._pipeline_owned = False
        self._heartbeat_thread = None
        self._heartbeat_stop = threading.Event()
        self._recover()

    # ── 持久化恢复 ─────────────────────────────────────────
    def _recover(self):
        """服务重启后从任务库恢复队列。

        - cutting（进程崩溃时正在切）归位为 pending，允许重新切片；
        - completed/failed 一并恢复，保持队列视图在重启前后一致；
        - 停止任务持久化为 pending，天然可继续。
        """
        jobs = self.store.list('cut')
        recovered_pending = 0
        with self.lock:
            for job in jobs:
                status = job['status']
                if status == 'cutting':
                    status = 'pending'
                    recovered_pending += 1
                params = job.get('params') or {}
                task = {
                    'id': job['id'],
                    'drama_name': params.get('drama_name', ''),
                    'account': params.get('account', '小桃追剧中'),
                    'resume': bool(params.get('resume', False)),
                    'status': status,
                    'created_at': job.get('created_at'),
                    'started_at': job.get('started_at'),
                    'finished_at': job.get('finished_at'),
                    'error': job.get('error'),
                }
                result = job.get('result') or {}
                if result:
                    task['output_dirs'] = result.get('output_dirs', [])
                    task['skipped'] = bool(result.get('skipped'))
                if status == 'completed':
                    task['completed_at'] = job.get('finished_at')
                    self.completed.append(task['id'])
                elif status == 'failed':
                    task['failed_at'] = job.get('finished_at')
                    self.failed.append(task['id'])
                elif status == 'pending':
                    recovered_pending += 1
                self.queue.append(task)
        if jobs:
            self._log('info', f'从任务库恢复 {len(jobs)} 个切片任务'
                              f'（{recovered_pending} 个待切）')

    # ── 任务持久化 ─────────────────────────────────────────
    @staticmethod
    def _elapsed_seconds(started_at):
        """阶段耗时（秒）：started_at 距今；无起点返回 0。"""
        if not started_at:
            return 0
        try:
            start = datetime.strptime(started_at, '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            return 0
        return max(0, int((datetime.now() - start).total_seconds()))

    def _persist_task(self, task):
        """把任务快照写入任务库。"""
        result = None
        if task.get('status') == 'completed':
            result = {
                'output_dirs': task.get('output_dirs', []),
                'skipped': bool(task.get('skipped', False)),
            }
        heartbeat = None
        if task.get('status') == 'cutting':
            heartbeat = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.store.put({
            'id': task['id'],
            'job_type': 'cut',
            'status': task['status'],
            'stage': task['status'],
            'progress': 0,
            'params': {
                'drama_name': task['drama_name'],
                'account': task.get('account', '小桃追剧中'),
                'resume': bool(task.get('resume', False)),
            },
            'result': result,
            'error': task.get('error'),
            'cancel_requested': bool(task.get('cancel_requested', False)),
            'created_at': task.get('created_at'),
            'started_at': task.get('started_at'),
            'finished_at': task.get('finished_at'),
            'heartbeat_at': heartbeat,
        })

    def add_to_queue(self, drama_name, account='小桃追剧中', resume=False):
        """添加切片任务到队列。"""
        with self.lock:
            for task in self.queue:
                if (task['drama_name'] == drama_name and
                        task['status'] in ('pending', 'cutting')):
                    return {'status': 'failed', 'error': f'{drama_name} 已在队列中'}

            task = {
                'id': uuid.uuid4().hex,
                'drama_name': drama_name,
                'account': account,
                'resume': bool(resume),
                'status': 'pending',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            self.queue.append(task)
            try:
                self._persist_task(task)
                self.store.add_event(task['id'], 'info', 'cut_enqueued',
                                     f'添加切片任务：{drama_name}'
                                     f'{"（续跑）" if resume else ""}')
                self._log('info', f'添加切片任务：{drama_name}'
                                  f'{"（续跑）" if resume else ""}')
            except Exception:
                # 持久化失败不阻断入队（内存态仍可用），但要暴露给日志
                self._log('error', f'任务持久化失败：{drama_name}')
            return {'status': 'success', 'id': task['id']}

    def start_cutting(self):
        """开始切片。"""
        with self.lock:
            if self.cutting:
                return {'status': 'failed', 'error': '已在切片中'}
            if self.cut_thread and self.cut_thread.is_alive():
                return {'status': 'failed', 'error': '上一轮任务正在停止，请稍候'}

            pending = [t for t in self.queue if t['status'] == 'pending']
            if not pending:
                return {'status': 'failed', 'error': '队列为空'}
            if not execution_gate.try_acquire_pipeline():
                return {'status': 'failed', 'error': '主处理任务正在运行，请等待完成'}

            self._cancel_event = threading.Event()
            self._pipeline_owned = True
            self.cutting = True
            self.paused = False
            self.stopping = False
            self._log('info', f'开始切片：{len(pending)}个任务')

            self.cut_thread = threading.Thread(
                target=self._cut_worker,
                name='batch-cut-worker',
                daemon=True,
            )
            try:
                self.cut_thread.start()
                self._heartbeat_stop.clear()
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, name='cut-heartbeat', daemon=True)
                self._heartbeat_thread.start()
            except Exception:
                self._pipeline_owned = False
                execution_gate.release_pipeline()
                self.cutting = False
                raise
            return {'status': 'success'}

    def pause_cutting(self):
        """当前任务完成后暂停队列。"""
        with self.lock:
            if not self.cutting:
                return {'status': 'failed', 'error': '未在切片'}
            self.paused = True
            self._log('info', '切片已暂停')
            return {'status': 'success'}

    def resume_cutting(self):
        """恢复执行队列。"""
        with self.lock:
            if not self.cutting:
                return {'status': 'failed', 'error': '未在切片'}
            self.paused = False
            self._log('info', '切片已恢复')
            return {'status': 'success'}

    def stop_cutting(self):
        """立即请求停止当前 ASR/渲染，并把当前任务放回待切片。"""
        with self.lock:
            worker_alive = bool(self.cut_thread and self.cut_thread.is_alive())
            if not self.cutting and not worker_alive:
                return {'status': 'failed', 'error': '未在切片'}

            self.paused = False
            self.cutting = False
            self.stopping = True
            if self._cancel_event is not None:
                self._cancel_event.set()
            # 标记停止信号；worker 确认后把任务归位 pending 并清掉该信号
            if self.current_task is not None:
                try:
                    self.store.update(self.current_task['id'],
                                      cancel_requested=True)
                except Exception:
                    pass
            self._log('info', '收到停止请求，正在终止当前任务')
            return {'status': 'success', 'stopping': True}

    def clear_queue(self, status='pending'):
        """清除队列（内存 + 任务库）。"""
        with self.lock:
            if status == 'all':
                cleared = len(self.queue)
                self.queue.clear()
                self.completed.clear()
                self.failed.clear()
                try:
                    self.store.delete_by_status(
                        'cut', ['pending', 'cutting', 'completed', 'failed'])
                except Exception:
                    pass
            else:
                cleared = len([t for t in self.queue if t['status'] == status])
                self.queue = [t for t in self.queue if t['status'] != status]
                try:
                    self.store.delete_by_status('cut', [status])
                except Exception:
                    pass

            self._log('info', f'清除队列：{cleared}个任务')
            return {'status': 'success', 'cleared': cleared}

    def get_status(self):
        """返回前端可直接渲染的互斥任务集合。"""
        with self.lock:
            queue = [dict(t) for t in self.queue if t['status'] in ('pending', 'cutting')]
            completed = [dict(t) for t in self.queue if t['status'] == 'completed']
            failed = [dict(t) for t in self.queue if t['status'] == 'failed']
            return {
                'queue_length': len([t for t in queue if t['status'] == 'pending']),
                'cutting': self.cutting,
                'paused': self.paused,
                'stopping': self.stopping,
                'current_task': dict(self.current_task) if self.current_task else None,
                'completed_count': len(completed),
                'failed_count': len(failed),
                'queue': queue,
                'completed': completed,
                'failed': failed,
                'logs': self.logs[-50:],
            }

    def get_events(self, job_id=None, limit=100):
        """查询持久化事件（job_id=None 查全部，含队列级事件）。"""
        return self.store.events(job_id=job_id, limit=limit)

    def _heartbeat_loop(self):
        """心跳线程：切片运行期间周期性刷新当前任务心跳。"""
        while not self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL):
            task = None
            with self.lock:
                if self.cutting and self.current_task is not None:
                    task = dict(self.current_task)
            if task is not None:
                try:
                    self.store.touch_heartbeat(task['id'])
                except Exception:
                    pass

    def _stop_heartbeat(self):
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        self._heartbeat_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(1)

    # ── 状态对账（R3）────────────────────────────────────────
    def _task_output_dir(self, job):
        """任务期望的输出目录（output_root/account/drama）。"""
        if not self.output_root:
            return None
        params = job.get('params') or {}
        drama = params.get('drama_name')
        if not drama:
            return None
        account = params.get('account', '小桃追剧中') or '小桃追剧中'
        return os.path.join(self.output_root, account, drama)

    @staticmethod
    def _output_exists(out_dir):
        """输出目录是否已存在可交付成片（_去重版.mp4 或 edl_config）。"""
        if not out_dir or not os.path.isdir(out_dir):
            return False
        try:
            names = os.listdir(out_dir)
        except OSError:
            return False
        return any(n.endswith('_去重版.mp4') for n in names) or \
            any(n.startswith('edl_config_v') and n.endswith('.json') for n in names)

    def _find_task(self, job_id):
        with self.lock:
            for t in self.queue:
                if t['id'] == job_id:
                    return t
        return None

    def reconcile(self):
        """状态对账：修复中断与状态-磁盘不一致的切片任务。

        规则（设计 2.4）：
        1. 心跳过期（>30s）的 cutting 任务视为进程僵死 → 归位 pending，
           清空运行时字段（started_at/cancel_requested/heartbeat）；
        2. completed 但输出目录无成片 → 标 failed（可重切，不阻止）；
        3. pending 但磁盘已有成片 → 记录为可恢复现场（不改状态，交用户确认）。

        返回对账报告；每个变更写事件。幂等，可重复调用。
        """
        report = {'scanned': 0, 'stale_reset': 0, 'completed_missing': 0,
                  'pending_with_output': 0, 'changed': []}

        # 1. 僵死任务（心跳超时）
        for job in self.store.stale_jobs('cut', ('cutting',),
                                         self.HEARTBEAT_WINDOW):
            tid = job['id']
            report['scanned'] += 1
            task = self._find_task(tid)
            if task is not None:
                with self.lock:
                    task['status'] = 'pending'
                    task.pop('started_at', None)
                    task['error'] = '中断任务已归位待切（心跳超时）'
                    task['cancel_requested'] = False
            try:
                self.store.update(tid, status='pending', started_at=None,
                                  error='中断任务已归位待切（心跳超时）',
                                  cancel_requested=False, heartbeat_at=None)
                self.store.add_event(tid, 'warn', 'reconcile_stale',
                                     '中断任务已归位待切（心跳超时）')
            except Exception:
                pass
            report['stale_reset'] += 1
            report['changed'].append({'id': tid, 'action': 'stale_reset'})

        # 2/3. 磁盘对账
        for status in ('completed', 'pending'):
            for job in self.store.list('cut', statuses=[status]):
                tid = job['id']
                out_dir = self._task_output_dir(job)
                if not out_dir:
                    continue
                has_output = self._output_exists(out_dir)
                report['scanned'] += 1
                if status == 'completed' and not has_output:
                    task = self._find_task(tid)
                    if task is not None:
                        with self.lock:
                            task['status'] = 'failed'
                            task['error'] = '成片缺失，已对账标记失败'
                            task['failed_at'] = datetime.now().strftime(
                                '%Y-%m-%d %H:%M:%S')
                    try:
                        self.store.update(tid, status='failed',
                                          error='成片缺失，已对账标记失败',
                                          finished_at=datetime.now().strftime(
                                              '%Y-%m-%d %H:%M:%S'))
                        self.store.add_event(tid, 'error', 'reconcile_missing',
                                             '成片缺失，已对账标记失败')
                    except Exception:
                        pass
                    report['completed_missing'] += 1
                    report['changed'].append(
                        {'id': tid, 'action': 'completed_missing'})
                elif status == 'pending' and has_output:
                    task = self._find_task(tid)
                    if task is not None:
                        with self.lock:
                            task['recovered'] = True
                            task['recover_note'] = '磁盘已有成片，待确认'
                    try:
                        self.store.add_event(tid, 'warn', 'recoverable_scene',
                                             '磁盘已有成片，待用户确认')
                    except Exception:
                        pass
                    report['pending_with_output'] += 1
                    report['changed'].append(
                        {'id': tid, 'action': 'recoverable_scene'})

        if report['stale_reset'] or report['completed_missing'] or \
                report['pending_with_output']:
            self._log('info', f'对账完成：僵死归位 {report["stale_reset"]}，'
                              f'缺成片标记 {report["completed_missing"]}，'
                              f'可恢复现场 {report["pending_with_output"]}')
        return report

    def _cut_worker(self):
        """切片工作线程。"""
        worker_thread = threading.current_thread()
        try:
            while True:
                task = None
                cancel_event = None
                with self.lock:
                    if not self.cutting:
                        break
                    if self.paused:
                        should_wait = True
                    else:
                        should_wait = False
                        task = next((t for t in self.queue if t['status'] == 'pending'), None)
                        if task is None:
                            self.cutting = False
                            self._log('info', '切片完成：所有任务已处理')
                            break
                        task['status'] = 'cutting'
                        task['started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        task.pop('stopped_at', None)
                        task.pop('error', None)
                        task['cancel_requested'] = False
                        self.current_task = task
                        cancel_event = self._cancel_event
                        try:
                            self._persist_task(task)
                            self.store.add_event(
                                task['id'], 'info', 'cut_start',
                                f'开始切片：{task["drama_name"]}')
                        except Exception:
                            pass
                        self._log('info', f'开始切片：{task["drama_name"]}')

                if should_wait:
                    time.sleep(0.2)
                    continue

                cancelled = False
                try:
                    result = self.processor.manual_cut(
                        task['drama_name'],
                        resume=task.get('resume', False),
                        cancel_event=cancel_event,
                    )
                    cancelled = bool(
                        result.get('cancelled') or
                        result.get('status') == 'cancelled' or
                        (cancel_event is not None and cancel_event.is_set())
                    )

                    with self.lock:
                        if cancelled:
                            duration_seconds = self._elapsed_seconds(task.get('started_at'))
                            task['status'] = 'pending'
                            task.pop('started_at', None)
                            task['stopped_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            task['error'] = '用户已停止，任务已放回队列'
                            task['cancel_requested'] = False
                            try:
                                self._persist_task(task)
                                self.store.add_event(
                                    task['id'], 'info', 'cut_stopped',
                                    f'切片已停止：{task["drama_name"]}',
                                    metadata={'duration_seconds': duration_seconds,
                                              'stage': 'cut'})
                            except Exception:
                                pass
                            self._log('info', f'切片已停止：{task["drama_name"]}')
                        elif result.get('status') in ('success', 'skipped'):
                            task['status'] = 'completed'
                            task['completed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            task['output_dirs'] = result.get('output_dirs', [])
                            task['skipped'] = result.get('status') == 'skipped'
                            self.completed.append(task['id'])
                            try:
                                self._persist_task(task)
                                self.store.add_event(
                                    task['id'], 'info', 'cut_completed',
                                    f'切片完成：{task["drama_name"]}',
                                    metadata={'duration_seconds': self._elapsed_seconds(task.get('started_at')),
                                              'stage': 'cut'})
                            except Exception:
                                pass
                            if task['skipped']:
                                self._log('info', f'切片跳过：{task["drama_name"]}（已有成片）')
                            else:
                                self._log(
                                    'info',
                                    f'切片完成：{task["drama_name"]} - {result.get("cut", 0)}个版本',
                                )
                        else:
                            task['status'] = 'failed'
                            task['failed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            task['error'] = result.get('error', '未知错误')
                            self.failed.append(task['id'])
                            try:
                                self._persist_task(task)
                                self.store.add_event(
                                    task['id'], 'error', 'cut_failed',
                                    f'切片失败：{task["drama_name"]} - {task["error"]}',
                                    metadata={'duration_seconds': self._elapsed_seconds(task.get('started_at')),
                                              'stage': 'cut'})
                            except Exception:
                                pass
                            self._log('error', f'切片失败：{task["drama_name"]} - {task["error"]}')
                except Exception as exc:
                    cancelled = bool(cancel_event is not None and cancel_event.is_set())
                    with self.lock:
                        if cancelled:
                            duration_seconds = self._elapsed_seconds(task.get('started_at'))
                            task['status'] = 'pending'
                            task.pop('started_at', None)
                            task['stopped_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            task['error'] = '用户已停止，任务已放回队列'
                            task['cancel_requested'] = False
                            try:
                                self._persist_task(task)
                                self.store.add_event(
                                    task['id'], 'info', 'cut_stopped',
                                    f'切片已停止：{task["drama_name"]}',
                                    metadata={'duration_seconds': duration_seconds,
                                              'stage': 'cut'})
                            except Exception:
                                pass
                            self._log('info', f'切片已停止：{task["drama_name"]}')
                        else:
                            task['status'] = 'failed'
                            task['failed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            task['error'] = str(exc)
                            self.failed.append(task['id'])
                            try:
                                self._persist_task(task)
                                self.store.add_event(
                                    task['id'], 'error', 'cut_failed',
                                    f'切片异常：{task["drama_name"]} - {exc}',
                                    metadata={'duration_seconds': self._elapsed_seconds(task.get('started_at')),
                                              'stage': 'cut'})
                            except Exception:
                                pass
                            self._log('error', f'切片异常：{task["drama_name"]} - {exc}')
                finally:
                    with self.lock:
                        self.current_task = None

                if cancelled or not self.cutting:
                    break
        finally:
            with self.lock:
                if self.cut_thread is worker_thread:
                    self.current_task = None
                    self.cutting = False
                    self.stopping = False
                    self._cancel_event = None
                    if self._pipeline_owned:
                        self._pipeline_owned = False
                        execution_gate.release_pipeline()
            self._stop_heartbeat()

    def _log(self, level, message):
        """记录切片队列日志（内存 + 任务库事件）。"""
        entry = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'level': level,
            'message': message,
        }
        self.logs.append(entry)
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]
        try:
            self.store.add_event('', level, 'queue', message)
        except Exception:
            pass
