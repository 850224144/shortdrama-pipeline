# -*- coding: utf-8 -*-
"""
短剧工坊 流水线处理器
功能：下载、切片、去重、错误处理、进度监控

P1-5 拆分后：下载 / 切片 / EDL / 渲染 / 报告 五个域分别在
pipeline/*_mixin.py，本文件只保留应用装配与流程入口（编排层）。
"""
import os
import re
import json
import glob
import time
import math
import shutil
import subprocess
import sys
import threading
import random
import uuid
import hashlib
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 导入AI文案生成器
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_copy_generator import AICopyGenerator
from job_runtime import JobCancelled, execution_gate
from job_store import JobStore
from config import CONFIG
from log_utils import make_entry
from pending_queue import (create_batch as _pq_create,
                           mark as _pq_mark,
                           mark_by_drama as _pq_mark_by_drama,
                           load_pending as _pq_load,
                           clear_pending as _pq_clear)
from pipeline.core import (CutCancelled, DownloadStalled, LinkExtractor,
                          PUNCTUATIONS, DEFAULT_CUT_CONFIG, CUT_CONFIG_ABS)
from pipeline.download_mixin import DownloadMixin
from pipeline.cut_mixin import CutMixin
from pipeline.edl_mixin import EdlMixin
from pipeline.render_mixin import RenderMixin
from pipeline.report_mixin import ReportMixin
from pipeline import duration as _duration_mod
from pipeline import edl_approval as _edl_mod



class Logger:
    """日志记录器"""
    
    def __init__(self, log_dir, socketio=None):
        self.log_dir = log_dir
        self.socketio = socketio
        os.makedirs(log_dir, exist_ok=True)
        self.logs = []
        self.lock = threading.Lock()
        self._clean_old_logs()

    def _clean_old_logs(self, keep_days=7):
        """清理 N 天前的日志文件"""
        import time as _t
        cutoff = _t.time() - keep_days * 86400
        try:
            for f in os.listdir(self.log_dir):
                if f.endswith('.log'):
                    fp = os.path.join(self.log_dir, f)
                    if os.path.getmtime(fp) < cutoff:
                        try: os.remove(fp)
                        except: pass
        except: pass
    
    def log(self, drama_name, level, message, account='', task_id='', batch_id='', stage=''):
        """记录日志；条目结构见 log_utils.make_entry。"""
        log_entry = make_entry(
            level, message, drama_name=drama_name, account=account,
            task_id=task_id, batch_id=batch_id, stage=stage)
        timestamp = log_entry['timestamp']

        with self.lock:
            self.logs.append(log_entry)

            # 写入文件（人类可读行，格式保持不变）
            log_file = os.path.join(self.log_dir, f"{datetime.now().strftime('%Y%m%d')}.log")
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] [{log_entry['level']}] [{drama_name}] {message}\n")

        # 推送到前端
        if self.socketio:
            try:
                self.socketio.emit('log_update', log_entry)
            except Exception as e:
                print(f"推送日志失败: {e}")

        return log_entry
    
    def get_recent_logs(self, limit=100):
        """获取最近的日志"""
        with self.lock:
            return self.logs[-limit:]
    
    def cleanup_logs(self, days=30):
        """清理过期日志"""
        from datetime import timedelta
        cutoff_date = datetime.now() - timedelta(days=days)
        deleted_count = 0
        
        for f in os.listdir(self.log_dir):
            if f.endswith('.log'):
                fpath = os.path.join(self.log_dir, f)
                try:
                    file_date = datetime.strptime(f[:8], '%Y%m%d')
                    if file_date < cutoff_date:
                        os.remove(fpath)
                        deleted_count += 1
                except Exception:
                    pass
        
        return deleted_count


class StateManager:
    """状态管理器"""
    
    def __init__(self, state_file):
        self.state_file = state_file
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
    
    def load(self):
        """加载状态"""
        try:
            if not os.path.exists(self.state_file):
                return {
                    'completed': [],
                    'failed': [],
                    'pending_confirm': [],
                    'current_task': None,
                    'history': []
                }
            
            with open(self.state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            
            # 确保所有键都存在
            state.setdefault('completed', [])
            state.setdefault('failed', [])
            state.setdefault('pending_confirm', [])
            state.setdefault('history', [])
            
            return state
        except Exception as e:
            # JSON 损坏：备份原文件再返回空，避免静默丢历史
            print(f"[WARN] 状态文件损坏({self.state_file}): {e}")
            try:
                bak = self.state_file + f'.broken.{int(time.time())}'
                shutil.copy(self.state_file, bak)
                print(f"[WARN] 已备份损坏文件到: {bak}")
            except Exception:
                pass
            return {
                'completed': [],
                'failed': [],
                'pending_confirm': [],
                'current_task': None,
                'history': []
            }
    
    def save(self, state):
        """保存状态（原子写：.tmp + os.replace，防崩溃截断 state.json）"""
        try:
            with self.lock:
                tmp = self.state_file + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.state_file)
            return True
        except Exception as e:
            print(f"保存状态失败: {e}")
            return False
    
    def add_completed(self, drama_name, account, versions, path):
        """添加完成记录"""
        state = self.load()
        state['completed'].append({
            'name': drama_name,
            'account': account,
            'versions': versions,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'path': path
        })
        
        state['history'].append({
            'type': 'completed',
            'name': drama_name,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'details': {'versions': versions, 'path': path}
        })
        
        # 限制历史记录
        if len(state['history']) > 100:
            state['history'] = state['history'][-100:]
        
        self.save(state)
    
    def add_failed(self, drama_name, reason):
        """添加失败记录"""
        state = self.load()
        state['failed'].append({
            'name': drama_name,
            'reason': reason,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M')
        })
        
        state['history'].append({
            'type': 'failed',
            'name': drama_name,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'details': {'reason': reason}
        })
        
        self.save(state)
    
    def get_history(self, limit=50):
        """获取历史记录"""
        state = self.load()
        return list(reversed(state['history'][-limit:]))


class PipelineProcessor(DownloadMixin, CutMixin, EdlMixin, RenderMixin, ReportMixin):
    """流水线处理器：编排层（各域实现见 pipeline/*_mixin.py）。"""

    FIXED_WATERMARKS = ('小桃追剧中', '福桃说剧')

    def __init__(self, logger, state_manager):
        self.logger = logger
        self.state_manager = state_manager
        self.processing = False
        self._job_store = None  # P3: 下载任务持久化（惰性）
        self._cut_progress_callback = None
        self.lock = threading.Lock()
        
        # 初始化目录
        os.makedirs(CONFIG['download_base_dir'], exist_ok=True)
        os.makedirs(CONFIG['output_dir'], exist_ok=True)

    def process_message(self, message, cancel_event=None, watermark=None):
        """处理消息

        watermark: 可选水印选择。None/''=自动交替（现有轮询）；
            "小桃追剧中"/"福桃说剧"=固定账号（账号/水印/输出目录统一为该值）；
            其它非空文本=自定义水印文字（只改水印，账号仍自动交替）。
        """
        if cancel_event is not None and cancel_event.is_set():
            return {'status': 'cancelled', 'cancelled': True,
                    'error': '用户已停止任务'}
        with self.lock:
            if self.processing:
                return {'status': 'busy', 'message': '正在处理其他任务'}
            
            self.processing = True
        
        # 检查资源
        ok, reason = self.check_resources()
        if not ok:
            self.logger.log('system', 'ERROR', f'资源检查失败：{reason}')
            self.processing = False
            return {'status': 'failed', 'error': reason}
        
        try:
            # 检查是否是手动切片模式
            for prefix in ('/cut ', '/切片 '):
                if message.startswith(prefix):
                    return self.manual_cut(message[len(prefix):].strip(),
                                           cancel_event=cancel_event,
                                           watermark=watermark)
            
            # 提取链接
            links = LinkExtractor.extract(message)
            if not links:
                self.logger.log('system', 'WARN', f'未找到链接')
                return {'status': 'failed', 'error': '未找到百度网盘链接'}
            
            self.logger.log('system', 'INFO', f'找到{len(links)}个链接，开始下载...')
            # 队列落盘：崩溃后启动钩子据此重提未完成链接（修复“内存队列丢失”）
            try:
                _pq_create(links)
            except Exception as _e:
                self.logger.log('system', 'WARN', f'批次队列落盘失败（不影响本次处理）：{_e}')
            
            # 下载所有链接
            download_results = self.download_batch(links, cancel_event=cancel_event)
            # 下载阶段回标：失败→failed（下次重启重提），成功→pending（等切片收尾）
            for _r in download_results:
                try:
                    if _r.get('cancelled'):
                        continue
                    _pq_mark(link_id=_r.get('link_id'), link=_r.get('link'),
                             status=('pending' if _r.get('success') else 'failed'),
                             drama_name=_r.get('drama_name'))
                except Exception:
                    pass
            self._raise_if_cancelled(cancel_event)
            cancelled_downloads = [r for r in download_results if r.get('cancelled')]
            if cancelled_downloads:
                return {
                    'status': 'cancelled',
                    'cancelled': True,
                    'error': '用户已停止任务',
                    'failed_downloads': len(cancelled_downloads),
                }
            
            successful_downloads = [r for r in download_results if r['success']]
            failed_downloads = [r for r in download_results if not r['success']]
            
            self.logger.log('system', 'INFO',
                           f'下载完成: 成功{len(successful_downloads)}, 失败{len(failed_downloads)}')

            # P7: 完整性门控 —— 明显残缺的下载不进切片（视为失败，避免产出残缺成品）。
            # 放行策略：集数达 min_download_files 下限且完成度达 min_completeness。
            min_eps = CONFIG['download'].get('min_download_files', 10)
            min_ratio = CONFIG['download'].get('min_completeness', 0.5)
            gated = []
            for r in successful_downloads:
                mp4s = [f for f in (r.get('downloaded_files') or [])
                        if f.endswith('.mp4') and '.downloading' not in f]
                incomplete = (r.get('episode_complete') is False)
                total = r.get('total_episodes') or 0
                ratio = (len(mp4s) / total) if (incomplete and total) else 1.0
                if incomplete and (len(mp4s) < min_eps or ratio < min_ratio):
                    self.logger.log('system', 'WARN',
                        f'{r.get("drama_name") or r.get("link_id")}: '
                        f'下载不完整（{len(mp4s)}/{total}集，{ratio:.0%}），低于切片阈值'
                        f'（{min_eps}集/{min_ratio:.0%}），跳过切片')
                    r['error'] = f'下载不完整（{len(mp4s)}/{total}集），跳过切片'
                    r['success'] = False
                    failed_downloads.append(r)
                else:
                    gated.append(r)
            successful_downloads = gated
            
            if not successful_downloads:
                return {
                    'status': 'failed',
                    'error': '所有下载失败',
                    'failed_downloads': failed_downloads
                }
            
            # 切片所有剧
            if cancel_event is not None and cancel_event.is_set():
                return {'status': 'cancelled', 'cancelled': True,
                        'error': '用户已停止任务'}
            cut_results = self.cut_batch(successful_downloads,
                                         cancel_event=cancel_event,
                                         watermark=watermark)
            if any(r.get('cancelled') for r in cut_results):
                return {'status': 'cancelled', 'cancelled': True,
                        'error': '用户已停止任务'}
            
            successful_cuts = [r for r in cut_results if r['success']]
            # 已切过而跳过的版本算“完成”，不能算失败——否则队列会被永久标黄
            skipped_cuts = [r for r in cut_results
                            if r.get('skipped') and not r['success']]
            failed_cuts = [r for r in cut_results
                           if not r['success'] and not r.get('skipped')]
            
            self.logger.log('system', 'INFO',
                           f'切片完成: 成功{len(successful_cuts)}, '
                           f'跳过{len(skipped_cuts)}, 失败{len(failed_cuts)}')
            # 队列收尾：成功/跳过交付的链接标 done；失败保留 failed 供重启重试
            try:
                for _r in successful_cuts + skipped_cuts:
                    # 先按 link_id 精确回标（防重跳过的下载结果没有 drama_name，
                    # 只按剧名会漏标），再按剧名兜底。
                    _pq_mark(link_id=_r.get('link_id'), link=_r.get('link'),
                             status='done', drama_name=_r.get('name'))
                    _pq_mark_by_drama(_r.get('name'), 'done')
                for _r in failed_cuts:
                    _pq_mark(link_id=_r.get('link_id'), link=_r.get('link'),
                             status='failed', drama_name=_r.get('name'))
                    _pq_mark_by_drama(_r.get('name'), 'failed')
                _q = _pq_load()
                if _q and all(x.get('status') == 'done' for x in _q.get('links', [])):
                    _pq_clear()
            except Exception as _e:
                self.logger.log('system', 'WARN', f'批次队列收尾失败：{_e}')
            
            # 记录切片失败详情
            for fc in failed_cuts:
                self.logger.log('system', 'ERROR', f'切片失败 - {fc.get("name", "未知")}: {fc.get("error", "未知错误")}')
            
            return {
                'status': 'success' if successful_cuts else 'partial_failure',
                'total_links': len(links),
                'downloaded': len(successful_downloads),
                'failed_downloads': len(failed_downloads),
                'cut': len(successful_cuts),
                'failed_cuts': len(failed_cuts),
                'output_dirs': [r['output_dir'] for r in successful_cuts],
                'errors': [
                    {'type': 'download', 'link': r['link'], 'error': r['error']}
                    for r in failed_downloads
                ] + [
                    {'type': 'cut', 'name': r['name'], 'error': r['error']}
                    for r in failed_cuts
                ]
            }
            
        except CutCancelled:
            self.logger.log('system', 'INFO', '处理已停止')
            return {'status': 'cancelled', 'cancelled': True,
                    'error': '用户已停止任务'}
        except Exception as e:
            self.logger.log('system', 'ERROR', f'处理失败: {str(e)}')
            return {'status': 'failed', 'error': str(e)}
        
        finally:
            with self.lock:
                self.processing = False

    def _update_state(self, status, drama_name, progress, message):
        """更新状态"""
        try:
            state = self.state_manager.load()
            state["current_task"] = {
                "status": status,
                "drama_name": drama_name,
                "progress": progress,
                "message": message,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            self.state_manager.save(state)
            return True
        except Exception as e:
            self.logger.log('system', 'WARN', f'状态更新失败：{str(e)}')
            return False

    def _acquire_lock(self, lock_path):
        """获取文件锁"""
        f = None
        try:
            import msvcrt
            f = open(lock_path, 'w')
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except Exception:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
            return None

    def _release_lock(self, lock_file):
        """释放文件锁（P10: 同时删除锁文件，避免 .processing.lock 0 字节残留）"""
        if lock_file:
            try:
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass
            # P10: 释放后删除锁文件，不留 0 字节残留；下一进程获取时会重建。
            try:
                os.remove(lock_file.name)
            except Exception:
                pass

    def check_resources(self):
        """检查资源"""
        # 磁盘空间
        try:
            usage = shutil.disk_usage(CONFIG['output_dir'])
            free_gb = usage.free / (1024 ** 3)
            if free_gb < CONFIG['resource']['min_disk_gb']:
                return False, f"磁盘空间不足：{free_gb:.1f}GB < {CONFIG['resource']['min_disk_gb']}GB"
        except Exception:
            pass
        
        # 并发限制
        state = self.state_manager.load()
        current_tasks = state.get('current_task', []) or []
        if isinstance(current_tasks, dict):
            current_tasks = [current_tasks]
        active_tasks = len([t for t in current_tasks if t.get('status') == 'processing'])
        if active_tasks >= CONFIG['resource']['max_concurrent']:
            return False, f"并发任务过多：{active_tasks}"
        
        return True, "资源充足"

    def normalize_name(self, name):
        """标准化剧名"""
        return re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', name)

    def is_fuzzy_duplicate(self, name1, name2):
        """模糊去重：用 SequenceMatcher ratio>0.85，避免"归乡检察官"和"归乡检察官2"误判"""
        import difflib
        norm1 = self.normalize_name(name1)
        norm2 = self.normalize_name(name2)
        if not norm1 or not norm2:
            return False
        if norm1 == norm2:
            return True
        ratio = difflib.SequenceMatcher(None, norm1, norm2).ratio()
        return ratio >= 0.85

    def should_process(self, drama_name, src_dir, out_dir, expected_count=None,
                       force=False):
        """检查是否应该处理。

        expected_count：本次计划交付的版本数。旧逻辑只要目录里有 3 个 mp4
        就永久跳过，导致 2/8、6/8 这类未完成任务无法补齐；显式重试还会被
        completed 历史记录拦住。传入计划数后改为“已达到计划数才跳过”。
        force：用户在历史记录中显式点击重切时绕过历史闸门（锁仍照常使用）。
        """
        if force:
            return True, '用户显式重切'
        # 第1层：状态文件检查
        state = self.state_manager.load()
        completed_names = [c["name"] for c in state.get("completed", [])]
        
        if drama_name in completed_names:
            return False, f"状态文件显示已完成：{drama_name}"
        
        # 第2层：素材目录标记检查
        marker_file = os.path.join(src_dir, "已切完.txt")
        if os.path.exists(marker_file):
            return False, f"素材目录有已切完标记"
        
        # 第3层：输出目录检查
        if os.path.exists(out_dir):
            try:
                mp4s = [f for f in os.listdir(out_dir) 
                       if f.endswith(".mp4") and "测试" not in f and "raw" not in f]
                required = int(expected_count) if expected_count is not None else 3
                if len(mp4s) >= max(1, required):
                    return False, f"输出目录已有成品：{len(mp4s)}个"
            except Exception as e:
                self.logger.log('system', 'WARN', f'检查输出目录失败：{str(e)}')
        
        # 第4层：模糊去重
        for name in completed_names:
            if self.is_fuzzy_duplicate(drama_name, name):
                return False, f"与已完成剧重复：{name}"
        
        return True, "可以处理"

    def assign_account(self, drama_name, submit_order=None):
        """按交叉顺序分配账号（纯轮询，不再按剧情分类）

        一个剧归小桃追剧中，下一个归福桃说剧，循环。
        返回值即最终输出目录名/水印名，三者（账号/水印/output）统一，
        不再有 "福桃追剧"（逻辑名）与 "福桃说剧"（目录名）的错位。

        稳定性：已切过的剧（state.completed 有记录）复用其历史账号，
        保证重切/续跑不因 submit_order 漂移到另一账号；新剧才按
        submit_order 交叉交替。

        Args:
            drama_name: 剧名
            submit_order: 提交顺序（从0开始），决定交叉轮询的落点

        Returns:
            账号名称："小桃追剧中" 或 "福桃说剧"
        """
        # 已切过的剧：复用历史账号，保证归属稳定（重切不漂移）
        try:
            state = self.state_manager.load()
            for c in state.get('completed', []):
                if (c.get('name') or '') == drama_name and c.get('account'):
                    return c['account']
        except Exception:
            pass
        if submit_order is not None:
            return "小桃追剧中" if submit_order % 2 == 0 else "福桃说剧"
        # 无序号时按已完成剧数继续交叉（保持轮询连贯）
        try:
            order = self.get_submit_order()
            return "小桃追剧中" if order % 2 == 0 else "福桃说剧"
        except Exception:
            return "小桃追剧中"

    def get_submit_order(self):
        """获取当前提交顺序"""
        # 从状态文件中读取已处理的剧数
        state = self.state_manager.load()
        completed = state.get('completed', [])
        return len(completed)

    def get_watermark_name(self, account):
        """根据账号获取水印名称（也是输出目录的子目录名）"""
        # 账号 → 水印名称 → 输出目录
        # 发布账号：福桃追剧/福桃说剧 → 福桃说剧 → output/福桃说剧/
        # 切片账号：小桃追剧中 → 小桃追剧中 → output/小桃追剧中/
        if account in ("福桃追剧", "福桃说剧"):
            return "福桃说剧"
        return "小桃追剧中"

    def get_output_subdir(self, drama_name, submit_order=None, account=None):
        """获取输出目录的子目录。

        优先使用本次实际分配账号；未传入时才回退全局 upload_account。
        这样 Web 展示的账号、水印和输出目录来自同一决策，不会各写各的。
        """
        upload_account = account or CONFIG.get('upload_account', '福桃说剧')
        return self.get_watermark_name(upload_account)

    def apply_watermark_override(self, download_result, cut_cfg, drama_name):
        """解析提交消息/手动切片透传的 watermark_override。

        规则：
          - 固定账号名（FIXED_WATERMARKS）→ 返回该账号（账号覆盖，输出目录随之）；
          - 其它非空文本 → 仅水印文字覆盖（写进 cut_cfg['watermark']），
            账号仍按自动交替 assign_account 返回；
          - 无覆盖 → 走原自动交替逻辑，返回 assign_account(drama_name)。

        就地更新 cut_cfg['watermark']（自定义文字），不改变其它字段。
        返回值：最终 account（覆盖或自动交替结果）。
        """
        wm_override = download_result.get('watermark_override')
        if wm_override and wm_override in self.FIXED_WATERMARKS:
            # 固定账号：账号/水印/输出目录统一为该值。回写 download_result 让
            # 下游（内容去重指纹键 :2623、add_completed 账号 :2688）拿到同一账号，
            # 否则指纹/上报落在自动交替账号下，与发布账号不一致，会削弱
            # upload_manager._fingerprint_blocks 的防重发查重（查 auto 记录，
            # 实际记录在 fixed → 漏检）。
            download_result['account'] = wm_override
            download_result['auto_selected'] = True
            return wm_override
        account = ((download_result.get('account')
                    if download_result.get('auto_selected') else None)
                   or self.assign_account(drama_name))
        # 自定义水印文字：写进本剧 cut_cfg，供 _generate_edl_versions 组装
        # edl_config.watermark_text（优先级高于输出目录账号名）。清洗 drawtext
        # 危险字符（' : % \），避免破坏 filter 语法；清洗后为空则不覆盖。
        if wm_override and wm_override not in self.FIXED_WATERMARKS:
            custom_wm = re.sub(r"[':%\\]", '', wm_override).strip()
            if custom_wm:
                cut_cfg['watermark'] = custom_wm
        return account

    def check_drama_id(self, drama_name):
        """检查CPS ID"""
        if not os.path.exists(CONFIG['cache_path']):
            return False, None
        
        try:
            with open(CONFIG['cache_path'], 'r', encoding='utf-8') as f:
                cache = json.load(f)
        except Exception as e:
            self.logger.log('system', 'WARN', f'读取缓存失败：{str(e)}')
            return False, None
        
        norm = self.normalize_name(drama_name)
        for key, entries in cache.get("dramas", {}).items():
            if norm in key or key in norm:
                for e in entries:
                    if e.get("is_published") == 1:
                        return True, e.get("drama_id")
        
        return False, None

    def _resolve_cut_config(self, config):
        """把原始 config（可为 None/部分字段）解析为完整默认合并配置。

        - version_count: None=自动；否则 clamp 到 [1, 8]（用户硬性要求）。
        - min_dur/max_dur: clamp 到 [60, 1200]，且保证 min < max。
        - reuse_material: 布尔。
        - watermark: None=账号名。
        """
        rc = dict(DEFAULT_CUT_CONFIG)
        if isinstance(config, dict):
            for k in DEFAULT_CUT_CONFIG:
                if config.get(k) is not None:
                    rc[k] = config[k]
        # 版本数 clamp 1-8
        vc = rc.get('version_count')
        if vc is not None:
            try:
                rc['version_count'] = max(
                    CUT_CONFIG_ABS['version_min'],
                    min(CUT_CONFIG_ABS['version_max'], int(vc)))
            except (TypeError, ValueError):
                rc['version_count'] = None
        # 时长 clamp + 保证 min < max
        try:
            rc['min_dur'] = max(CUT_CONFIG_ABS['dur_min'],
                                min(CUT_CONFIG_ABS['dur_max'],
                                    float(rc['min_dur'])))
            rc['max_dur'] = max(CUT_CONFIG_ABS['dur_min'],
                                min(CUT_CONFIG_ABS['dur_max'],
                                    float(rc['max_dur'])))
            if rc['min_dur'] >= rc['max_dur']:
                rc['max_dur'] = min(CUT_CONFIG_ABS['dur_max'],
                                    rc['min_dur'] + 60.0)
        except (TypeError, ValueError):
            rc['min_dur'] = float(DEFAULT_CUT_CONFIG['min_dur'])
            rc['max_dur'] = float(DEFAULT_CUT_CONFIG['max_dur'])
        rc['reuse_material'] = bool(rc.get('reuse_material'))
        return rc

    def determine_version_count(self, ep_count):
        """判断版本数"""
        if ep_count >= 30:
            return 8
        return 6
