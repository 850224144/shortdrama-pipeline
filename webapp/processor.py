# -*- coding: utf-8 -*-
"""
短剧工坊 流水线处理器
功能：下载、切片、去重、错误处理、进度监控
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


# 标点符号列表
PUNCTUATIONS = ['，', '。', '？', '！', '、', '：', '；', '“', '”', '（', '）', ' ', '\t']


# 兼容现有调用与测试名称；取消语义由 job_runtime 统一提供。
CutCancelled = JobCancelled


# 每部剧的切制参数默认值（用户可逐剧覆盖，存 job_store.cut_config）。
# 语义（用户拍板 A）：时长与版本数无关——每版都在 [min_dur, max_dur] 内。
# max_dur=330 修复原 CONFIG['duration']['max_final']=1200 导致的 V1 膨胀
# （63 段/21 分钟 → 渲染 600s 超时 → 素材池耗尽 → 仅 1 版成功）。
DEFAULT_CUT_CONFIG = {
    'version_count': None,     # None = 自动（集数判定 + 素材容量 clamp）
    'min_dur': 180.0,          # 单版成片下限（s）
    'max_dur': 330.0,          # 单版成片上限（s）≈5.5 分钟
    'reuse_material': False,   # False = 跨版本零复用（版本差异化）
    'watermark': None,         # None = 输出目录账号名
}
# 绝对安全边界：无论配置怎么设，版本数/时长不超过这些（防手误）
CUT_CONFIG_ABS = {
    'version_min': 1, 'version_max': 8,
    'dur_min': 60.0, 'dur_max': 1200.0,
}


class DownloadStalled(Exception):
    """下载卡顿：进程存活但长时间无新文件产出（P1 卡顿检测）。"""
    pass


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


class LinkExtractor:
    """链接提取器"""

    BAIDU_PAN_PATTERN = r'https?://pan\.baidu\.com/s/[a-zA-Z0-9_]+(?:\?pwd=[a-zA-Z0-9]+)?'

    @classmethod
    def extract(cls, message):
        """从消息中提取百度网盘链接，支持URL内pwd和正文提取码两种形式"""
        matches = re.findall(cls.BAIDU_PAN_PATTERN, message)

        # 正文里的提取码（"提取码：9999" / "提取码 9999" / "pwd:9999"）
        body_pwd = ''
        m = re.search(r'提取码[:：\s]*([a-zA-Z0-9]{4})', message) \
            or re.search(r'\bpwd[:：=]\s*([a-zA-Z0-9]{4})\b', message, re.I)
        if m:
            body_pwd = m.group(1)

        results = []
        seen = set()

        for match in matches:
            link_id = re.search(r'pan\.baidu\.com/s/([a-zA-Z0-9_]+)', match)
            if link_id:
                link_id = link_id.group(1)
                if link_id not in seen:
                    seen.add(link_id)
                    pwd = re.search(r'pwd=([a-zA-Z0-9]+)', match)
                    pwd = pwd.group(1) if pwd else body_pwd
                    results.append({'link': match, 'link_id': link_id, 'pwd': pwd})

        return results


class PipelineProcessor:
    """流水线处理器"""
    
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

    def set_cut_progress_callback(self, callback):
        """注册切片阶段回调（BatchCutManager 用于实时 Web 进度）。"""
        self._cut_progress_callback = callback if callable(callback) else None

    def _emit_cut_progress(self, stage, progress, message='', **metadata):
        """发送可审计的阶段事件；回调异常绝不能中断切片。"""
        callback = getattr(self, '_cut_progress_callback', None)
        if not callback:
            return
        try:
            callback(stage, max(0, min(100, int(progress))),
                     str(message or ''), metadata)
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
    
    def process_message(self, message, cancel_event=None):
        """处理消息"""
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
                                           cancel_event=cancel_event)
            
            # 提取链接
            links = LinkExtractor.extract(message)
            if not links:
                self.logger.log('system', 'WARN', f'未找到链接')
                return {'status': 'failed', 'error': '未找到百度网盘链接'}
            
            self.logger.log('system', 'INFO', f'找到{len(links)}个链接，开始下载...')
            
            # 下载所有链接
            download_results = self.download_batch(links, cancel_event=cancel_event)
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
                                         cancel_event=cancel_event)
            if any(r.get('cancelled') for r in cut_results):
                return {'status': 'cancelled', 'cancelled': True,
                        'error': '用户已停止任务'}
            
            successful_cuts = [r for r in cut_results if r['success']]
            failed_cuts = [r for r in cut_results if not r['success']]
            
            self.logger.log('system', 'INFO',
                           f'切片完成: 成功{len(successful_cuts)}, 失败{len(failed_cuts)}')
            
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
    
    def manual_cut(self, drama_name, resume=False, force=False, cancel_event=None,
                   config=None):
        """手动切片 - 从素材目录直接切片

        resume=True：失败续跑，只重渲染保留的 edl_config，不重新生成 EDL；
        无保留 config 时自动回退全量重切。

        config：本剧切制参数覆盖（version_count/min_dur/max_dur/reuse_material/
        watermark），None 走代码默认；调用方（UI）先合并 job_store.cut_config。
        """
        if not drama_name:
            return {'status': 'failed', 'error': '请输入剧名'}

        # 路径遍历防护：剧名不允许含路径分隔符/..
        if any(ch in drama_name for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|')) \
           or '..' in drama_name:
            return {'status': 'failed', 'error': f'剧名含非法字符: {drama_name}'}

        self.logger.log('system', 'INFO', f'手动切片：{drama_name}')
        
        # 构建素材目录路径
        src_dir = os.path.join(CONFIG['download_base_dir'], drama_name)
        
        if not os.path.exists(src_dir):
            self.logger.log('system', 'ERROR', f'素材目录不存在：{src_dir}')
            return {'status': 'failed', 'error': f'素材目录不存在：{drama_name}'}
        
        # 扫描素材文件
        files = self._scan_files_recursive(src_dir)
        raw_mp4 = [f for f in files if f.endswith('.mp4') and '.downloading' not in f]
        # 去重：网盘源若含子目录副本（/短剧素材/{剧名}/{剧名}/），递归扫描得 2N 个文件；
        # 按集号去重只保留每集一份（顶层优先），避免「找到N集」误报与双份裁剪。
        mp4_files = self._dedupe_episodes_by_number(raw_mp4)
        
        if len(mp4_files) < 2:
            self.logger.log('system', 'ERROR', f'素材文件不足：{len(mp4_files)}集（原始{len(raw_mp4)}个文件）')
            return {'status': 'failed', 'error': f'素材文件不足：{len(mp4_files)}集'}
        
        if len(raw_mp4) != len(mp4_files):
            self.logger.log('system', 'INFO',
                f'检测到嵌套副本：原始{len(raw_mp4)}个文件去重为{len(mp4_files)}集'
                f'（网盘源含子目录副本，仅保留每集一份）')
        
        self.logger.log('system', 'INFO', f'找到{len(mp4_files)}集素材，开始切片...')

        # 本剧切制参数：resolve 后一路透传到 _cut_once / _generate_edl_versions。
        cut_cfg = self._resolve_cut_config(config)
        expected_count = (cut_cfg['version_count']
                          if cut_cfg['version_count'] is not None
                          else self.determine_version_count(len(mp4_files)))
        if cut_cfg['version_count'] is not None:
            self.logger.log('system', 'INFO',
                            f'本剧切制配置：{expected_count}版，'
                            f'时长{cut_cfg["min_dur"]:.0f}-{cut_cfg["max_dur"]:.0f}s，'
                            f'素材复用{"开" if cut_cfg["reuse_material"] else "关"}')

        # 重复切片检查：只有达到目标版本数才跳过。旧逻辑看到任意一部成片
        # 就返回 skipped，导致 2/8、6/8 的批次永远无法继续完成。
        for account_dir in os.listdir(CONFIG['output_dir']) if os.path.exists(CONFIG['output_dir']) else []:
            existing = os.path.join(CONFIG['output_dir'], account_dir, drama_name)
            if os.path.isdir(existing):
                done = [f for f in os.listdir(existing) if f.endswith('_去重版.mp4')]
                if done and len(done) >= expected_count and not force:
                    self.logger.log('system', 'INFO', f'已有{len(done)}个成片，跳过重复切片：{existing}')
                    return {'status': 'skipped', 'reason': 'already_cut', 'output_dir': existing, 'existing': done}
                if done:
                    self.logger.log(
                        'system', 'INFO',
                        f'未完成任务继续处理：已有{len(done)}/{expected_count}版'
                        f'，{"显式重切" if force else "补齐完整批次"}')

        # 构建下载结果
        download_result = {
            'drama_name': drama_name,
            'link_id': drama_name,
            'downloaded_files': mp4_files,
            'src_dir': src_dir,
            'success': True,
            'error': None,
            'cut_config': cut_cfg,
        }
        
        # 切片
        result = self.cut_batch([download_result], resume=resume, force=force,
                                cancel_event=cancel_event)
        
        successful_cuts = [r for r in result if r['success']]
        skipped_cuts = [r for r in result if r.get('skipped')]
        failed_cuts = [r for r in result
                       if not r['success'] and not r.get('skipped') and not r.get('cancelled')]
        cancelled_cuts = [r for r in result if r.get('cancelled')]

        if cancelled_cuts:
            return {
                'status': 'cancelled',
                'cancelled': True,
                'drama_name': drama_name,
                'episodes': len(mp4_files),
                'cut': len(successful_cuts),
                'failed_cuts': 0,
                'output_dirs': [r['output_dir'] for r in successful_cuts if r.get('output_dir')],
                'error': '用户已停止切片',
            }
        if skipped_cuts and not successful_cuts and not failed_cuts:
            return {
                'status': 'skipped',
                'reason': 'already_cut',
                'drama_name': drama_name,
                'episodes': len(mp4_files),
                'cut': 0,
                'failed_cuts': 0,
                'output_dirs': [r['output_dir'] for r in skipped_cuts if r.get('output_dir')],
            }
        
        return {
            'status': 'success' if successful_cuts else 'failed',
            'drama_name': drama_name,
            'episodes': len(mp4_files),
            'cut': len(successful_cuts),
            'failed_cuts': len(failed_cuts),
            'output_dirs': [r['output_dir'] for r in successful_cuts],
            'error': None if successful_cuts else next(
                (r.get('error') for r in failed_cuts if r.get('error')),
                '切片失败'),
        }
    
    def download_batch(self, links, cancel_event=None):
        """并发下载所有链接"""
        results = []
        self._raise_if_cancelled(cancel_event)
        
        with ThreadPoolExecutor(max_workers=CONFIG['download']['max_workers']) as executor:
            future_to_link = {
                executor.submit(self._download_with_retry, link_info,
                                CONFIG['download']['max_retries'], cancel_event): link_info
                for link_info in links
            }
            
            for future in as_completed(future_to_link):
                link_info = future_to_link[future]
                try:
                    result = future.result()
                    results.append(result)
                except CutCancelled:
                    results.append({
                        'link': link_info['link'],
                        'link_id': link_info['link_id'],
                        'pwd': link_info['pwd'],
                        'success': False,
                        'cancelled': True,
                        'error': '用户已停止任务',
                        'downloaded_files': [],
                        'attempts': 0,
                    })
                except Exception as e:
                    results.append({
                        'link': link_info['link'],
                        'link_id': link_info['link_id'],
                        'pwd': link_info['pwd'],
                        'success': False,
                        'error': str(e),
                        'downloaded_files': [],
                        'attempts': 0
                    })
        
        return results
    
    def _download_with_retry(self, link_info, max_retries, cancel_event=None):
        """带重试的单次下载"""
        attempts = 0
        last_error = None
        last_result = None
        
        while attempts < max_retries:
            if cancel_event is not None and cancel_event.is_set():
                return {
                    'link': link_info['link'],
                    'link_id': link_info['link_id'],
                    'pwd': link_info['pwd'],
                    'success': False,
                    'cancelled': True,
                    'error': '用户已停止任务',
                    'downloaded_files': [],
                    'attempts': attempts,
                }
            attempts += 1
            
            try:
                if cancel_event is None:
                    result = self._download_once(link_info)
                else:
                    result = self._download_once(link_info, cancel_event=cancel_event)
                last_result = result
                
                if result['success']:
                    # P7/P4: 下载成功但明显残缺 → 视为可重试，续传缺失集（P4 只补缺失集）。
                    # 重试上限内继续；超限则保留结果，由切片段 P7 门控放行/拦截。
                    if result.get('episode_complete') is False:
                        total = result.get('total_episodes') or 0
                        n_mp4 = len([f for f in (result.get('downloaded_files') or [])
                                     if f.endswith('.mp4') and '.downloading' not in f])
                        min_ratio = CONFIG['download'].get('min_completeness', 0.5)
                        if total and (n_mp4 / total) < min_ratio and attempts < max_retries:
                            last_error = f'下载不完整（{n_mp4}/{total}集），续传缺失集'
                            self.logger.log(
                                result.get('drama_name') or link_info['link_id'],
                                'WARN', last_error)
                            continue
                    # archive 回填：真实下载/手动素材成功；防重跳过不覆盖历史
                    if not (result.get('error') or '').startswith('防重'):
                        try:
                            from archive import upsert_archive as _arch_up
                            _dn = result.get('drama_name') or link_info['link_id']
                            _mp4s = [f for f in result['downloaded_files'] if f.endswith('.mp4')]
                            _sz = sum(os.path.getsize(f) for f in result['downloaded_files'] if os.path.exists(f))
                            _arch_up(_dn, download_state='done',
                                     source_url=link_info.get('link'),
                                     source_passwd=link_info.get('pwd'),
                                     local_dir=result.get('target_dir'),
                                     episode_count=len(_mp4s),
                                     download_size=_sz // (1024 * 1024),
                                     download_files=len(result['downloaded_files']),
                                     download_error=None)
                        except Exception as _e:
                            self.logger.log(link_info['link_id'], 'WARN', f'archive 下载成功回填失败：{_e}')
                    return {
                        'link': link_info['link'],
                        'link_id': link_info['link_id'],
                        'pwd': link_info['pwd'],
                        'success': True,
                        'error': None,
                        'downloaded_files': result['downloaded_files'],
                        'attempts': attempts
                    }
                else:
                    last_error = result.get('error', 'Unknown error')
                    # 单次失败后立即标记 failed，让下一次重试不被旧目录防重挡住；
                    # 最终成功时会被 done 覆盖。
                    try:
                        from archive import upsert_archive as _arch_up
                        _fail_name = result.get('drama_name') or link_info['link_id']
                        _arch_up(_fail_name, download_state='failed',
                                 download_error=str(last_error)[:500])
                    except Exception as _e:
                        self.logger.log(link_info['link_id'], 'WARN',
                                        f'archive 下载重试状态回填失败：{_e}')
            
            except CutCancelled:
                return {
                    'link': link_info['link'],
                    'link_id': link_info['link_id'],
                    'pwd': link_info['pwd'],
                    'success': False,
                    'cancelled': True,
                    'error': '用户已停止任务',
                    'downloaded_files': [],
                    'attempts': attempts,
                }
            except Exception as e:
                last_error = str(e)
            
            # 重试前等待
            if attempts < max_retries:
                wait_time = 2 ** attempts
                if cancel_event is not None:
                    if cancel_event.wait(wait_time):
                        return {
                            'link': link_info['link'],
                            'link_id': link_info['link_id'],
                            'pwd': link_info['pwd'],
                            'success': False,
                            'cancelled': True,
                            'error': '用户已停止任务',
                            'downloaded_files': [],
                            'attempts': attempts,
                        }
                else:
                    time.sleep(wait_time)
        
        # archive 标记失败（最终重试失败后）
        try:
            from archive import upsert_archive as _arch_up
            _fail_name = ((last_result or {}).get('drama_name') or
                          link_info['link_id'] or
                          link_info['link'].split('/')[-1].split('?')[0])
            _arch_up(_fail_name, download_state='failed',
                     download_error=f'Failed after {max_retries} attempts: {last_error}')
        except Exception as _e:
            self.logger.log(link_info['link_id'], 'WARN', f'archive 下载失败标记失败：{_e}')
        return {
            'link': link_info['link'],
            'link_id': link_info['link_id'],
            'pwd': link_info['pwd'],
            'success': False,
            'error': f'Failed after {max_retries} attempts: {last_error}',
            'downloaded_files': [],
            'attempts': attempts
        }
    
    def _download_once(self, link_info, cancel_event=None):
        """单次下载 - 转存到/短剧素材/目录，下载后清理"""
        self._raise_if_cancelled(cancel_event)
        link = link_info['link']
        link_id = link_info['link_id']
        
        # 从链接提取ID（用于日志）
        link_dir_name = link.split('/')[-1].split('?')[0]
        # P3: 下载任务持久化 id（在下载段创建；此前未创建则为 None）
        download_job_id = None
        
        # archive 防重：本地目录已存在 或 历史 download_state=done → 不重复下载
        # （用户设计：防重复下载，集数完整性不校验）
        try:
            from archive import should_download as _arch_should
            _drama_guess = link_id or link_dir_name
            _local = os.path.join(CONFIG['download_base_dir'], _drama_guess)
            _ok, _reason = _arch_should(
                _drama_guess, _local, source_url=link)
            if not _ok:
                # 防重跳过：按 source_url 找回真实剧名/素材目录（调用方只有 link_id
                # 猜的路径，与真实目录名（剧名）不一致会指向空目录 → 切片"没有素材文件"）。
                _real_name, _real_dir = _drama_guess, _local
                try:
                    from archive import get_archive_by_source as _arch_by_src
                    _rec = _arch_by_src(link)
                    if _rec:
                        _real_name = _rec.get('drama_name') or _real_name
                        _real_dir = _rec.get('local_dir') or _real_dir
                except Exception:
                    pass
                _existing = (self._dedupe_episodes_by_number(
                                self._scan_files_recursive(_real_dir))
                            if os.path.isdir(_real_dir) else [])
                self.logger.log(_real_name, 'INFO', f'防重跳过下载：{_reason}')
                return {'success': True, 'downloaded_files': _existing,
                        'target_dir': _real_dir, 'drama_name': _real_name,
                        'error': f'防重：{_reason}', 'transferred': False}
        except Exception as _e:
            self.logger.log(link_id, 'WARN', f'archive 防重检查失败（继续下载）：{_e}')
        
        # 1. 检查用户是否已手动放置素材
        # 检查所有可能的目录名
        potential_names = [link_dir_name, link_id]
        for name in potential_names:
            manual_dir = os.path.join(CONFIG['download_base_dir'], name)
            if os.path.exists(manual_dir):
                existing_files = self._scan_files_recursive(manual_dir)
                existing_mp4 = [f for f in existing_files if f.endswith('.mp4') and '.downloading' not in f]
                if len(existing_mp4) >= CONFIG['download']['min_manual_files']:
                    try:
                        from archive import get_archive as _arch_get
                        _prev = _arch_get(name)
                    except Exception:
                        _prev = None
                    if _prev and _prev.get('download_state') == 'failed':
                        self.logger.log(
                            name, 'WARN',
                            '本地存在上次失败下载的残留，本次继续下载而不是按完成跳过')
                    else:
                        self.logger.log(name, 'INFO', f'找到手动放置的素材：{len(existing_mp4)}集')
                        # 检查集数连续性
                        is_complete, missing_eps, max_ep, continuity_msg = self._check_episode_continuity(manual_dir)
                        if is_complete:
                            self.logger.log(name, 'INFO', f'✓ {continuity_msg}')
                        else:
                            self.logger.log(name, 'WARN', f'⚠ {continuity_msg}')
                        return {
                            'success': True,
                            'downloaded_files': existing_files,
                            'target_dir': manual_dir,
                            'drama_name': name,
                            'error': None,
                            'transferred': False,
                            'episode_complete': is_complete,
                            'missing_episodes': missing_eps,
                            'total_episodes': max_ep
                        }
        
        # 2. 转存到网盘（先转存到根目录，再移动到/短剧素材/）
        # 默认密码，如果链接中有密码则使用链接中的
        pwd = link_info.get('pwd') or CONFIG['download']['default_pwd']
        self.logger.log(link_dir_name, 'INFO', f'开始转存到网盘（密码：{pwd}）...')
        transfer_cmd = [
            CONFIG['baidupcs_path'], "transfer",
            link,
            pwd
        ]
        
        try:
            process = self._run_command_cancellable(
                transfer_cmd,
                timeout=CONFIG['download']['transfer_timeout'],
                cancel_event=cancel_event,
                cwd=os.path.dirname(CONFIG['baidupcs_path']),
            )
            
            # 解析转存结果，获取实际目录名
            dir_name = link_dir_name
            
            if process.returncode == 0 and "成功" in process.stdout:
                self.logger.log(link_dir_name, 'INFO', '转存成功')
                # 提取目录名：保存了XXX到当前目录
                match = re.search(r'保存了(.+?)到当前目录', process.stdout)
                if match:
                    dir_name = match.group(1).strip()
                    # 移动到 /短剧素材/ 目录
                    move_cmd = [
                        CONFIG['baidupcs_path'], "mv",
                        dir_name,
                        f"/短剧素材/{dir_name}"
                    ]
                    move_result = subprocess.run(
                        move_cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=30,
                        cwd=os.path.dirname(CONFIG['baidupcs_path'])
                    )
                    if move_result.returncode == 0:
                        self.logger.log(dir_name, 'INFO', f'已移动到 /短剧素材/ 目录')
                    else:
                        self.logger.log(dir_name, 'WARN', f'移动失败：{move_result.stdout[:100]}')
                else:
                    self.logger.log(link_dir_name, 'WARN', f'未找到目录名：{process.stdout[:100]}')
            elif "已存在" in process.stdout or "已转存" in process.stdout or "重复" in process.stdout:
                self.logger.log(link_dir_name, 'INFO', '文件已存在于网盘，查找目录...')
                # 先从根目录查找（刚转存的目录在根目录）
                root_dirs = self._find_dir_in_cloud('/')
                # 再检查 /短剧素材/ 目录
                material_dirs = self._find_dir_in_cloud('/短剧素材')
                
                # 合并所有目录，优先使用根目录的
                all_dirs = root_dirs + material_dirs
                
                if not all_dirs:
                    self.logger.log(link_dir_name, 'WARN', '未找到网盘目录')
                    return {
                        'success': False,
                        'downloaded_files': [],
                        'target_dir': None,
                        'drama_name': link_dir_name,
                        'error': '文件已存在于网盘但未找到目录',
                        'transferred': False
                    }
                
                # 优先选择与链接相关的目录（通过剧名匹配）
                # 获取剧集素材目录中已有的剧名
                drama_name = None
                for dir_name_candidate in all_dirs:
                    # 检查本地是否已有同名目录
                    local_path = os.path.join(CONFIG['download_base_dir'], dir_name_candidate)
                    if os.path.exists(local_path):
                        drama_name = dir_name_candidate
                        break
                
                if not drama_name:
                    # 如果没有找到本地目录，使用第一个目录
                    drama_name = all_dirs[0]
                
                dir_name = drama_name
                self.logger.log(link_dir_name, 'INFO', f'找到网盘目录：{dir_name}')
            else:
                error_msg = process.stdout[:300] if process.stdout else process.stderr[:300]
                self.logger.log(link_dir_name, 'ERROR', f'转存失败：{error_msg}')
                return {
                    'success': False,
                    'downloaded_files': [],
                    'target_dir': None,
                    'drama_name': link_dir_name,
                    'error': f'转存失败：{error_msg}',
                    'transferred': False
                }
            
            # 3. 确保目录在 /短剧素材/ 下（如果在根目录，先移动）
            # 检查目录是否在根目录
            check_cmd = [CONFIG['baidupcs_path'], "ls", "/"]
            check_result = subprocess.run(
                check_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                cwd=os.path.dirname(CONFIG['baidupcs_path'])
            )
            if check_result.returncode == 0 and dir_name in check_result.stdout:
                # 目录在根目录，移动到 /短剧素材/
                move_cmd = [
                    CONFIG['baidupcs_path'], "mv",
                    dir_name,
                    f"/短剧素材/{dir_name}"
                ]
                move_result = subprocess.run(
                    move_cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                    cwd=os.path.dirname(CONFIG['baidupcs_path'])
                )
                if move_result.returncode == 0:
                    self.logger.log(dir_name, 'INFO', f'已移动到 /短剧素材/ 目录')
                    
                    # 检查是否有嵌套目录（转存分享链接时可能形成嵌套结构）
                    # 分享链接内容本身是目录时，会形成 /短剧素材/剧名/剧名/ 的结构
                    ls_cmd = [CONFIG['baidupcs_path'], "ls", f"/短剧素材/{dir_name}"]
                    ls_result = subprocess.run(
                        ls_cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=30,
                        cwd=os.path.dirname(CONFIG['baidupcs_path'])
                    )
                    if ls_result.returncode == 0:
                        # 检查是否有同名子目录
                        lines = ls_result.stdout.split('\n')
                        has_nested = False
                        for line in lines:
                            if f"{dir_name}/" in line or f"{dir_name}/ " in line:
                                has_nested = True
                                break
                        
                        if has_nested:
                            self.logger.log(dir_name, 'INFO', f'检测到嵌套目录，正在合并...')
                            # 将子目录中的文件移动到外层目录
                            mv_cmd = [
                                CONFIG['baidupcs_path'], "mv",
                                f"/短剧素材/{dir_name}/{dir_name}/*",
                                f"/短剧素材/{dir_name}/"
                            ]
                            mv_result = subprocess.run(
                                mv_cmd,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                timeout=60,
                                cwd=os.path.dirname(CONFIG['baidupcs_path'])
                            )
                            if mv_result.returncode == 0:
                                self.logger.log(dir_name, 'INFO', f'已合并嵌套目录文件')
                                # 删除空的嵌套目录
                                rm_cmd = [
                                    CONFIG['baidupcs_path'], "rm",
                                    f"/短剧素材/{dir_name}/{dir_name}",
                                    "--force"
                                ]
                                subprocess.run(
                                    rm_cmd,
                                    capture_output=True,
                                    text=True,
                                    encoding="utf-8",
                                    timeout=30,
                                    cwd=os.path.dirname(CONFIG['baidupcs_path'])
                                )
                                self.logger.log(dir_name, 'INFO', f'已删除空嵌套目录')
                            else:
                                self.logger.log(dir_name, 'WARN', f'合并嵌套目录失败：{mv_result.stdout[:100]}')
                else:
                    self.logger.log(dir_name, 'WARN', f'移动失败：{move_result.stdout[:100]}')
            
            # 4. 创建本地目标目录。实际下载先落独立暂存区，成功后再整理，
            #    避免 BaiduPCS-Go 按网盘相对路径在目标目录内重复套剧名。
            target_dir = os.path.join(CONFIG['download_base_dir'], dir_name)
            os.makedirs(target_dir, exist_ok=True)
            staging_dir = os.path.join(target_dir, '.incoming')
            os.makedirs(staging_dir, exist_ok=True)

            # P6: 清理残留 .downloading 与 <5MB 损坏文件，避免带病续传
            self._clean_partial_downloads(target_dir)
            self._clean_partial_downloads(staging_dir)
            # 上次中断时可能已有完整文件留在暂存区，先整理并纳入本地集数比对。
            self._normalize_download_tree(staging_dir, target_dir, dir_name)

            # P3: 下载开始前持久化任务（崩溃后由启动恢复续传）
            download_job_id = self._persist_download_job(
                link_info, dir_name, target_dir, staging_dir=staging_dir)

            # P4: 比对本地已下集号 vs 网盘清单，仅对缺失集发起下载
            cloud_files = self._list_cloud_files(f"/短剧素材/{dir_name}")
            cloud_eps = sorted({n for n in
                                (self.parse_episode_number(os.path.basename(x)) for x in cloud_files)
                                if n is not None})
            local_files = self._scan_files_recursive(target_dir)
            local_eps = sorted({n for n in
                                (self.parse_episode_number(os.path.basename(x)) for x in local_files)
                                if n is not None})
            missing_eps = sorted(set(cloud_eps) - set(local_eps))

            if cloud_eps and not missing_eps:
                # 网盘集全部已在本机：跳过下载，直接进入扫描/连续性校验
                self.logger.log(dir_name, 'INFO',
                                f'网盘 {len(cloud_eps)} 集已全部在本机，跳过下载')
                download_cmd = None
            elif cloud_eps and missing_eps and local_eps:
                # 部分缺失：细粒度续传，只补下载缺失集
                missing_names = []
                for cf in cloud_files:
                    n = self.parse_episode_number(os.path.basename(cf))
                    if n in missing_eps:
                        missing_names.append(cf)
                self.logger.log(dir_name, 'INFO',
                    f'续传：已下{len(local_eps)}集，缺失{len(missing_eps)}集'
                    f'（{",".join(str(e) for e in missing_eps[:12])}），仅补缺失集')
                download_cmd = ([CONFIG['baidupcs_path'], "download"] +
                                [f"/短剧素材/{dir_name}/{name}" for name in missing_names] +
                                ["--saveto", staging_dir, "--overwrite=never"])
            else:
                # 全新下载：与源目录名隔离的 staging，避免 saveto 同名嵌套。
                download_cmd = [
                    CONFIG['baidupcs_path'], "download",
                    f"/短剧素材/{dir_name}",
                    "--saveto", staging_dir,
                    "--overwrite=never"
                ]
                self.logger.log(dir_name, 'INFO', f'开始下载：{dir_name}')

            if download_cmd is not None:
                # P1: 共享进度状态（last 时间戳供卡顿探测，eps 供集号级进度）。
                # size 从 0 起：监控线程按「总字节增长」刷新 last，避免续传时
                # 目标目录集数与暂存目录文件数基数不同导致误判卡顿（P1 根因修复）。
                progress = {'last': time.monotonic(), 'count': len(local_eps),
                            'size': 0, 'eps': local_eps}
                stop_monitor = threading.Event()
                monitor_thread = threading.Thread(
                    target=self._monitor_download_progress,
                    args=(staging_dir, dir_name, stop_monitor),
                    kwargs={
                        'progress': progress,
                        'expected_eps': set(cloud_eps) if cloud_eps else None,
                        'job_id': download_job_id,
                    }
                )
                monitor_thread.start()

                try:
                    process = self._run_command_cancellable(
                        download_cmd,
                        timeout=CONFIG['download']['timeout'],
                        cancel_event=cancel_event,
                        cwd=os.path.dirname(CONFIG['baidupcs_path']),
                        progress_probe=lambda: progress['last'],
                        stall_timeout=CONFIG['download'].get('stall_timeout', 300),
                    )
                finally:
                    # 取消/超时/卡顿也必须先停监控，避免后台线程一直轮询目录。
                    stop_monitor.set()
                    monitor_thread.join(timeout=5)

            # 下载命令可能返回非 0 但已经写入部分完成文件；先整理完整文件，
            # 再由后续“云端集号 vs 本地集号”做最终判据。
            if download_cmd is not None and process.returncode != 0:
                self.logger.log(dir_name, 'WARN',
                                f'BaiduPCS-Go 退出码 {process.returncode}，先整理已完成文件')
            self._normalize_download_tree(staging_dir, target_dir, dir_name)
            self._clean_partial_downloads(target_dir)
            
            # 6. 扫描下载的文件
            downloaded_files = self._scan_files_recursive(target_dir)
            # 去重：网盘源若同时含顶层文件与 {剧名}/ 子目录副本，递归扫描得 2N 个文件；
            # 按集号去重只保留每集一份（顶层优先），避免「N集」误报与双份裁剪。
            deduped_files = self._dedupe_episodes_by_number(downloaded_files)
            
            if downloaded_files:
                mp4_files = [f for f in downloaded_files if f.endswith('.mp4')]
                if len(deduped_files) != len(mp4_files):
                    self.logger.log(dir_name, 'INFO',
                        f'检测到嵌套副本：原始{len(mp4_files)}个文件去重为{len(deduped_files)}集'
                        f'（网盘源含子目录副本，仅保留每集一份）')
                self.logger.log(dir_name, 'INFO', f'下载完成：{len(deduped_files)}集（{len(mp4_files)}个mp4）')
                for f in downloaded_files[:20]:
                    size = os.path.getsize(f) / (1024*1024)
                    self.logger.log(dir_name, 'INFO', f'  ✓ {os.path.basename(f)} ({size:.1f}MB)')

                # 完整性校验：必须有 mp4，且每个 mp4 > 5MB（短剧单集远大于此，更小的是损坏/残留）
                MIN_MP4_MB = 5
                too_small = [f for f in mp4_files if os.path.getsize(f) < MIN_MP4_MB * 1024 * 1024]
                if not mp4_files or too_small:
                    self.logger.log(dir_name, 'ERROR',
                        f'下载不完整：mp4={len(mp4_files)}个，其中{len(too_small)}个小于{MIN_MP4_MB}MB。'
                        f'保留网盘备份待重试，不删除云端文件。')
                    self._finalize_download_job(download_job_id, False, '下载不完整')
                    return {
                        'success': False,
                        'downloaded_files': [],
                        'target_dir': target_dir,
                        'drama_name': dir_name,
                        'error': f'下载不完整：mp4={len(mp4_files)}，损坏{len(too_small)}',
                        'transferred': True
                    }

                # 最终完整性以网盘清单为权威。临时标记对应的残缺 mp4 已在前面
                # 删除；只要云端还有缺失集，就不能把本地任务标记为 done 或删云端。
                local_eps_final = sorted({
                    n for n in (self.parse_episode_number(os.path.basename(f))
                                for f in deduped_files)
                    if n is not None
                })
                missing_final = (sorted(set(cloud_eps) - set(local_eps_final))
                                 if cloud_eps else [])
                if missing_final:
                    self.logger.log(
                        dir_name, 'ERROR',
                        f'下载不完整：云端{len(cloud_eps)}集，本地{len(local_eps_final)}集，'
                        f'仍缺{len(missing_final)}集 '
                        f'（{",".join(str(e) for e in missing_final[:12])}）')
                    self._finalize_download_job(download_job_id, False, '下载不完整')
                    return {
                        'success': False,
                        'downloaded_files': [],
                        'target_dir': target_dir,
                        'drama_name': dir_name,
                        'error': f'下载不完整：缺少{len(missing_final)}集',
                        'transferred': True,
                        'episode_complete': False,
                        'missing_episodes': missing_final,
                        'total_episodes': (max(cloud_eps) if cloud_eps else 0),
                    }

                layout_errors = self._validate_download_layout(target_dir)
                if layout_errors:
                    detail = '；'.join(layout_errors[:5])
                    self.logger.log(dir_name, 'ERROR',
                                    f'下载目录布局校验失败：{detail}')
                    self._finalize_download_job(
                        download_job_id, False, '下载目录布局异常')
                    return {
                        'success': False,
                        'downloaded_files': [],
                        'target_dir': target_dir,
                        'drama_name': dir_name,
                        'error': f'下载目录布局异常：{detail}',
                        'transferred': True,
                    }

                # 检查集数连续性
                is_complete, missing_eps, max_ep, continuity_msg = self._check_episode_continuity(target_dir)
                if is_complete:
                    self.logger.log(dir_name, 'INFO', f'✓ {continuity_msg}')
                else:
                    self.logger.log(dir_name, 'WARN', f'⚠ {continuity_msg}')
                
                # 7. 清理网盘文件
                try:
                    delete_cmd = [
                        CONFIG['baidupcs_path'], "delete",
                        f"/短剧素材/{dir_name}",
                        "--force"
                    ]
                    self.logger.log(dir_name, 'INFO', '开始清理网盘文件...')
                    delete_process = subprocess.run(
                        delete_cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=CONFIG['download']['delete_timeout'],
                        cwd=os.path.dirname(CONFIG['baidupcs_path'])
                    )
                    if delete_process.returncode == 0:
                        self.logger.log(dir_name, 'INFO', '网盘文件已清理')
                    else:
                        self.logger.log(dir_name, 'WARN', f'清理网盘失败：{delete_process.stdout[:100]}')
                except Exception as e:
                    self.logger.log(dir_name, 'WARN', f'清理网盘异常：{str(e)}')
                
                self._finalize_download_job(download_job_id, True, None)
                return {
                    'success': True,
                    'downloaded_files': deduped_files,
                    'target_dir': target_dir,
                    'drama_name': dir_name,
                    'error': None,
                    'transferred': True,
                    'episode_complete': is_complete,
                    'missing_episodes': missing_eps,
                    'total_episodes': max_ep
                }
            else:
                self.logger.log(dir_name, 'ERROR', '下载完成但未找到文件，将重试')
                self._finalize_download_job(download_job_id, False, '下载完成但未找到文件')
                return {
                    'success': False,
                    'downloaded_files': [],
                    'target_dir': target_dir,
                    'drama_name': dir_name,
                    'error': '下载完成但未找到文件',
                    'transferred': True
                }
            
        except CutCancelled:
            self._promote_staged_on_failure(
                locals().get('staging_dir'), locals().get('target_dir'),
                locals().get('dir_name'))
            self._finalize_download_job(download_job_id, False, '用户已停止任务')
            return {
                'success': False,
                'cancelled': True,
                'downloaded_files': [],
                'target_dir': None,
                'drama_name': link_dir_name,
                'error': '用户已停止任务',
                'transferred': False,
            }
        except DownloadStalled:
            # P1: 卡顿已由 _run_command_cancellable 终止并抛错。
            # P2: 保留网盘备份（本地残缺，续传靠 --overwrite=never 补缺失集）。
            # 失败也先把暂存区已完成的集数提升到目标目录，避免滞留。
            self._promote_staged_on_failure(
                locals().get('staging_dir'), locals().get('target_dir'),
                locals().get('dir_name'))
            self._finalize_download_job(download_job_id, False, '下载卡顿')
            return {
                'success': False,
                'downloaded_files': [],
                'target_dir': None,
                'drama_name': link_dir_name,
                'error': '下载卡顿，已终止（保留网盘备份待续传）',
                'transferred': True
            }
        except subprocess.TimeoutExpired:
            # P2: 超时不再删除网盘备份 —— 本地残缺时云端源是续传的基础。
            # 保留 /短剧素材/{dir_name}，下次重试走"已存在"分支复用。
            self._promote_staged_on_failure(
                locals().get('staging_dir'), locals().get('target_dir'),
                locals().get('dir_name'))
            self._finalize_download_job(download_job_id, False, '下载超时')
            self.logger.log(dir_name if 'dir_name' in locals() else link_dir_name,
                            'WARN', '下载超时：保留网盘备份待续传，不删除云端文件')
            return {
                'success': False,
                'downloaded_files': [],
                'target_dir': target_dir if 'target_dir' in locals() else None,
                'drama_name': link_dir_name,
                'error': '下载超时（保留网盘备份待续传）',
                'transferred': True
            }
        except Exception as e:
            # P2: 异常也不删除网盘备份 —— 保留云端源待续传。
            self._promote_staged_on_failure(
                locals().get('staging_dir'), locals().get('target_dir'),
                locals().get('dir_name'))
            self._finalize_download_job(download_job_id, False, str(e))
            self.logger.log(dir_name if 'dir_name' in locals() else link_dir_name,
                            'WARN', f'下载异常：保留网盘备份待续传（{str(e)}）')
            return {
                'success': False,
                'downloaded_files': [],
                'target_dir': target_dir if 'target_dir' in locals() else None,
                'drama_name': link_dir_name,
                'error': str(e),
                'transferred': True
            }
    
    # ── P3: 下载任务持久化（崩溃自动续传）─────────────────────────
    def _download_job_store(self):
        """惰性创建下载任务存储（与切片共用 jobs.db，job_type 区分）。"""
        if self._job_store is None:
            self._job_store = JobStore(os.path.join(CONFIG['data_dir'], 'jobs.db'))
        return self._job_store

    def _persist_download_job(self, link_info, dir_name, target_dir,
                              staging_dir=None):
        """下载开始前持久化任务，崩溃后由启动恢复续传。返回 job_id 或 None。"""
        try:
            job_id = uuid.uuid4().hex[:12]
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self._download_job_store().put({
                'id': job_id,
                'job_type': 'download',
                'status': 'downloading',
                'stage': 'downloading',
                'progress': 0,
                'params': {
                    'link': link_info.get('link'),
                    'pwd': link_info.get('pwd'),
                    'link_id': link_info.get('link_id'),
                    'drama_guess': dir_name,
                    'target_dir': target_dir,
                    'staging_dir': staging_dir,
                },
                'started_at': now,
                'heartbeat_at': now,
            })
            return job_id
        except Exception as e:
            self.logger.log('system', 'WARN',
                            f'持久化下载任务失败（不影响下载）：{e}')
            return None

    def _finalize_download_job(self, job_id, success, error=None):
        """下载结束（成功/终态失败）更新任务状态。"""
        if not job_id:
            return
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self._download_job_store().update(
                job_id, status='done' if success else 'failed',
                finished_at=now, error=error)
        except Exception as e:
            self.logger.log('system', 'WARN', f'更新下载任务状态失败：{e}')

    # 可自动续传的失败原因（网络/限速类瞬态失败；转存失败等永久错误不自动重试）
    _AUTO_RESUME_FAIL_MARKERS = ('下载卡顿', '下载超时', '下载不完整',
                                 '下载完成但未找到文件', '下载异常',
                                 'Failed after')
    _AUTO_RESUME_MAX = 3  # 同一任务自动续传上限，防永久失效源无限重试

    def find_orphaned_download_jobs(self):
        """返回待续传的下载任务：
        - 心跳过期且 status=downloading 的崩溃遗留；
        - 可恢复的 failed 任务（瞬态失败且自动续传次数未超限），
          让卡顿/超时后无需人工干预即可续传（P1 修复增强）。
        """
        try:
            store = self._download_job_store()
            jobs = store.stale_jobs(job_type='download',
                                    statuses=('downloading',),
                                    window_seconds=90)
            failed = store.list(job_type='download', statuses=('failed',))
            now = datetime.now()
            for job in failed:
                err = (job.get('error') or '')
                if not any(m in err for m in self._AUTO_RESUME_FAIL_MARKERS):
                    continue
                params = job.get('params') or {}
                if int(params.get('auto_resume', 0) or 0) >= self._AUTO_RESUME_MAX:
                    continue
                # 仅自动续传 48h 内失败的，避免复活很久前已被人工处理的旧任务。
                finished = job.get('finished_at')
                if finished:
                    try:
                        ft = datetime.strptime(finished, '%Y-%m-%d %H:%M:%S')
                        if (now - ft).total_seconds() > 48 * 3600:
                            continue
                    except ValueError:
                        pass
                jobs.append(job)
            return jobs
        except Exception as e:
            self.logger.log('system', 'WARN', f'查找中断下载任务失败：{e}')
            return []

    def bump_download_auto_resume(self, job_id):
        """记录一次自动续传（防同一任务无限重试）。"""
        try:
            store = self._download_job_store()
            job = store.get(job_id)
            if not job:
                return
            params = dict(job.get('params') or {})
            params['auto_resume'] = int(params.get('auto_resume', 0) or 0) + 1
            store.update(job_id, params=params)
        except Exception as e:
            self.logger.log('system', 'WARN', f'记录自动续传失败：{e}')

    # ── P4: 网盘文件清单（缺失集比对）────────────────────────────
    def _list_cloud_files(self, cloud_dir):
        """列出网盘目录下的文件名（供缺失集比对）。失败返回 []。"""
        try:
            ls_cmd = [CONFIG['baidupcs_path'], "ls", cloud_dir]
            ls_process = subprocess.run(
                ls_cmd, capture_output=True, text=True, encoding="utf-8",
                timeout=30, cwd=os.path.dirname(CONFIG['baidupcs_path']))
            if ls_process.returncode != 0:
                return []
            pattern = r'^\s*\d+\s+\S+\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+(.+)$'
            names = []
            for line in ls_process.stdout.split('\n'):
                m = re.match(pattern, line)
                if m:
                    names.append(m.group(1).strip().strip('/'))
            return names
        except Exception as e:
            self.logger.log('system', 'WARN', f'列出网盘文件失败：{e}')
            return []

    # ── P6: 本地下载残留清理 ────────────────────────────────────
    def _clean_partial_downloads(self, target_dir):
        """清理下载临时标记及其残缺 mp4，以及 <5MB 的损坏 mp4。

        兼容两种 BaiduPCS-Go 临时后缀：
        - ``<file>.downloading``
        - ``<file>.BaiduPCS-Go-downloading``

        后者旁边的 mp4 也是未完成文件；只删标记而保留 mp4，会让“缺失集比对”
        误判该集已经下载完成。返回实际删除的文件数。
        """
        if not os.path.isdir(target_dir):
            return 0
        removed = 0
        marker_suffixes = ('.downloading', '.baidupcs-go-downloading')
        for root, dirs, files in os.walk(target_dir):
            for f in files:
                p = os.path.join(root, f)
                try:
                    lower = f.lower()
                    marker = next((s for s in marker_suffixes
                                   if lower.endswith(s)), None)
                    if marker:
                        # BaiduPCS 的标记名通常是 <集数>.mp4.<marker>。
                        # 先删标记，再删同一目录下对应的未完成 mp4。
                        base = p[:-len(marker)]
                        os.remove(p)
                        removed += 1
                        if base.lower().endswith('.mp4') and os.path.isfile(base):
                            os.remove(base)
                            removed += 1
                    elif f.lower().endswith('.mp4') and \
                            os.path.getsize(p) < 5 * 1024 * 1024:
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
        if removed:
            self.logger.log(os.path.basename(target_dir), 'WARN',
                            f'清理下载残留：删除 {removed} 个临时/损坏文件')
        return removed

    def _find_dir_in_cloud(self, cloud_dir):
        """从网盘目录查找剧名目录"""
        # 已知的非剧目录，需要跳过
        skip_dirs = [
            'PS PR AU 软件', '百度云解压', '剪辑好的电视剧待发布', 
            '剪辑好的短剧待发布', '来自：iPhone', '绿幕直播预设场景素材',
            '曼剧基础教程', '视频号', '短剧素材', '短剧1', '短剧2牛志强专用',
            '短剧解说 九姐', '短剧解说 娜姐', '电视剧解说素材专用'
        ]
        
        try:
            ls_cmd = [CONFIG['baidupcs_path'], "ls", cloud_dir]
            ls_process = subprocess.run(
                ls_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                cwd=os.path.dirname(CONFIG['baidupcs_path'])
            )
            
            if ls_process.returncode == 0:
                lines = ls_process.stdout.split('\n')
                pattern = r'^\s*\d+\s+\S+\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+(.+)$'
                
                dirs = []
                for line in lines:
                    match = re.match(pattern, line)
                    if match:
                        dir_candidate = match.group(1).strip().rstrip('/')
                        if dir_candidate not in skip_dirs:
                            dirs.append(dir_candidate)
                
                return dirs
            
            return []
        except Exception as e:
            self.logger.log('system', 'WARN', f'查找网盘目录失败：{str(e)}')
            return []
    
    def _monitor_download_progress(self, target_dir, dir_name, stop_event,
                                   progress=None, expected_eps=None, job_id=None):
        """监控下载进度，记录已下载的文件（P1/P3/P5 增强）。

        progress: 共享 dict {last, count, eps}；进度变化时刷新 last 时间戳
                  （P1 卡顿探测用）与 eps 集号列表（P5 显示）。
        expected_eps: 云端预期集号集合（P5 进度分母）。
        job_id: 下载任务 id；进度变化时刷新心跳（P3 崩溃恢复）。
        """
        last_count = 0
        last_logged = 0
        try:
            while not stop_event.is_set():
                try:
                    files = self._scan_files_recursive(target_dir)
                    mp4_files = [f for f in files if f.endswith('.mp4') and '.downloading' not in f]
                    total_size = sum(os.path.getsize(f) for f in mp4_files)
                    eps = []
                    for f in mp4_files:
                        n = self.parse_episode_number(os.path.basename(f))
                        if n is not None:
                            eps.append(n)
                    eps = sorted(set(eps))

                    # P1: 共享进度状态（last 时间戳、size、count、eps）
                    # 用「总字节增长」判定下载活性而非「文件数增长」：
                    # 续传时目标目录已含旧集数，暂存目录文件数基数不同，按文件数
                    # 会永不刷新 last → 300s 后误判卡顿（P1 根因修复）；单文件
                    # 下载慢时字节持续增长也能正确保持活性。
                    if progress is not None:
                        if total_size > progress.get('size', 0):
                            progress['last'] = time.monotonic()
                            progress['size'] = total_size
                        if len(mp4_files) > progress.get('count', 0):
                            progress['count'] = len(mp4_files)
                        progress['eps'] = eps

                    # P5: 仅在集数增加时打日志，集号级进度
                    if len(mp4_files) > last_logged:
                        if expected_eps:
                            total = len(expected_eps)
                            pct = (len(eps) / total * 100) if total else 0
                            eps_str = ','.join(str(e) for e in eps[:12])
                            if len(eps) > 12:
                                eps_str += f'...共{len(eps)}集'
                            self.logger.log(dir_name, 'INFO',
                                f'下载中：已下{len(eps)}/{total}集 [{eps_str}] · '
                                f'{total_size/1024/1024:.1f}MB · {pct:.0f}%')
                        else:
                            # expected_eps 缺失（云端清单为空）时，仍用独特集号而非原始文件数，
                            # 避免嵌套副本把计数撑到 2N（「51集/72集」误报根因）。
                            n_disp = len(eps) if eps else len(mp4_files)
                            self.logger.log(dir_name, 'INFO',
                                f'下载中：已下{n_disp}集 ({total_size/1024/1024:.1f}MB)')
                        last_logged = len(mp4_files)

                    # P3: 下载任务心跳（崩溃后判定孤儿用）
                    if job_id:
                        try:
                            self._download_job_store().touch_heartbeat(job_id)
                        except Exception:
                            pass

                    stop_event.wait(timeout=10)
                except Exception as e:
                    self.logger.log(dir_name, 'WARN', f'监控异常：{str(e)}')
                    stop_event.wait(timeout=10)
        finally:
            self.logger.log(dir_name, 'INFO', f'监控线程结束')
    
    def _flatten_local_dir(self, target_dir, dir_name):
        """仅折叠同名包装目录，保留 transcribe/covers 等业务子目录。

        保留该方法用于修复“下载代码直接写最终目录”的旧调用现场；新下载路径
        使用 _normalize_download_tree，在独立暂存目录中整理后一次性落盘。
        """
        flattened = False
        try:
            for entry in os.listdir(target_dir):
                sub = os.path.join(target_dir, entry)
                if entry != dir_name or not os.path.isdir(sub):
                    continue
                while os.path.isdir(sub) and os.listdir(sub) == [dir_name]:
                    inner = os.path.join(sub, dir_name)
                    if not os.path.isdir(inner):
                        break
                    for item in os.listdir(inner):
                        src = os.path.join(inner, item)
                        dst = os.path.join(sub, item)
                        if os.path.exists(dst):
                            continue
                        shutil.move(src, dst)
                        flattened = True
                    try:
                        os.rmdir(inner)
                    except OSError:
                        break
                # 内容上移到 target_dir；同名冲突的副本留给集号去重/隔离。
                for item in os.listdir(sub):
                    src = os.path.join(sub, item)
                    dst = os.path.join(target_dir, item)
                    if os.path.exists(dst):
                        continue
                    shutil.move(src, dst)
                    flattened = True
                try:
                    os.rmdir(sub)
                except OSError:
                    pass
        except OSError as e:
            self.logger.log(dir_name, 'WARN', f'P1 展平异常（可忽略）：{e}')
        return flattened

    @staticmethod
    def _download_marker_suffix(filename):
        """返回 BaiduPCS 临时文件命中的后缀；不是临时文件返回 None。"""
        lower = (filename or '').lower()
        for suffix in ('.baidupcs-go-downloading', '.downloading'):
            if lower.endswith(suffix):
                return suffix
        return None

    def _is_partial_download_file(self, path, filenames=None):
        """判断路径是否本身为下载临时文件，或旁边存在 BaiduPCS 临时标记。"""
        if self._download_marker_suffix(os.path.basename(path)):
            return True
        folder = os.path.dirname(path)
        base = os.path.basename(path)
        if filenames is None:
            try:
                filenames = os.listdir(folder)
            except OSError:
                filenames = []
        lowered = {str(f).lower() for f in filenames}
        return any((base.lower() + suffix) in lowered
                   for suffix in ('.baidupcs-go-downloading', '.downloading'))

    def _quarantine_download_duplicate(self, source, staging_dir, batch_dir):
        """把暂存区中的重复内容移到数据区隔离目录，保留可追溯性。"""
        rel = os.path.relpath(source, staging_dir)
        if rel.startswith('..'):
            rel = os.path.basename(source)
        dst = os.path.join(batch_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(source, dst)
        return dst

    def _normalize_download_tree(self, staging_dir, target_dir, dir_name):
        """把暂存区中的完整文件整理到最终剧目录的直接层。

        下载阶段不再直接写目标目录，所以无论网盘分享保留几层目录，这里都能把
        实际文件一次整理到位。同一集号/同名文件已存在时不覆盖，重复副本移入
        data/download_quarantine 供追溯。返回 (moved, quarantined)。
        """
        if not os.path.isdir(staging_dir):
            return 0, 0
        os.makedirs(target_dir, exist_ok=True)
        self._clean_partial_downloads(staging_dir)

        existing = [f for f in self._scan_files_recursive(target_dir)
                    if os.path.dirname(os.path.abspath(f)) ==
                    os.path.abspath(target_dir)]
        existing_names = {os.path.basename(f).lower() for f in existing}
        existing_eps = {}
        for f in existing:
            n = self.parse_episode_number(os.path.basename(f))
            if n is not None:
                existing_eps[n] = f

        source_files = []
        for root, dirs, files in os.walk(staging_dir):
            dirs[:] = [d for d in dirs if d.lower() != '.incoming']
            for f in files:
                p = os.path.join(root, f)
                if not self._is_partial_download_file(p, files):
                    source_files.append(p)
        source_files.sort(key=lambda p: (os.path.relpath(p, staging_dir).count(os.sep),
                                         os.path.basename(p).lower()))

        moved = 0
        quarantined = 0
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^0-9A-Za-z_\-\u4e00-\u9fff]+', '_', dir_name)
        quarantine_batch = os.path.join(
            CONFIG.get('data_dir', os.getcwd()), 'download_quarantine',
            f'{safe_name}_{stamp}_{uuid.uuid4().hex[:6]}')
        for src in source_files:
            name = os.path.basename(src)
            lower_name = name.lower()
            ep = (self.parse_episode_number(name)
                  if lower_name.endswith('.mp4') else None)
            duplicate = lower_name in existing_names or (
                ep is not None and ep in existing_eps)
            dst = os.path.join(target_dir, name)
            if duplicate or os.path.exists(dst):
                dst = self._quarantine_download_duplicate(
                    src, staging_dir, quarantine_batch)
                quarantined += 1
                self.logger.log(dir_name, 'WARN',
                                f'下载重复文件已隔离：{name} → {dst}')
                continue
            shutil.move(src, dst)
            existing_names.add(lower_name)
            if ep is not None:
                existing_eps[ep] = dst
            moved += 1

        # 清理暂存区空目录；保留非空目录供下一次续传。
        for root, dirs, files in os.walk(staging_dir, topdown=False):
            if root == staging_dir:
                continue
            try:
                os.rmdir(root)
            except OSError:
                pass
        try:
            os.rmdir(staging_dir)
        except OSError:
            pass
        if moved or quarantined:
            self.logger.log(
                dir_name, 'INFO',
                f'下载整理完成：上移{moved}个文件，隔离重复{quarantined}个')
        return moved, quarantined

    def _promote_staged_on_failure(self, staging_dir, target_dir, dir_name):
        """下载失败/卡顿/超时/取消路径：把暂存区已完成的文件提升到目标目录。

        原实现只在整体成功时整理暂存区，失败路径会把已下完的集数滞留
        .incoming（如娇妻婆婆为我挡风雨 6,14,15,18,42 卡顿后滞留）。本方法在
        各异常处理分支调用，确保失败也保留进度，下次续传仅下载真正缺失的集数。
        """
        try:
            if (staging_dir and target_dir and dir_name
                    and os.path.isdir(staging_dir)):
                self._normalize_download_tree(staging_dir, target_dir, dir_name)
        except Exception as e:
            self.logger.log(dir_name or 'system', 'WARN',
                            f'失败路径提升暂存文件异常：{e}')

    def _validate_download_layout(self, target_dir):
        """校验增量下载的最终布局；返回问题列表，空列表表示通过。"""
        errors = []
        target_abs = os.path.abspath(target_dir)
        for root, dirs, files in os.walk(target_dir):
            for f in files:
                if self._download_marker_suffix(f):
                    errors.append(
                        f'存在下载临时标记：{os.path.relpath(os.path.join(root, f), target_dir)}')
                if (f.lower().endswith('.mp4')
                        and os.path.abspath(root) != target_abs):
                    errors.append(
                        f'mp4仍在子目录：{os.path.relpath(os.path.join(root, f), target_dir)}')
            if os.path.abspath(root) == target_abs:
                incoming = os.path.join(root, '.incoming')
                if os.path.isdir(incoming):
                    try:
                        if os.listdir(incoming):
                            errors.append('暂存区 .incoming 仍有未整理文件')
                    except OSError:
                        pass
        return errors

    def _scan_files_recursive(self, directory):
        """递归扫描完整 mp4；排除暂存区、隔离区和 BaiduPCS 半成品。"""
        files = []
        try:
            for root, dirs, filenames in os.walk(directory):
                dirs[:] = [d for d in dirs
                           if d.lower() not in ('.incoming', 'download_quarantine')]
                marker_names = {f.lower() for f in filenames}
                for f in filenames:
                    if not f.lower().endswith('.mp4'):
                        continue
                    if any((f.lower() + suffix) in marker_names
                           for suffix in ('.baidupcs-go-downloading', '.downloading')):
                        continue
                    files.append(os.path.join(root, f))
        except Exception as e:
            self.logger.log('system', 'WARN', f'扫描文件失败：{str(e)}')
        return files
    
    def _dedupe_episodes_by_number(self, file_list):
        """按集号去重同一剧的多份副本（顶层/嵌套），保留每集最浅路径一份。

        根因：网盘源 /短剧素材/{剧名}/ 若同时含顶层文件与 {剧名}/ 子目录
        （分享链接内容本身是目录），下载会拉下两份全集 → 递归扫描得 2N 个文件，
        旧代码用 len(mp4_files) 计数导致「下载中：51集/72集」误报，且
        downloaded_files 全量传给 cut_batch 造成双份裁剪。

        本方法对每集号只保留路径最浅（最接近顶层）的一份；集号解析失败的
        文件原样保留（不丢数据）。返回按集号排序的去重文件列表。
        """
        by_ep = {}
        unparsed = []
        for f in file_list:
            if not f.lower().endswith('.mp4') or '.downloading' in f:
                continue
            n = self.parse_episode_number(os.path.basename(f))
            if n is None:
                unparsed.append(f)
                continue
            cur = by_ep.get(n)
            # 保留路径更浅（更少分隔符）的副本：顶层 第N集.mp4 优先于 嵌套 剧名-第N集.mp4
            if cur is None or f.count(os.sep) < cur.count(os.sep):
                by_ep[n] = f
        return [by_ep[n] for n in sorted(by_ep)] + unparsed

    def _check_episode_continuity(self, src_dir):
        """检查集数是否连续完整
        返回：(is_complete, missing_eps, max_ep, message)
        """
        try:
            episodes = []
            # 使用递归扫描，处理嵌套目录结构
            for f in self._scan_files_recursive(src_dir):
                if f.endswith('.mp4') and not f.endswith('.downloading'):
                    fname = os.path.basename(f)
                    ep_num = self.parse_episode_number(fname)
                    if ep_num is not None:
                        episodes.append(ep_num)
            
            if not episodes:
                return False, [], 0, '没有找到有效集数文件'
            
            episodes.sort()
            min_ep = episodes[0]
            max_ep = episodes[-1]
            
            # 检查是否从1开始
            if min_ep > 1:
                return False, list(range(1, min_ep)), max_ep, f'缺少第1-{min_ep-1}集'
            
            # 检查是否有中间缺失
            expected = set(range(1, max_ep + 1))
            actual = set(episodes)
            missing = sorted(expected - actual)
            
            if missing:
                missing_str = ','.join([str(m) for m in missing[:10]])
                if len(missing) > 10:
                    missing_str += f'等{len(missing)}集'
                return False, missing, max_ep, f'缺少{len(missing)}集：{missing_str}'
            
            return True, [], max_ep, f'集数完整：1-{max_ep}集'
        
        except Exception as e:
            self.logger.log('system', 'WARN', f'检查集数连续性失败：{str(e)}')
            return True, [], 0, f'检查失败：{str(e)}'
    
    def _valid_delivered_filenames(self, out_dir):
        """返回 out_dir 里通过质量校验的 _去重版.mp4 文件名集合（供清理保留）。

        用于断点续跑：真实成品（已合格）不当作"假成品"删除，崩溃/停止后
        _re_render_from_configs 的逐版跳过可复用，不必重渲。返回 None 表示
        扫描失败（调用方回退全清，保守）。只有整集合格成品才可能触发
        should_process 的"已有成品"闸门，而那只在整剧确实完成时成立。
        """
        try:
            kept = []
            for f in os.listdir(out_dir):
                if f.endswith('_去重版.mp4'):
                    q = self._validate_delivery_media(os.path.join(out_dir, f))
                    if q.get('ok'):
                        kept.append(f)
            return set(kept) if kept else None
        except Exception as e:
            self.logger.log('system', 'WARN', f'成品扫描失败（全清）：{e}')
            return None

    def _clean_partial_outputs(self, out_dir, keep_filenames=None, include_configs=True):
        """清理 out_dir 里的渲染产物（P1-A2/A3）。

        根因：out_dir 没有 run 边界，完成/去重判断全靠数文件名反推——
        历史失败 run 留下的未后处理 mp4（render_generic 交付名与成品同名）
        会被 should_process 第三层当成"已有成品"永久跳过重试，被 _{n}_
        子串收集混进下一次交付，被去重闸门当成品。

        删除对象：.mp4 / .mp4.tmp / edl_config_v*.json。
        - keep_filenames：交付完成路径传入已交付文件名集合，只删未交付的；
        - 失败/重切路径传 None 全清（out_dir 是单剧目录，能走到清理说明本剧
          未被标记完成，里面的 mp4 只可能来自历史失败 run 或本次 run）；
        - include_configs=False 用于交付完成路径：本 run 的 edl_config 与
          交付一致，保留可当简易 manifest 排查用。

        Returns: 删除的文件数（仅用于日志）。
        """
        removed = 0
        try:
            for f in os.listdir(out_dir):
                p = os.path.join(out_dir, f)
                if not os.path.isfile(p):
                    continue
                if keep_filenames is not None and f in keep_filenames:
                    continue
                if '_REJECTED' in f:
                    continue  # P5: 拒收成片保留供复核/补齐，不删（名如 *_REJECTED.mp4）
                is_mp4 = f.endswith('.mp4') or f.endswith('.mp4.tmp')
                is_cfg = include_configs and f.startswith('edl_config_v') and f.endswith('.json')
                if is_mp4 or is_cfg:
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass
        except OSError as e:
            self.logger.log('system', 'WARN', f'清理渲染残留失败：{str(e)}')
        return removed

    def _count_delivered_versions(self, result):
        """统计交付版本数（output_dir 内 _去重版.mp4 数）。"""
        od = result.get('output_dir')
        if od and os.path.isdir(od):
            try:
                return len([f for f in os.listdir(od) if f.endswith('_去重版.mp4')])
            except OSError:
                pass
        return 0

    def cut_batch(self, downloads, resume=False, force=False, cancel_event=None):
        """批量切片"""
        results = []
        failed = []
        # 剧级档案：切片状态追踪（延迟 import 避免循环，archive 独立不依赖 processor）
        from archive import upsert_archive, init_database as _arc_init
        try:
            _arc_init()
        except Exception:
            pass
        
        # 第一轮：处理所有
        for d in downloads:
            drama_name = d.get('drama_name') or d.get('link_id', '')
            if cancel_event is not None and cancel_event.is_set():
                results.append({
                    'name': drama_name,
                    'success': False,
                    'cancelled': True,
                    'output_dir': None,
                    'error': '用户已停止切片',
                })
                break
            if drama_name:
                try:
                    upsert_archive(drama_name, slice_state='cutting')
                except Exception:
                    pass
            result = self._cut_once(d, resume=resume, force=force,
                                    cancel_event=cancel_event)
            result['retried'] = False
            if drama_name:
                try:
                    if result.get('cancelled'):
                        # 停止不是失败；清掉“切片中”并在下次启动时重新处理。
                        upsert_archive(drama_name, slice_state='none',
                                       slice_error='用户已停止切片')
                    elif result['success']:
                        upsert_archive(drama_name, slice_state='done',
                                       slice_version_count=self._count_delivered_versions(result),
                                       sliced_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    elif '跳过' not in (result.get('error') or ''):
                        upsert_archive(drama_name, slice_state='failed',
                                       slice_error=(result.get('error') or '')[:500])
                    # 跳过（已切过）不覆盖状态
                except Exception:
                    pass
            
            if result.get('cancelled'):
                results.append(result)
                break
            if result['success'] or result.get('skipped'):
                results.append(result)
            else:
                error_msg = result.get("error", "未知错误") or "未知错误"
                self.logger.log('system', 'WARN', f'切片失败，将重试：{result.get("name", "未知")} - {error_msg}')
                failed.append({'download': d, 'error': error_msg})
        
        # 第二轮：重试失败的
        for item in failed:
            d = item['download']
            drama_name = d.get('drama_name') or d.get('link_id', '')
            if cancel_event is not None and cancel_event.is_set():
                results.append({
                    'name': drama_name,
                    'success': False,
                    'cancelled': True,
                    'output_dir': None,
                    'error': '用户已停止切片',
                })
                break
            self.logger.log('system', 'INFO', f'重试切片：{d.get("link_id", "未知")}')
            result = self._cut_once(d, resume=resume, force=force,
                                    cancel_event=cancel_event)
            result['retried'] = True
            if drama_name:
                try:
                    if result.get('cancelled'):
                        upsert_archive(drama_name, slice_state='none',
                                       slice_error='用户已停止切片')
                    elif result['success']:
                        upsert_archive(drama_name, slice_state='done',
                                       slice_version_count=self._count_delivered_versions(result),
                                       sliced_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    elif '跳过' not in (result.get('error') or ''):
                        upsert_archive(drama_name, slice_state='failed',
                                       slice_error=(result.get('error') or '')[:500])
                except Exception:
                    pass
            if result.get('cancelled'):
                results.append(result)
                break
            if result['success'] or result.get('skipped'):
                results.append(result)
            else:
                error_msg = result.get("error", "未知错误") or "未知错误"
                self.logger.log('system', 'ERROR', f'重试切片失败：{d.get("link_id", "未知")} - {error_msg}')
                results.append({
                    'name': d['link_id'],
                    'success': False,
                    'output_dir': None,
                    'error': f'Retry failed: {error_msg}',
                    'retried': True
                })
        
        return results
    
    def _cut_once(self, download_result, resume=False, force=False,
                  cancel_event=None):
        """单次切片"""
        # 从下载目录提取实际剧名
        drama_name = download_result.get('drama_name') or download_result.get('link_id', '')
        # 本剧切制参数（manual_cut 已 resolve；下载批路径无 config 走默认）
        cut_cfg = (download_result.get('cut_config')
                   or self._resolve_cut_config(None))
        self._emit_cut_progress('material_scan', 2, f'扫描素材：{drama_name}')
        if cancel_event is not None and cancel_event.is_set():
            return {
                'name': drama_name,
                'success': False,
                'cancelled': True,
                'output_dir': None,
                'error': '用户已停止切片',
            }
        src_dir = None
        
        if download_result.get('downloaded_files'):
            # 从第一个文件路径获取目录 - 这就是素材目录
            src_dir = os.path.dirname(download_result['downloaded_files'][0])
            # 如果src_dir是download_base_dir下的直接子目录，使用它作为剧名
            if os.path.basename(src_dir) and os.path.basename(src_dir) != os.path.basename(CONFIG['download_base_dir']):
                drama_name = os.path.basename(src_dir)
        elif download_result.get('src_dir'):
            # 手动指定素材目录
            src_dir = download_result['src_dir']
            if os.path.basename(src_dir):
                drama_name = os.path.basename(src_dir)
        
        if not src_dir:
            self.logger.log(drama_name, 'ERROR', '切片失败：没有素材文件')
            return {
                'name': drama_name,
                'success': False,
                'output_dir': None,
                'error': '没有素材文件'
            }
        
        submit_order = self.get_submit_order()
        account = self.assign_account(drama_name, submit_order)
        # 输出目录按水印分类：output/福桃说剧/ 或 output/小桃追剧中/
        output_subdir = self.get_output_subdir(
            drama_name, submit_order, account=account)
        out_dir = os.path.join(CONFIG['output_dir'], output_subdir, drama_name)
        
        # 去重检查：按计划版本数判断是否真的完成，而非“有任意成片就跳过”。
        downloaded = download_result.get('downloaded_files') or []
        expected_count = (cut_cfg['version_count']
                          if cut_cfg['version_count'] is not None
                          else self.determine_version_count(
                              len(self._dedupe_episodes_by_number(downloaded))))
        should, reason = self.should_process(
            drama_name, src_dir, out_dir, expected_count=expected_count,
            force=force)
        if not should:
            self.logger.log(drama_name, 'WARN', f'切片跳过：{reason}')
            self._update_state('idle', '', 0, f'跳过：{reason}')
            return {
                'name': drama_name,
                'success': False,
                'output_dir': None,
                'error': f'跳过：{reason}',
                'skipped': True,
            }
        
        # 获取文件锁
        lock_path = os.path.join(out_dir, '.processing.lock')
        os.makedirs(out_dir, exist_ok=True)
        lock = self._acquire_lock(lock_path)
        
        if not lock:
            self.logger.log(drama_name, 'ERROR', '切片失败：正在被其他进程处理')
            return {
                'name': drama_name,
                'success': False,
                'output_dir': None,
                'error': '正在被其他进程处理'
            }
        
        try:
            # 更新状态
            self._update_state('processing', drama_name, 0, '开始切片')
            self._raise_if_cancelled(cancel_event)

            # P1-A2/A3：run 开始先清掉历史失败 run 的残留产物。
            # 能走到这里说明本剧未被标记完成（state/已切完/去重闸门都已过），
            # out_dir 里的旧 mp4 只可能是未过后处理的"假成品"：不清则
            # should_process 第三层会把重试误判为"已有成品"，收集按 _{n}_
            # 子串还会把旧件混进本次交付。
            # include_configs=False：保留历史 edl_config 作为续跑现场，
            # 若本剧之前失败过，_re_render_from_configs 可直接续跑。
            # 断点续跑增强：保留已通过质量校验的 _去重版.mp4（真实成品而非
            # 假成品），崩溃后 _re_render_from_configs 的逐版跳过可复用它们，
            # 不必重渲。should_process 已确认本剧未完成，故保留的只是部分成品，
            # 不会误判为整剧完成。
            resume_keep = self._valid_delivered_filenames(out_dir)
            if resume_keep:
                self.logger.log(
                    drama_name, 'INFO',
                    f'run开始：保留{len(resume_keep)}个已合格成品待续跑复用')
            swept = self._clean_partial_outputs(out_dir, include_configs=False,
                                                keep_filenames=resume_keep)
            if swept:
                self.logger.log(drama_name, 'WARN', f'run开始清理：删除{swept}个历史残留渲染产物（保留EDL配置）')
            
            # 收集素材 - 使用新的list_episodes方法
            episodes = self.list_episodes(src_dir)
            all_eps = [ep[1] for ep in episodes]  # 只取文件路径
            
            ep_count = len(all_eps)
            version_count = (cut_cfg['version_count']
                             if cut_cfg['version_count'] is not None
                             else self.determine_version_count(ep_count))
            self._emit_cut_progress('material_scan', 8,
                                    f'识别 {ep_count} 集素材', episodes=ep_count)
            
            self.logger.log(drama_name, 'INFO', f'总集数：{ep_count}，计划生成{version_count}个版本')
            
            # ASR转写（失败续跑时跳过：直接用保留的 edl_config 重渲染，不重新生成）
            resume = bool(resume)
            # 断点续跑：自动识别有效现场（保留 config + 清单源签名匹配），
            # 即便传入 resume=False 也接管为续跑，避免崩溃后全量重切白干。
            # 无清单/源签名不匹配的旧现场 → _resume_scene_valid=False → 全量。
            if not resume and self._resume_scene_valid(out_dir, src_dir):
                self.logger.log(
                    drama_name, 'INFO',
                    '检测到有效续跑现场（EDL配置+素材签名匹配），自动续跑')
                resume = True
            if resume:
                ai_videos = []
                self._update_state('processing', drama_name, 30, '续跑重渲染中')
                self._emit_cut_progress('render', 45,
                                        '读取保留的 EDL，准备续跑重渲染')
                self._raise_if_cancelled(cancel_event)
                edl_videos = self._re_render_from_configs(
                    drama_name, out_dir, cancel_event=cancel_event)
                if not edl_videos:
                    # 无保留 config 或全部渲染失败 → 回退全量重切（重新生成 EDL）
                    self.logger.log(drama_name, 'WARN',
                                    '续跑无可用版本，回退全量重切（重新生成EDL）')
                    resume = False
            if not resume:
                # 全量路径：ASR + 重新生成 EDL
                self._update_state('processing', drama_name, 10, 'ASR转写中')
                self._emit_cut_progress('asr', 10,
                                        f'开始逐集 ASR（共 {ep_count} 集）')
                self._raise_if_cancelled(cancel_event)
                transcript_path = self._run_asr(src_dir, drama_name,
                                                cancel_event=cancel_event)
                self._raise_if_cancelled(cancel_event)
                self._emit_cut_progress('asr', 24, 'ASR 转写完成')
                short_titles, long_titles = self._extract_quotes(transcript_path, 10)
                
                # 全部素材交给 EDL 切片。
                # AI 高光智剪已移除：它依赖火山引擎 mediakit-cli 的认证，项目未配置该 key，
                # 每次运行都会白等一次 doctor + 最长 20 分钟任务轮询然后空转。
                # 版本差异化由导演叙事结构 + 钩子主题 + ASR 高光台词保证。
                edl_eps = all_eps
                
                self.logger.log(drama_name, 'INFO',
                               f'EDL素材：{len(edl_eps)}集（全量）')
                # 方案B：素材容量决定版本数（非重叠段总数 // 每版段数，零复用前提）。
                # 覆盖 determine_version_count 的集数判定（>=30→8），避免版本数超过
                # 素材容量导致被迫复用同一素材片段。
                if CONFIG['edl'].get('non_overlap'):
                    try:
                        self._emit_cut_progress(
                            'version_budget', 27,
                            '按独立素材容量计算安全版本数')
                        version_count = self._capacity_version_count(
                            edl_eps, drama_name, config=cut_cfg)
                    except Exception as e:
                        self.logger.log(drama_name, 'WARN',
                                        f'方案B容量计算异常，用集数判定：{e}')
                        version_count = (cut_cfg['version_count']
                                         if cut_cfg['version_count'] is not None
                                         else self.determine_version_count(ep_count))
                
                # 生产路径统一走导演叙事（钩子→前因→发展→结局）。
                # 只有高光/转写全缺时，_generate_reverse_segments 才回落随机兜底，
                # 避免正式成片随机跨集跳切、剧情无法理解。
                use_reverse = True
                cut_method = '导演叙事结构'
                self.logger.log(drama_name, 'INFO', f'使用切法：{cut_method}')
                
                # EDL手动切片（AI 高光已移除，ai_videos 恒为空，保留变量以兼容报告/状态字段）
                ai_videos = []
                edl_count = version_count
                self._update_state('processing', drama_name, 30, 'EDL切片中')
                self._emit_cut_progress(
                    'version_budget', 42,
                    f'计划生成 {version_count} 版',
                    version_count=version_count)
                self._raise_if_cancelled(cancel_event)
                edl_videos = self._generate_edl_versions(
                    drama_name, src_dir, edl_eps, edl_count, out_dir,
                    use_reverse, short_titles, long_titles,
                    cancel_event=cancel_event, config=cut_cfg)
            else:
                use_reverse = False
                cut_method = '续跑重渲染'
                self.logger.log(drama_name, 'INFO',
                                f'续跑重渲染完成：{len(edl_videos)}个版本（未重新生成EDL）')
            
            # 合并版本
            self._raise_if_cancelled(cancel_event)
            all_videos = ai_videos + edl_videos
            
            # 筛选最佳版本（时长窗口用本剧 config：[min_dur, max_dur]）
            best_videos = self._select_best_videos(all_videos, cut_cfg)

            # 内容级去重（R9）：版本间感知哈希相似度 → 剔除高重复版本，
            # 规避"成片重复度很高"被平台判重。剔除只在交付集内进行，
            # 被剔除文件保留在 out_dir 供人工核查。
            self._emit_cut_progress('content_dedup', 90,
                                    '计算成片指纹并比较版本重合')
            best_videos, dedup_report = self._dedup_versions_by_content(
                best_videos, drama_name, out_dir, src_dir)

            # 后处理
            self._update_state('processing', drama_name, 80, '后处理中')
            processed_videos = []
            for i, video in enumerate(best_videos):
                self._raise_if_cancelled(cancel_event)
                pct = 92 + int(4 * (i / max(1, len(best_videos))))
                self._emit_cut_progress('post_process', pct,
                                        f'后处理 V{i + 1}/{len(best_videos)}',
                                        version=i + 1)
                ok = self._post_process(video['path'], video['final_path'],
                                        src_dir=src_dir, drama_name=drama_name)
                if ok and os.path.exists(video['final_path']):
                    processed_videos.append(video)
                else:
                    self.logger.log(drama_name, 'WARN',
                        f'V{i+1} 后处理失败，已跳过（不裸发）')
            best_videos = processed_videos
            self._raise_if_cancelled(cancel_event)
            
            # 清理测试版本
            self._cleanup_test_versions(out_dir)

            # P1-A1：0 个可交付成片时绝不能当作完成。
            # 此前的行为是照常写报告、add_completed({'total':0})、写已切完.txt，
            # 三层闸门（state.completed / 已切完.txt / 567 行去重检查）会同时锁死，
            # 这部剧永远无法重切，只能手工删文件。0 条时必须走 1356 行失败分支。
            if not best_videos:
                # 顺手清掉本次残留的非交付 mp4：后处理全部失败时 out_dir 里
                # 不可能有 *_去重版.mp4（有也会被 567 行去重闸门提前拦下，
                # 走不到这里），删除是安全的；否则重切时 1637 行按 _{v_idx}_
                # 收集输出会捞到这些旧半成品。
                removed = 0
                try:
                    for f in os.listdir(out_dir):
                        if f.endswith('.mp4') and '_去重版' not in f and re.search(r'V\d+', f):
                            os.remove(os.path.join(out_dir, f))
                            removed += 1
                except OSError as e:
                    self.logger.log('system', 'WARN', f'清理失败残留 mp4 出错：{str(e)}')
                if removed:
                    self.logger.log(drama_name, 'WARN', f'已清理{removed}个失败残留的未后处理文件')
                raise RuntimeError(
                    f'无可交付成片：{version_count}个版本全部渲染/后处理失败，'
                    f'已保留失败记录可重切')

            # 写交付报告
            self._raise_if_cancelled(cancel_event)
            self._emit_cut_progress('report', 97, '生成交付与质量报告')
            self._write_report(out_dir, drama_name, best_videos, cut_method,
                               dedup_report=dedup_report)
            self._write_content_risk_audit(out_dir, drama_name)

            # A7 QC 报告：blackdetect/响度/词中切断（审计非门禁）
            self._raise_if_cancelled(cancel_event)
            self._generate_qc_report(best_videos, out_dir, drama_name,
                                     src_dir=src_dir)
            
            # 标记完成
            self._raise_if_cancelled(cancel_event)
            # P7: 账号取实际输出目录（output/{账号}/{剧名}），与交付报告/水印一致
            try:
                acc_dir = os.path.dirname(out_dir.rstrip('/\\'))
                account = os.path.basename(acc_dir)
                if not account or account == 'output':
                    account = self.assign_account(drama_name)
            except Exception:
                account = self.assign_account(drama_name)
            self.state_manager.add_completed(drama_name, account, {
                'ai': len(ai_videos),
                'edl': len(edl_videos),
                'total': len(best_videos)
            }, out_dir)

            # P1-A3：交付后清掉未进入交付集的渲染产物（后处理失败被跳过的、
            # version_count 降级后多余的旧 vid），防"假成品"污染后续闸门。
            # 本 run 的 edl_config 保留：与交付一致，可当简易 manifest 排查用。
            delivered = {v['filename'] for v in best_videos}
            swept = self._clean_partial_outputs(out_dir, keep_filenames=delivered,
                                                include_configs=False)
            if swept:
                self.logger.log(drama_name, 'WARN', f'交付清理：删除{swept}个未交付渲染产物')
            
            self._update_state('idle', '', 100, '完成，可上传')
            self._emit_cut_progress(
                'completed', 100,
                f'完成：交付 {len(best_videos)} 版，可进入内容风险复核',
                delivered=len(best_videos), output_dir=out_dir)
            
            # 创建素材标记
            try:
                with open(os.path.join(src_dir, "已切完.txt"), "w") as f:
                    f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as e:
                self.logger.log('system', 'WARN', f'写已切完标记失败：{str(e)}')
            
            self.logger.log(drama_name, 'INFO', f'完成：{len(best_videos)}个版本，可上传（账号 {account}，见交付报告）')
            
            return {
                'name': drama_name,
                'success': True,
                'output_dir': out_dir,
                'error': None
            }
        
        except CutCancelled as e:
            self.logger.log(drama_name, 'INFO', f'切片已停止：{str(e)}')
            self._emit_cut_progress('cancelled', 0, '收到停止请求')
            self._update_state('idle', '', 0, '已停止')
            swept = self._clean_partial_outputs(
                out_dir, include_configs=False,
                keep_filenames=self._valid_delivered_filenames(out_dir))
            if swept:
                self.logger.log(
                    drama_name, 'INFO',
                    f'停止清理：已删除{swept}个未交付渲染产物（保留EDL配置与已合格成品可续跑）')
            return {
                'name': drama_name,
                'success': False,
                'cancelled': True,
                'output_dir': None,
                'error': str(e),
            }
        except Exception as e:
            self.logger.log(drama_name, 'ERROR', f'切片失败：{str(e)}')
            self._emit_cut_progress('failed', 0, f'切片失败：{str(e)[:160]}')
            self.state_manager.add_failed(drama_name, str(e))
            # 状态文件也必须离开 processing，否则历史页/API 会长期显示伪“处理中”。
            self._update_state('idle', '', 0, f'切片失败：{str(e)[:120]}')
            # P1-A2：失败路径清掉本次 run 的渲染产物，瞬时故障不升级为永久卡死。
            # out_dir 是单剧目录且 run 开始时已清过，此刻的 mp4 全是本次渲染的
            # 未交付半成品；留着它们，重试会被"已有成品/已切完"闸门永久跳过。
            # include_configs=False：保留 edl_config_v*.json 作为失败现场，
            # 供 _re_render_from_configs 续跑（失败可继续处理），不重新生成 EDL。
            swept = self._clean_partial_outputs(
                out_dir, include_configs=False,
                keep_filenames=self._valid_delivered_filenames(out_dir))
            if swept:
                self.logger.log(drama_name, 'WARN', f'失败清理：已删除{swept}个未交付渲染产物（保留EDL配置与已合格成品可续跑）')
            return {
                'name': drama_name,
                'success': False,
                'output_dir': None,
                'error': str(e)
            }
        
        finally:
            self._release_lock(lock)
    
    @staticmethod
    def _raise_if_cancelled(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise CutCancelled('用户已停止切片')

    @staticmethod
    def _terminate_process_tree(process):
        """终止进程及其 ffmpeg 等子进程，避免停止后继续写文件。"""
        if process.poll() is not None:
            return
        try:
            if os.name == 'nt':
                taskkill = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                        'System32', 'taskkill.exe')
                subprocess.run(
                    [taskkill, '/PID', str(process.pid), '/T', '/F'],
                    capture_output=True, timeout=3, check=False)
            else:
                process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=1)
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=1)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except Exception:
                    pass

    def _run_command_cancellable(self, cmd, timeout, cancel_event=None, cwd=None,
                                 progress_probe=None, stall_timeout=None):
        """运行子进程；cancel_event 置位时终止整棵进程树。

        未提供取消事件时仍走 subprocess.run，保持已有调用和测试兼容。

        P1 卡顿检测：progress_probe 为无参可调用，返回最近一次有进度的时间戳
        （monotonic），或 None 表示尚未有进度；stall_timeout 给定且当前时间减
        最近进度超过该值，判定下载卡顿，终止整棵进程树并抛 DownloadStalled。
        """
        self._raise_if_cancelled(cancel_event)
        child_env = self._child_process_env()
        if cancel_event is None:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
                cwd=cwd,
                env=child_env,
            )

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=cwd,
            env=child_env,
        )
        started_at = time.monotonic()
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    self._terminate_process_tree(process)
                    raise CutCancelled('用户已停止切片')
                # P1: 卡顿检测 —— 进程活着但长时间无进度，主动终止并重试
                if stall_timeout and progress_probe is not None:
                    try:
                        last_progress = progress_probe()
                    except Exception:
                        last_progress = None
                    if last_progress is not None and \
                       (time.monotonic() - last_progress) >= stall_timeout:
                        self.logger.log('system', 'WARN',
                            f'下载卡顿：{stall_timeout}s 无新文件，终止并重试')
                        self._terminate_process_tree(process)
                        raise DownloadStalled(
                            f'下载卡顿（{stall_timeout}s无进度），已终止待重试')
                if time.monotonic() - started_at >= timeout:
                    self._terminate_process_tree(process)
                    raise subprocess.TimeoutExpired(cmd, timeout)

    @staticmethod
    def _child_process_env():
        """强制 Python 子进程使用 UTF-8，避免 Windows GBK 日志编码崩溃。"""
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        return env

    def _run_asr(self, src_dir, drama_name, cancel_event=None):
        """运行ASR转写"""
        try:
            self.logger.log(drama_name, 'INFO', '开始ASR转写...')
            
            # 检查transcribe_all.py是否存在
            if not os.path.exists(CONFIG['asr_script']):
                self.logger.log(drama_name, 'WARN', f'ASR脚本不存在：{CONFIG["asr_script"]}')
                return None
            
            # 运行ASR
            cmd = [
                sys.executable,
                CONFIG['asr_script'],
                src_dir
            ]
            
            with execution_gate.asr_slot(cancel_event):
                result = self._run_command_cancellable(
                    cmd, timeout=600, cancel_event=cancel_event, cwd=src_dir)
            
            if result.returncode == 0:
                # 检查输出文件
                transcript_path = os.path.join(src_dir, 'transcribe', 'full_transcript.txt')
                if os.path.exists(transcript_path):
                    self.logger.log(drama_name, 'INFO', 'ASR转写完成')
                    return transcript_path
                else:
                    self.logger.log(drama_name, 'WARN', 'ASR完成但未找到转录文件')
                    return None
            else:
                self.logger.log(drama_name, 'ERROR', f'ASR失败：{result.stderr[:200]}')
                return None
                
        except CutCancelled:
            self.logger.log(drama_name, 'INFO', 'ASR转写已停止')
            raise
        except Exception as e:
            self.logger.log(drama_name, 'ERROR', f'ASR异常：{str(e)}')
            return None
    
    def _extract_quotes(self, transcript_path, count=10):
        """从ASR转录中提取台词 - 返回短标题(16字无标点)和长标题"""
        if not transcript_path or not os.path.exists(transcript_path):
            return [], []
        
        try:
            with open(transcript_path, 'r', encoding='utf-8') as f:
                text = f.read()
            
            lines = []
            for l in text.split('\n'):
                l = l.strip()
                if not l:
                    continue
                # transcribe_dir.py 输出格式: "[12.3-15.1] 你好啊" —— 剥掉时间戳前缀，只留台词
                m = re.match(r'^\[[\d.\-]+\]\s*(.+)$', l)
                if m:
                    l = m.group(1).strip()
                lines.append(l)
            
            # 冲突+悬念关键词
            conflict_words = ['打脸', '傻', '死', '滚', '敢', '谁', '凭什么', 
                            '不可能', '疯了', '假', '真', '骗', '恨', '杀', '血', '泪']
            suspense_words = ['发现', '秘密', '真相', '原来', '竟然', '居然', 
                            '突然', '没想到', '终于']
            
            short_titles = []
            long_titles = []
            
            for line in lines:
                if 5 <= len(line) <= 80:
                    has_conflict = any(w in line for w in conflict_words)
                    has_suspense = any(w in line for w in suspense_words)
                    
                    if has_conflict or has_suspense:
                        # 长标题：保留完整句子
                        long_titles.append(line)
                        
                        # 短标题：16字以内，去掉标点
                        short = line[:16]
                        # 去标点
                        for punct in PUNCTUATIONS:
                            short = short.replace(punct, '')
                        short_titles.append(short)
            
            # 去重
            short_titles = list(dict.fromkeys(short_titles))[:count]
            long_titles = list(dict.fromkeys(long_titles))[:count]
            
            # 兜底
            if not short_titles and not long_titles and len(lines) > count:
                start = len(lines) // 2
                for line in lines[start:start+count]:
                    long_titles.append(line)
                    short = line[:16]
                    for punct in PUNCTUATIONS:
                        short = short.replace(punct, '')
                    short_titles.append(short)
            
            return short_titles, long_titles
            
        except Exception as e:
            self.logger.log('system', 'ERROR', f'提取台词失败：{str(e)}')
            return [], []

    @staticmethod
    def _sanitize_short_title(title, max_len=16, min_len=4):
        """强制短标题规范：去标点/空白 → 截断到 max_len 字，≥min_len 字。

        视频号短标题要求短、无标点、抓人。AI 标题通常已 6-16 字无标点，但
        AI 失效/去重兜底用『剧名+hook+瞬间』可能超长或带标点；ASR 抽取的
        16 字金句是更优兜底。本函数对任何来源做最终规范，保证落库标题合规。
        """
        import re as _re
        s = _re.sub(r'[\s\u3000\uff0c\u3001\u3002\uff01\uff1f\uff1b\uff1a\u201c'
                    r'\u201d\u2018\u2019\u2014\u2013\u2026\uff08\uff09\u3010'
                    r'\u3011《》【】\[\]\(\)\{\}<>、,.;:!?\'\"|·~\-_]', '',
                    str(title or ''))
        s = s.strip()
        if not s:
            return ''
        return s[:max_len] if len(s) >= min_len else s

    def _pick_version_title(self, short_titles, long_titles, v_idx, drama_name,
                            hook, ai_title=None):
        """选取本版短标题：优先 AI 标题（6-16字无标点），失效时用 ASR 金句，
        最后兜底『剧名+hook+瞬间』，统一经 _sanitize_short_title 规范。"""
        s = self._sanitize_short_title(ai_title) if ai_title else ''
        if not s and short_titles:
            s = self._sanitize_short_title(
                short_titles[v_idx % len(short_titles)])
        if not s and long_titles:
            s = self._sanitize_short_title(
                long_titles[v_idx % len(long_titles)])
        if not s:
            s = self._sanitize_short_title(f'{drama_name}{hook}瞬间')
        if not s:
            s = '精彩瞬间'
        return s


    def parse_episode_number(self, filename):
        """解析文件名中的集数，兼容 '1.mp4' 和 '第1集.mp4' 等格式"""
        # 格式1: 第X集.mp4
        match = re.search(r'第(\d+)集', filename)
        if match:
            return int(match.group(1))
        
        # 格式2: X.mp4 或 X - XXX.mp4
        match = re.search(r'(\d+)\.mp4', filename, re.IGNORECASE)
        if match:
            return int(match.group(1))
        
        return None
    
    def get_episode_file(self, src_dir, ep_num):
        """根据集数获取文件路径"""
        try:
            # 先找 '第X集.mp4'
            for f in os.listdir(src_dir):
                if f.endswith('.mp4'):
                    num = self.parse_episode_number(f)
                    if num == ep_num:
                        return os.path.join(src_dir, f)
        except Exception as e:
            self.logger.log('system', 'WARN', f'获取剧集文件失败：{str(e)}')
        return None
    
    def list_episodes(self, src_dir):
        """列出所有集数，返回 [(ep_num, filepath), ...] - 递归扫描子目录，按集号去重。

        去重根因：网盘源 /短剧素材/{剧名}/ 若同时含顶层文件与 {剧名}/ 子目录
        （分享链接内容本身是目录），递归扫描会得 2N 个文件；本方法对同一集号
        只保留路径最浅（最接近顶层）的一份，避免「72集」误报与双份裁剪。
        """
        by_ep = {}
        try:
            # 使用递归扫描，处理嵌套目录结构
            for f in self._scan_files_recursive(src_dir):
                if f.endswith('.mp4') and not f.endswith('.downloading'):
                    fname = os.path.basename(f)
                    ep_num = self.parse_episode_number(fname)
                    if ep_num is not None:
                        # 去重：保留路径更浅（更少分隔符）的副本：顶层 第N集.mp4 优先于 嵌套 剧名-第N集.mp4
                        cur = by_ep.get(ep_num)
                        if cur is None or f.count(os.sep) < cur[1].count(os.sep):
                            by_ep[ep_num] = (ep_num, f)
        except Exception as e:
            self.logger.log('system', 'WARN', f'列出剧集失败：{str(e)}')
        episodes = [by_ep[k] for k in sorted(by_ep)]
        return episodes
    
    def _generate_edl_versions(self, drama_name, src_dir, edl_eps, count, out_dir,
                               use_reverse=False, short_titles=None, long_titles=None,
                               cancel_event=None, config=None):
        """生成EDL版本 - 优先导演叙事结构，高光缺失时随机兜底。"""
        self._raise_if_cancelled(cancel_event)
        self.logger.log(drama_name, 'INFO', f'生成EDL版本（{count}个）')
        self._emit_cut_progress('version_plan', 40,
                                f'生成 {count} 个版本的叙事与文案方案')
        
        # 初始化AI文案生成器
        ai_copy = AICopyGenerator()
        
        # 获取剧集列表
        episodes = self.list_episodes(src_dir)
        ep_nums = [ep[0] for ep in episodes]
        
        if not ep_nums:
            self.logger.log(drama_name, 'ERROR', '未找到有效的剧集文件')
            return []
        # 断点续跑：记录素材签名 + 初始化清单。若本次中途崩溃，_cut_once 据此
        # 自动识别有效现场并续跑（源签名匹配才续，素材变了则全量重切）。
        self._save_resume_manifest(out_dir, {
            'source_sig': self._source_signature(src_dir),
            'versions': {},
        })

        # 使用AI生成文案
        titles = []
        descriptions = []
        hashtags_list = []
        
        # 钩子主题库：无导演方案时按 v_idx 轮换（与 ARC_ANGLES 同源，保证回退也丰富）
        keywords = list(self.ARC_ANGLES)
        videos = []
        
        # 导演/编剧弧线方案：每版独立主线（不同开头集/切入角度/叙事顺序），
        # 解决 V1-V8 用同一段素材、开头雷同。GLM 失败回退默认方案（top-N 高分集
        # 轮换开头），不阻塞版本生成。
        edl_plan = None
        if use_reverse:
            try:
                eps_hl = self._load_episode_highlights(edl_eps, drama_name=drama_name)
                edl_plan = self._analyze_drama_arc(eps_hl, min(count, len(ep_nums)),
                                                   drama_name=drama_name)
                if edl_plan:
                    self.logger.log(
                        drama_name, 'INFO',
                        f'导演方案就绪：{len(edl_plan)}版，'
                        f'开头集 {[v["opening_ep"] for v in edl_plan]}，'
                        f'角度 {[v["angle"] for v in edl_plan]}')
            except Exception as e:
                self.logger.log(drama_name, 'WARN',
                                f'导演方案生成异常，走关键词轮换：{e}')
                edl_plan = None
        
        expected_count = min(count, len(ep_nums))
        for v_idx in range(expected_count):
            self._raise_if_cancelled(cancel_event)
            # 使用AI生成文案
            hook = (edl_plan[v_idx]['angle']
                    if (edl_plan and v_idx < len(edl_plan))
                    else keywords[v_idx % len(keywords)])
            # 把ASR抽出的真实金句喂给AI，不再编假plot_summary
            quote = long_titles[v_idx % len(long_titles)] if long_titles else ''
            try:
                ai_result = ai_copy.generate_copy(
                    drama_name=drama_name,
                    version_idx=v_idx + 1,
                    version_hook=hook,
                    plot_summary=f'本剧高光台词参考：{quote}' if quote else '',
                    drama_genre='',
                    quotes=long_titles[:5] if long_titles else []
                )
                # 标题去重：同剧已用标题则换 hook 重新生成（最多3次），兜底模板
                if ai_result['title'] in titles:
                    self.logger.log(drama_name, 'WARN',
                                    f'V{v_idx+1} 标题与已有版本重复: {ai_result["title"]}，换 hook 重试')
                    dedup_hook = hook
                    dedup_ok = False
                    for dedup_attempt in range(3):
                        dedup_hook = keywords[(keywords.index(dedup_hook) + 1) % len(keywords)]
                        try:
                            retry = ai_copy.generate_copy(
                                drama_name=drama_name,
                                version_idx=v_idx + 1,
                                version_hook=dedup_hook,
                                plot_summary=f'本剧高光台词参考：{quote}' if quote else '',
                                drama_genre='',
                                quotes=long_titles[:5] if long_titles else []
                            )
                            if retry and retry['title'] not in titles:
                                ai_result = retry
                                hook = dedup_hook
                                dedup_ok = True
                                break
                        except Exception:
                            continue
                    if not dedup_ok:
                        ai_result['title'] = self._pick_version_title(
                            short_titles, long_titles, v_idx, drama_name, hook)
                        self.logger.log(drama_name, 'WARN', f'V{v_idx+1} 标题去重兜底为模板')
                # 短标题规范：任何来源统一 ≤16 字无标点（AI 失效/超长都兜住）
                _st = self._pick_version_title(
                    short_titles, long_titles, v_idx, drama_name, hook,
                    ai_title=ai_result.get('title'))
                titles.append(_st)
                descriptions.append(ai_result['description'])
                hashtags_list.append(ai_result['hashtags'])
                self.logger.log(drama_name, 'INFO', f'V{v_idx+1} AI文案生成成功')
            except Exception as e:
                # AI失败时使用模板（优先 ASR 金句短标题）
                self.logger.log(drama_name, 'WARN', f'V{v_idx+1} AI文案生成失败，使用模板: {e}')
                titles.append(self._pick_version_title(
                    short_titles, long_titles, v_idx, drama_name, hook))
                descriptions.append(f'{drama_name}精彩剧情，{hook}瞬间，不容错过！')
                hashtags_list.append(["#短剧", "#短剧推荐", "#热门", f"#{hook}"])
        
        # fail-fast ①源头预检：source_pattern 统一在循环外计算一次（所有版本共享同一源模式）。
        # 递归扫描取样（素材可能只在嵌套子目录，如 BaiduPCS-Go 下载产物）
        sample_files = [os.path.basename(f)
                        for f in self._scan_files_recursive(src_dir)
                        if f.endswith('.mp4')][:3]
        if sample_files:
            source_pattern = self._detect_source_pattern(sample_files[0])
        else:
            source_pattern = "{ep}.mp4"

        # fail-fast ①：source_pattern 无法匹配任何实际源文件 = 系统性错误
        # （所有版本必然失败，如命名格式漏识别/素材缺失），立即停止，不浪费在
        # 必然失败的版本上。单文件偶发缺失不在此列（由循环内连续失败②兜底）。
        precheck_err = self._precheck_source_pattern(src_dir, source_pattern, drama_name)
        if precheck_err:
            self.logger.log(drama_name, 'ERROR',
                            f'源文件预检失败（系统性错误，停止全部版本）：{precheck_err}')
            raise RuntimeError(
                f'源文件模式不匹配（系统性错误）：{precheck_err}')

        # fail-fast ②状态：连续同类别失败计数（≥2 同类失败 = 系统性错误，停止剩余版本）
        consecutive_fail = 0
        last_fail_cat = None
        # P8: 每版交付 verdict（accepted/rejected/render_failed）→ 循环后写渲染报告
        verdicts = {}

        # 方案B：跨版本 (集,起点) 零复用——所有版本共享一个全局已用集合，
        # 保证任何素材片段在全部版本中最多出现一次。
        used_global = set() if CONFIG['edl'].get('dedup_segments') else None

        for v_idx in range(expected_count):
            self._raise_if_cancelled(cancel_event)
            select_pct = 43 + int(5 * v_idx / max(1, expected_count))
            self._emit_cut_progress(
                'segment_select', select_pct,
                f'规划 V{v_idx + 1}/{expected_count} 的素材片段',
                version=v_idx + 1)
            # 根据倒叙或随机生成segments
            if use_reverse:
                segments = self._generate_reverse_segments(edl_eps, v_idx,
                                                           drama_name=drama_name,
                                                           plan=edl_plan,
                                                           used_global=used_global,
                                                           config=config)
            else:
                # 随机兜底路径也按剧情驱动预算（_generate_reverse_segments 内部已设）
                self._active_seg_target = self._compute_version_budget(
                    edl_plan, v_idx, None, config=config)
                segments = self._generate_random_segments(edl_eps, v_idx,
                                                          used_global=used_global)
            
            if not segments:
                verdicts[v_idx] = {'status': 'not_rendered',
                                   'reason': '未能生成EDL片段'}
                continue

            # 因果链校验（报告提案3）：对段序列做相邻因果/时间线校验，自动替换
            # 断层段。fail-open：LLM 失败/无台词 → 原样放行，绝不阻塞版本生成。
            if use_reverse and CONFIG.get('causal_check', {}).get('enabled', True):
                try:
                    _hl = self._load_episode_highlights(
                        edl_eps, drama_name=drama_name)
                    segments = self._causal_check_segments(
                        segments, _hl, drama_name, out_dir, v_idx,
                        used_global=used_global)
                except Exception as _e:
                    self.logger.log(drama_name, 'WARN',
                                    f'因果链校验跳过（原样放行）：{_e}')
            if not segments:
                verdicts[v_idx] = {'status': 'not_rendered',
                                   'reason': '因果链替换后无片段'}
                continue

            # source_pattern 已在循环外统一计算（fail-fast ①，所有版本共享同一源模式）
            # 交付文件名的安全剧名（与 render_generic 一致：全角/半角逗号、空格 → _）
            drama_safe = drama_name.replace('，', '_').replace(',', '_').replace(' ', '_')
            # 钩子关键词（与 config videos[0].hook_keyword 一致）
            kw = (edl_plan[v_idx]['angle']
                  if (edl_plan and v_idx < len(edl_plan))
                  else keywords[v_idx % len(keywords)])
            artifacts = {
                "video_path": os.path.join(out_dir, f'{drama_safe}_{v_idx+1}_{kw}_去重版.mp4'),
                # _generate_cover 用原始剧名写 covers/{剧名}_{版本}_封面.jpg
                "cover_path": os.path.join(out_dir, 'covers', f'{drama_name}_{v_idx+1}_封面.jpg'),
                "source_dir": src_dir,
                "output_dir": out_dir,
            }

            edl_config = {
                "drama_name": drama_name,
                "source_dir": src_dir,
                "source_pattern": source_pattern,
                "output_dir": out_dir,
                # 水印文本 = 本剧配置覆盖，否则输出目录账号名（output/{账号}/{剧名}
                # 的 {账号}），与交叉轮询 assign_account 产出一致；render_generic.py
                # 读不到时才落回默认"福桃说剧"，这里显式写入保证 账号/水印/输出 三者统一。
                "watermark_text": (self._resolve_cut_config(config)['watermark']
                                   or os.path.basename(os.path.dirname(out_dir))),
                "artifacts": artifacts,
                "videos": [
                    {
                        "id": v_idx + 1,
                        "hook_keyword": (edl_plan[v_idx]['angle']
                                         if (edl_plan and v_idx < len(edl_plan))
                                         else keywords[v_idx % len(keywords)]),
                        "title": titles[v_idx % len(titles)],
                        "description": descriptions[v_idx % len(descriptions)],
                        "hashtags": hashtags_list[v_idx % len(hashtags_list)],
                        "segments": segments
                    }
                ]
            }
            
            # 保存EDL配置
            config_path = os.path.join(out_dir, f"edl_config_v{v_idx+1}.json")
            try:
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(edl_config, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.logger.log('system', 'WARN', f'保存EDL配置失败：{str(e)}')
            
            self.logger.log(drama_name, 'INFO', f'生成EDL配置V{v_idx+1}：{len(segments)}个片段，'
                                                 f'规划合计{sum(s["duration"] for s in segments):.0f}s')
            
            # 调用render_generic.py（公共渲染方法，失败续跑也复用）
            render_pct = 48 + int(40 * v_idx / max(1, expected_count))
            self._emit_cut_progress(
                'render', render_pct,
                f'渲染 V{v_idx + 1}/{expected_count}',
                version=v_idx + 1,
                segments=len(segments), duration=round(sum(s['duration'] for s in segments), 1))
            # 渲染超时随本版规划时长缩放：长版（用户调大 max_dur）不再固定 600s 超时。
            render_timeout = max(600, int(sum(s['duration'] for s in segments) * 2.5))
            video, fail_cat, verdict = self._run_render_one(
                drama_name, out_dir, v_idx, config_path,
                cancel_event=cancel_event, timeout=render_timeout)
            render_done_pct = 48 + int(40 * (v_idx + 1) / max(1, expected_count))
            self._emit_cut_progress(
                'render', render_done_pct,
                f'V{v_idx + 1}/{expected_count} '
                f'{"渲染完成" if video else "渲染未通过"}',
                version=v_idx + 1,
                accepted=bool(video))
            # P8: 记录本版交付 verdict（accepted/rejected/render_failed）供报告反映
            verdicts[v_idx] = verdict
            if video:
                videos.append(video)
                consecutive_fail = 0
                last_fail_cat = None
                # 断点续跑：标记本版完成（主路径不删 config，靠清单+文件存在性幂等）
                self._mark_resume_version(
                    out_dir, v_idx, status='post_done',
                    artifact=video.get('filename'))
            else:
                # fail-fast ②：连续同类别失败 = 系统性错误（源文件缺失/编码器故障等），
                # 剩余版本必然同样失败，立即停止。不同类或单次失败视为偶发，继续。
                if fail_cat and fail_cat == last_fail_cat:
                    consecutive_fail += 1
                else:
                    consecutive_fail = 1
                    last_fail_cat = fail_cat
                if fail_cat and fail_cat not in ('other', 'quality') and consecutive_fail >= 2:
                    self.logger.log(drama_name, 'ERROR',
                        f'连续{consecutive_fail}个版本同类失败（{fail_cat}），判定系统性错误，'
                        f'停止剩余版本（已成功{len(videos)}个）')
                    break

        # P8：渲染报告反映交付结论——render_generic 每版覆写报告（只剩末版），
        # 这里统一按每版 verdict 重写，拒收版标 REJECTED 而非 OK。
        self._write_render_report(out_dir, drama_name, verdicts,
                                  expected_count=expected_count)
        
        return videos

    @staticmethod
    def _detect_source_pattern(first_file):
        """从实际样例文件名推导 render_generic 使用的源文件名模板。"""
        name = os.path.basename(first_file or '')
        # 纯“第N集.mp4”不能拼成“剧名第N集.mp4”。若源文件使用前导零
        # （第01集.mp4），模板必须保留相同宽度，否则数字格式化后会全部失配。
        cn = re.fullmatch(r'第(\d+)集\.mp4', name, re.IGNORECASE)
        if cn:
            width = len(cn.group(1))
            ep_field = (f'{{ep:0{width}d}}'
                        if width > 1 and cn.group(1).startswith('0')
                        else '{ep}')
            return f'第{ep_field}集.mp4'
        if "第" in name and "集" in name:
            # 取最后一个 第N集 作为集号标记（避免剧名本身含"第N集"时误判，
            # 如"第1集就离婚-第02集.mp4" 应取 "第02集" 为集号标记）。
            matches = list(re.finditer(r'第(\d+)集', name))
            cn_any = matches[-1] if matches else None
            digits = cn_any.group(1) if cn_any else ''
            ep_field = (f'{{ep:0{len(digits)}d}}'
                        if len(digits) > 1 and digits.startswith('0')
                        else '{ep}')
            # 前缀用样例文件的真实字面量，而非 {name} 占位符：剧名可能带序号前缀
            # （如"12重生成他弟媳后"）而文件名不带，{name} 替换后会全部失配，
            # 导致 fail-fast 预检系统性误杀。字面量前缀含分隔符（"-"、" - "）一并
            # 保留；花括号转义防后续 .format(name=..., ep=...) 误解析。
            prefix = (name[:cn_any.start()] if cn_any else '')
            prefix = prefix.replace('{', '{{').replace('}', '}}')
            return f"{prefix}第{ep_field}集.mp4"
        bare = re.fullmatch(r'(\d+)\.mp4', name, re.IGNORECASE)
        if bare:
            width = len(bare.group(1))
            return (f'{{ep:0{width}d}}.mp4'
                    if width > 1 and bare.group(1).startswith('0')
                    else '{ep}.mp4')
        suffix = re.search(r'([_-])(\d+)\.mp4$', name, re.IGNORECASE)
        if suffix:
            sep, digits = suffix.group(1), suffix.group(2)
            ep_field = (f'{{ep:0{len(digits)}d}}'
                        if len(digits) > 1 and digits.startswith('0')
                        else '{ep}')
            # 同上：前缀用真实字面量（含分隔符），不用 {name} 占位符。
            prefix = name[:suffix.start()] + sep
            prefix = prefix.replace('{', '{{').replace('}', '}}')
            return f"{prefix}{ep_field}.mp4"
        return "{ep}.mp4"

    def _repair_config_source_pattern(self, config, drama_name):
        """续跑前按当前真实素材重建 source_pattern，清除旧配置污染。

        旧版本可能把 `第10集.mp4` 误存成 `{name}第{ep}集.mp4`；若原样续跑，
        修复后的全量路径正常、续跑路径却仍会失败。这里用配置中的 source_dir
        （失效时回退标准素材目录）重新探测，并原子回写配置。
        """
        src_dir = config.get('source_dir') or os.path.join(
            CONFIG['download_base_dir'], drama_name)
        files = self._scan_files_recursive(src_dir) if os.path.isdir(src_dir) else []
        if not files:
            return config
        pattern = self._detect_source_pattern(os.path.basename(files[0]))
        config['source_dir'] = src_dir
        config['source_pattern'] = pattern
        return config
    
    def _precheck_source_pattern(self, src_dir, source_pattern, drama_name):
        """fail-fast ①源头预检：source_pattern 能否匹配到实际源文件。

        返回 None=通过；返回 str=系统性错误消息（应停止全部版本）。
        与渲染端（render_generic 用 config drama_name 拼源路径）同源判定：
        预检拦下的，渲染必然失败；宁可拦下让用户看到，也不让所有版本
        浪费在必然失败的渲染上。单文件偶发缺失不在此列（循环内连续失败②兜底）。
        """
        files = [os.path.basename(f) for f in self._scan_files_recursive(src_dir)
                 if f.lower().endswith('.mp4')]
        if not files:
            return f'源目录无 mp4 文件：{src_dir}'
        matched = 0
        for bn in files:
            # 取文件名末尾数字（集号，如 前夫你来迟了-36.mp4 → 36）
            m = re.search(r'(\d+)(?=[^\d]*\.mp4$)', bn)
            if not m:
                continue
            try:
                if source_pattern.format(name=drama_name, ep=int(m.group(1))) == bn:
                    matched += 1
            except Exception:
                # pattern/name 含特殊字符（如 {}），无法重建——交给渲染端处理
                continue
        if matched == 0:
            return (f'source_pattern "{source_pattern}" 无法匹配任何实际源文件'
                    f'（样例文件名：{sorted(files)[:3]}）——所有版本渲染将必然失败')
        return None

    def _classify_render_failure(self, stderr='', exc=None):
        """把渲染失败 stderr/异常归类为 {'source','encode','other'}。

        'source' = 源文件缺失/路径错（系统性，fail-fast ②关注）
        'encode' = 编码器/资源错误（系统性，fail-fast ②关注）
        'other' = 其他/未识别（偶发，不触发 fail-fast ②）
        """
        s = (stderr or '') + (str(exc or ''))
        if isinstance(exc, FileNotFoundError) or re.search(r'源文件不存在|No such file|FileNotFoundError', s):
            return 'source'
        if re.search(r'Conversion failed|Invalid data|No space left|Cannot allocate memory|'
                     r'Permission denied|device or resource busy|Error initializing', s):
            return 'encode'
        return 'other'

    def _run_render_one(self, drama_name, out_dir, v_idx, config_path,
                        cancel_event=None, timeout=600):
        """对单个 edl_config_v*.json 调 render_generic.py 渲染。

        公共方法：_generate_edl_versions（全量生成）与 _re_render_from_configs
        （失败续跑）共用，保证渲染/日志行为一致。

        timeout：本版渲染上限（秒）。默认 600；调用方按本版规划时长缩放
        （max(600, 规划秒数×2.5)），长版不再 600s 固定超时。

        Returns: video dict（path/final_path/filename/type）或 None（失败）。
        """
        try:
            with execution_gate.render_slot(cancel_event):
                result = self._run_command_cancellable(
                    [sys.executable, os.path.join(CONFIG['scripts_dir'], "render_generic.py"), config_path],
                    timeout=timeout, cancel_event=cancel_event,
                    cwd=os.path.dirname(CONFIG['scripts_dir']),
                )

            if result.returncode == 0:
                self.logger.log(drama_name, 'INFO', f'V{v_idx+1} EDL渲染成功')
                # 查找输出文件
                # P2-2: 锚定版本匹配（文件名尾段 {drama}_{n}_{主题}_去重版.mp4 或 V{n}.mp4），
                # 防剧名含 "_数字_"（如"夺产_2_版"）时 v2 误绑 v1 文件
                pat = re.compile(rf'_{v_idx+1}_[^_]*_去重版\.mp4$|V{v_idx+1}\.mp4$')
                output_files = [f for f in os.listdir(out_dir) if f.endswith(".mp4") and pat.search(f)]
                video = None
                for output_file in output_files:
                    video = {
                        'path': os.path.join(out_dir, output_file),
                        'final_path': os.path.join(out_dir, output_file),
                        'filename': output_file,
                        'type': 'edl'
                    }
                    # R6 媒体质量门禁：拒收 0 字节/无视频流/无音轨/时长异常产物
                    quality = self._validate_delivery_media(video['path'])
                    if not quality.get('ok'):
                        reason = quality.get('reason', '未知')
                        self.logger.log(
                            drama_name, 'ERROR',
                            f'V{v_idx+1} 交付质量拒收（{reason}）：{output_file}')
                        # P5：禁止删除未交付成片——拒收不删，重命名 *_REJECTED.mp4
                        # 保留供人工复核或补齐时长；交付清理会跳过 *_REJECTED_ 文件。
                        try:
                            src_p = video['path']
                            if src_p.endswith('.mp4'):
                                rej_p = src_p[:-4] + '_REJECTED.mp4'
                                os.rename(src_p, rej_p)
                                self.logger.log(
                                    drama_name, 'WARN',
                                    f'V{v_idx+1} 拒收成片保留为 '
                                    f'{os.path.basename(rej_p)}（供复核/补齐）')
                        except OSError as e:
                            self.logger.log(
                                drama_name, 'WARN',
                                f'V{v_idx+1} 保留拒收成片失败（可忽略）：{e}')
                        return None, 'quality', {'status': 'rejected', 'reason': reason}
                    # 回写实际 video_path 到 artifacts（钩子可能与预判不同）
                    self._update_config_artifacts(
                        out_dir, v_idx + 1, 'video_path',
                        os.path.join(out_dir, output_file))
                if video is None:
                    reason = '渲染命令成功但未找到对应版本产物'
                    self.logger.log(drama_name, 'ERROR',
                                    f'V{v_idx+1} 渲染失败：{reason}')
                    return None, 'other', {
                        'status': 'render_failed', 'reason': reason}
                return video, None, {'status': 'accepted', 'reason': ''}
            else:
                fail_cat = self._classify_render_failure(result.stderr)
                self.logger.log(drama_name, 'ERROR',
                                f'V{v_idx+1} EDL渲染失败（{fail_cat}）：{result.stderr[:200]}')
                return None, fail_cat, {'status': 'render_failed', 'reason': fail_cat or '未知'}

        except CutCancelled:
            self.logger.log(drama_name, 'INFO', f'V{v_idx+1} 渲染已停止')
            raise
        except Exception as e:
            fail_cat = self._classify_render_failure('', e)
            self.logger.log(drama_name, 'ERROR',
                            f'V{v_idx+1} EDL异常（{fail_cat}）：{str(e)}')
            return None, fail_cat, {'status': 'render_failed', 'reason': fail_cat or '未知'}

    # ------------------------------------------------------------------
    # 断点续跑：版本级 checkpoint 清单（.resume_state.json）
    # 清单记录 source_sig + 每版本阶段状态，崩溃后 _cut_once 自动识别
    # 有效现场并续跑；无清单/源签名不匹配的旧现场回退全量重切（保守）。
    # 文件存在性校验是权威（清单只是提示），_clean_partial_outputs 删了
    # 产物后清单虽标 done 但仍会按文件缺失重渲，不会误跳过。
    # ------------------------------------------------------------------
    @staticmethod
    def _resume_manifest_path(out_dir):
        return os.path.join(out_dir, '.resume_state.json')

    def _load_resume_manifest(self, out_dir):
        try:
            with open(self._resume_manifest_path(out_dir), 'r',
                      encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_resume_manifest(self, out_dir, manifest):
        try:
            path = self._resume_manifest_path(out_dir)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as e:
            self.logger.log('system', 'WARN', f'续跑清单写入失败：{e}')

    def _mark_resume_version(self, out_dir, v_idx, status='post_done', **extra):
        """记录某版本阶段完成（原子更新清单）。status: post_done/qc_done。"""
        manifest = self._load_resume_manifest(out_dir)
        versions = manifest.setdefault('versions', {})
        entry = versions.setdefault(str(v_idx), {})
        entry['status'] = status
        entry['ts'] = datetime.now().isoformat(timespec='seconds')
        for k, v in extra.items():
            entry[k] = v
        self._save_resume_manifest(out_dir, manifest)

    def _source_signature(self, src_dir):
        """素材签名：集号 + 文件 mtime，识别素材是否变化（防旧 config 续跑错乱）。"""
        try:
            episodes = self.list_episodes(src_dir)
            return {
                'eps': [int(e[0]) for e in episodes],
                'mtimes': [int(os.path.getmtime(e[1])) for e in episodes],
            }
        except Exception as e:
            self.logger.log('system', 'WARN', f'素材签名计算失败：{e}')
            return None

    def _resume_scene_valid(self, out_dir, src_dir):
        """是否有可续跑的现场：有保留 config 且清单源签名匹配当前素材。

        无清单（改造前的旧失败现场）→ 不自动接管，回退全量重切（保守），
        避免素材已变却拿旧 config 续跑出错误成片。签名算不出 → 放行续跑。
        """
        configs = sorted(glob.glob(os.path.join(out_dir, 'edl_config_v*.json')))
        if not configs:
            return False
        manifest = self._load_resume_manifest(out_dir)
        stored = manifest.get('source_sig')
        if not stored:
            return False
        cur = self._source_signature(src_dir)
        if cur is None:
            return True
        return stored == cur

    def _find_version_final(self, out_dir, v_idx):
        """按版本号锚定查找最终成品（与 _run_render_one 相同正则）。"""
        pat = re.compile(rf'_{v_idx + 1}_[^_]*_去重版\.mp4$|V{v_idx + 1}\.mp4$')
        try:
            for f in os.listdir(out_dir):
                if f.endswith('.mp4') and pat.search(f):
                    return os.path.join(out_dir, f)
        except OSError:
            pass
        return None

    def _re_render_from_configs(self, drama_name, out_dir, cancel_event=None):
        """失败续跑：读 out_dir 里保留的 edl_config_v*.json，只重渲染，不重新生成 EDL。

        前提：失败路径不再删除 edl_config（_clean_partial_outputs include_configs=False），
        所以失败后现场完整保留，修复后可直接续跑，无需重新 ASR/EDL 生成。

        Returns: videos 列表（与 _generate_edl_versions 相同格式），供
        _select_best_videos / 后处理 / 交付复用。全部失败或无可续跑配置时返回空列表。
        """
        videos = []
        config_paths = sorted(glob.glob(os.path.join(out_dir, 'edl_config_v*.json')))
        if not config_paths:
            self.logger.log(drama_name, 'WARN', '无保留的EDL配置可续跑（可能从未生成或已被旧清理删除）')
            return videos

        self.logger.log(drama_name, 'INFO', f'续跑：找到{len(config_paths)}个保留的EDL配置，开始重渲染')
        # fail-fast ②状态：连续同类别失败计数（≥2 同类失败 = 系统性错误，停止剩余版本）
        consecutive_fail = 0
        last_fail_cat = None
        verdicts = {}
        for config_path in config_paths:
            self._raise_if_cancelled(cancel_event)
            m = re.search(r'edl_config_v(\d+)\.json$', config_path)
            v_idx = (int(m.group(1)) - 1) if m else len(videos)
            self._emit_cut_progress(
                'render', min(88, 45 + int(40 * v_idx / max(1, len(config_paths)))),
                f'续跑渲染 V{v_idx + 1}', version=v_idx + 1)
            edl_config = None
            try:
                with open(config_path, encoding='utf-8') as f:
                    edl_config = json.load(f)
                before_pattern = edl_config.get('source_pattern')
                edl_config = self._repair_config_source_pattern(
                    edl_config, drama_name)
                # 续跑兼容：旧 config 缺 watermark_text 时补写为输出目录账号名，
                # 否则 render_generic.py 落回默认"福桃说剧"，水印与账号脱节。
                if not edl_config.get('watermark_text'):
                    edl_config['watermark_text'] = os.path.basename(
                        os.path.dirname(out_dir))
                if edl_config.get('source_pattern') != before_pattern:
                    tmp_path = config_path + '.tmp'
                    with open(tmp_path, 'w', encoding='utf-8') as f:
                        json.dump(edl_config, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, config_path)
                    self.logger.log(
                        drama_name, 'INFO',
                        f'续跑配置源文件模式已修复：{before_pattern} → '
                        f'{edl_config.get("source_pattern")}')
                seg_count = len((edl_config.get('videos') or [{}])[0].get('segments', []))
                self.logger.log(drama_name, 'INFO',
                                f'续跑 V{v_idx+1}：{seg_count}个片段，配置 {os.path.basename(config_path)}')
            except Exception as e:
                self.logger.log(drama_name, 'WARN', f'续跑读取配置失败 {os.path.basename(config_path)}: {e}')
            # 断点续跑：该版本最终成品已存在且通过质量校验 → 跳过重渲染。
            # 覆盖两种崩溃场景：①渲染成功、config 删除前崩溃；②成功交付后再
            # 触发续跑（幂等）。文件存在性+质量校验是权威，清单只是提示。
            final_path = None
            if isinstance(edl_config, dict):
                try:
                    cfg_final = edl_config.get('artifacts', {}).get('video_path')
                    if cfg_final and os.path.exists(cfg_final):
                        final_path = cfg_final
                except Exception:
                    final_path = None
            if not final_path:
                final_path = self._find_version_final(out_dir, v_idx)
            if final_path and os.path.exists(final_path):
                try:
                    q = self._validate_delivery_media(final_path)
                except Exception:
                    q = {'ok': False}
                if q.get('ok'):
                    self.logger.log(
                        drama_name, 'INFO',
                        f'续跑 V{v_idx+1} 成品已存在且合格，跳过重渲染：'
                        f'{os.path.basename(final_path)}')
                    videos.append({'path': final_path, 'final_path': final_path,
                                   'filename': os.path.basename(final_path),
                                   'type': 'edl'})
                    verdicts[v_idx] = {'status': 'accepted',
                                       'reason': '已存在(跳过重渲)'}
                    self._mark_resume_version(
                        out_dir, v_idx, status='post_done',
                        artifact=os.path.basename(final_path))
                    try:
                        os.remove(config_path)
                    except OSError:
                        pass
                    continue
            # 续跑也按本版规划时长缩放渲染超时（edl_config 已含 segments）
            _segs = ((edl_config.get('videos') or [{}])[0].get('segments', [])
                     if isinstance(edl_config, dict) else [])
            _planned = sum(float(s.get('duration', 0)) for s in _segs)
            video, fail_cat, _verdict = self._run_render_one(
                drama_name, out_dir, v_idx, config_path,
                cancel_event=cancel_event,
                timeout=max(600, int(_planned * 2.5)))
            self._emit_cut_progress(
                'render', min(89, 45 + int(40 * (v_idx + 1) / max(1, len(config_paths)))),
                f'续跑 V{v_idx + 1} '
                f'{"渲染完成" if video else "渲染未通过"}',
                version=v_idx + 1, accepted=bool(video))
            if not _verdict:
                _verdict = ({'status': 'accepted', 'reason': ''} if video else
                            {'status': 'render_failed',
                             'reason': fail_cat or '未知'})
            verdicts[v_idx] = _verdict
            if video:
                videos.append(video)
                consecutive_fail = 0
                last_fail_cat = None
                # 断点续跑：标记本版完成（清单源签名+状态，供 _cut_once 自动识别现场）
                self._mark_resume_version(
                    out_dir, v_idx, status='post_done',
                    artifact=video.get('filename'))
                # P2-4: 渲染成功即删除该版本 config——失败路径保留 config 以便续跑，
                # 成功删除避免 stale config 跨 run 累积、下次续跑重复渲染抬高 real_count
                try:
                    os.remove(config_path)
                except OSError as e:
                    self.logger.log(drama_name, 'WARN', f'清理已渲染配置失败（可忽略）: {e}')
            else:
                # fail-fast ②：连续同类别失败 = 系统性错误，剩余版本必然同样失败，立即停止。
                # 不同类或单次失败视为偶发，继续。
                if fail_cat and fail_cat == last_fail_cat:
                    consecutive_fail += 1
                else:
                    consecutive_fail = 1
                    last_fail_cat = fail_cat
                if fail_cat and fail_cat not in ('other', 'quality') and consecutive_fail >= 2:
                    self.logger.log(drama_name, 'ERROR',
                        f'续跑连续{consecutive_fail}个版本同类失败（{fail_cat}），判定系统性错误，'
                        f'停止剩余版本（已成功{len(videos)}个）')
                    break
        self._write_render_report(out_dir, drama_name, verdicts,
                                  expected_count=len(config_paths))
        return videos
    
    # 成片业务窗口 210-300s（见 _select_best_videos）。渲染会跑 atempo=1.05 轻微加速，
    # 所以 EDL 规划总时长收敛到 230-285s，留出余量确保成片落进窗口。
    # 之前两个生成器都不看累计时长：10 集能排到 ~370s，成片 350-360s 全部落在窗口外——
    # 实测「断情七载，前夫求复合」V1 规划 371.5s、成片 354.9s，8 个版本渲染约 48 分钟
    # 才发现一个都不达标（只有 _select_best_videos 的不足 6 个补全兜底才没归零）。
    PLANNED_SEGS_MIN = 230.0
    PLANNED_SEGS_MAX = 285.0
    MIN_SEG_DUR = 15.0
    # 高光候选的编辑窗口。只扩到 MIN_SEG_DUR 虽能出片，但 4 分钟会塞入
    # 18 个 15s 碎段；30s 更接近“前铺垫+完整冲突+后反应”的可观看单元。
    HIGHLIGHT_WINDOW_MIN = 30.0

    def _compute_version_budget(self, plan, v_idx, eps_hl, config=None):
        """剧情驱动 + 随机：计算本版 EDL 规划预算（秒）。

        剧情量 = 导演方案叙事单元数（集数）× per_unit + 弧线跨度 × per_spread
        + 角度加成；每版加 jitter 随机抖动（避免所有版本同长），clamp 到
        [min_final, max_final]（成片秒数）。渲染会 atempo=1.05 轻微压缩，故
        预算 = 成片目标 × 1.05；且预算下限预留 MIN_SEG_DUR 停手间隙，保证
        成片 ≥ min_final（add() 在预算剩 ≤MIN_SEG_DUR 时停手）。

        时长边界改用本剧 config（用户拍板 A：每版都在 [min_dur, max_dur] 内），
        默认 min_dur=180 / max_dur=330——修复原 CONFIG['duration']['max_final']
        =1200 导致的 V1 膨胀（63 段/21 分钟 → 渲染超时 → 素材池耗尽）。

        enabled=False 或 plan 缺失 → 退回固定 CONFIG['edl']['target_dur']。
        """
        cfg = CONFIG.get('duration', {})
        rc = self._resolve_cut_config(config)
        if not cfg.get('enabled', True):
            return float(CONFIG['edl'].get('target_dur', 200.0))
        min_final = float(rc['min_dur'])
        max_final = float(rc['max_dur'])
        per_unit = float(cfg.get('per_unit', 32))
        per_spread = float(cfg.get('per_spread', 8))
        angle_bonus = cfg.get('angle_bonus', {}) or {}
        jl, jh = cfg.get('jitter', (20, 55))
        # 剧情量：导演方案的叙事集序列 → 单元数 + 时间线跨度
        order = []
        vplan = plan[v_idx] if (plan and v_idx < len(plan)) else None
        if vplan and eps_hl:
            try:
                order = self._build_arc_order(
                    eps_hl, vplan.get('opening_ep'),
                    vplan.get('order_type', 'reverse'))
            except Exception:
                order = []
        units = max(1, len(order))
        spread = 1
        if order:
            spread = max(order) - min(order) + 1
        ab = 0.0
        if vplan:
            try:
                ab = float(angle_bonus.get(vplan.get('angle', ''), 0) or 0)
            except (TypeError, ValueError):
                ab = 0.0
        base = per_unit * units + per_spread * spread + ab
        try:
            jitter = random.uniform(float(jl), float(jh))
        except (TypeError, ValueError):
            jitter = 0.0
        final_target = min(max(base + jitter, min_final), max_final)
        # 预算下限：保证成片 ≥ min_final（add 停手最大间隙 = MIN_SEG_DUR）
        min_budget = min_final * 1.05 + self.MIN_SEG_DUR
        budget = min(max(final_target * 1.05, min_budget), max_final * 1.05)
        self.logger.log(
            'system', 'INFO',
            f'时长规划 V{v_idx+1}：剧情 {units}集×{per_unit}s + 跨度'
            f'{spread}×{per_spread}s + 角度{ab:.0f}s + 抖动{jitter:.0f}s'
            f' → 成片目标 {final_target:.0f}s（预算 {budget:.0f}s）')
        return round(budget, 1)

    def _seg_budget_left(self, segments):
        """规划总时长预算还剩多少秒。

        方案B（non_overlap）：预算 = _active_seg_target（剧情驱动，由
        _compute_version_budget 按版本设定）或 CONFIG['edl']['target_dur']。
        否则用 PLANNED_SEGS_MAX（285s）原逻辑。
        """
        target = getattr(self, '_active_seg_target', None)
        if target is None:
            if CONFIG['edl'].get('non_overlap'):
                target = float(CONFIG['edl'].get('target_dur', 200.0))
            else:
                target = self.PLANNED_SEGS_MAX
        return target - sum(s["duration"] for s in segments)

    def _causal_check_segments(self, segments, eps_hl, drama_name, out_dir,
                               v_idx, used_global=None):
        """叙事逻辑因果链校验（报告提案3）。

        对有序段序列做相邻因果链/时间线校验：LLM 判定断层位置，自动用同集
        未用候选替换（≤max_replace/版），并维护 used_global 跨版本去重不变量。
        fail-open：LLM 失败/超时/无效 → 原样放行；无台词段/全无台词 → 视为
        连贯放行。结果按段序列哈希缓存于 out_dir/causal_check_v{n}.json，
        命中不重复调 API（重跑 0 成本）。

        Returns: 修改后的 segments（或原样）。
        """
        cfg = CONFIG.get('causal_check', {})
        if not cfg.get('enabled', True):
            return segments
        if not segments or len(segments) < 2:
            return segments
        max_replace = int(cfg.get('max_replace', 2))
        retry = int(cfg.get('retry', 1))

        raw_cache = {}

        def ep_raw(ep_num):
            e = next((x for x in eps_hl if x.get('episode') == ep_num), None)
            if e is None or not e.get('path'):
                return []
            if ep_num not in raw_cache:
                try:
                    jp = os.path.join(
                        os.path.dirname(os.path.abspath(e['path'])), 'transcribe',
                        'EP' + os.path.splitext(
                            os.path.basename(e['path']))[0] + '.json')
                    with open(jp, 'r', encoding='utf-8') as f:
                        raw_cache[ep_num] = json.load(f) or []
                except Exception:
                    raw_cache[ep_num] = []
            return raw_cache[ep_num]

        def seg_text(seg):
            raw = ep_raw(seg.get('episode'))
            if not raw:
                return ''
            st = float(seg.get('start', 0))
            en = st + float(seg.get('duration', 0))
            return ' '.join(s.get('text', '') for s in raw
                            if float(s.get('start', 0)) < en
                            and float(s.get('end', st)) > st).strip()[:160]

        try:
            seg_lines = []
            for i, seg in enumerate(segments):
                t = seg_text(seg)
                st = float(seg.get('start', 0))
                seg_lines.append(
                    f"[{i}] 第{seg.get('episode')}集 [{st:.0f}-"
                    f"{st + float(seg.get('duration', 0)):.0f}s] {t or '（无台词）'}")
            if not any(seg_text(s) for s in segments):
                return segments  # 全无台词（关键词兜底），无法判断 → 放行

            sig = hashlib.md5(json.dumps([
                {'ep': s.get('episode'),
                 'st': round(float(s.get('start', 0)), 1),
                 'd': round(float(s.get('duration', 0)), 1)}
                for s in segments], ensure_ascii=False).encode('utf-8')).hexdigest()[:16]
            cache_path = os.path.join(
                out_dir, f'{self.CAUSAL_CACHE_PREFIX}{v_idx + 1}.json')
            breaks = None
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    c = json.load(f)
                if isinstance(c, dict) and c.get('sig') == sig:
                    breaks = c.get('breaks')
            except Exception:
                pass
            if breaks is None:
                breaks = self._causal_check_llm(drama_name, seg_lines, retry)
                if breaks is None:
                    return segments  # fail-open：LLM 失败 → 原样放行
                try:
                    tmp = cache_path + '.tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        json.dump({'sig': sig, 'breaks': breaks,
                                   'ts': datetime.now().isoformat(
                                       timespec='seconds')},
                                  f, ensure_ascii=False, indent=1)
                    os.replace(tmp, cache_path)
                except Exception:
                    pass
            if not breaks:
                return segments

            segs = list(segments)
            replaced = 0
            for b in breaks:
                if replaced >= max_replace:
                    break
                try:
                    idx = int(b.get('index'))
                except (TypeError, ValueError, AttributeError):
                    continue
                if not (0 <= idx < len(segs)):
                    continue
                old = segs[idx]
                cands = self._causal_replacement_cands(
                    eps_hl, old, segs, used_global)
                if not cands:
                    self.logger.log(
                        drama_name, 'INFO',
                        f'因果链 V{v_idx+1} 段{idx} 断层'
                        f'（{str(b.get("reason", ""))[:40]}）但同集无未用候选，保留')
                    continue
                new = cands[0]
                if used_global is not None:
                    used_global.discard((old.get('episode'),
                                         round(float(old.get('start', 0)), 1)))
                    used_global.add((new['episode'], round(float(new['start']), 1)))
                self.logger.log(
                    drama_name, 'INFO',
                    f'因果链 V{v_idx+1} 段{idx} 替换：第{old.get("episode")}集'
                    f'[{float(old.get("start", 0)):.0f}s] → 第{new["episode"]}集'
                    f'[{float(new["start"]):.0f}s]'
                    f'（{str(b.get("reason", ""))[:40]}）')
                segs[idx] = {'episode': new['episode'],
                             'start': round(float(new['start']), 1),
                             'duration': round(float(new['dur']), 1)}
                replaced += 1
            return segs
        except Exception as e:
            self.logger.log('system', 'WARN',
                            f'因果链校验异常（原样放行）：{e}')
            return segments

    def _causal_check_llm(self, drama_name, seg_lines, retry=1):
        """调 GLM 因果链校验，返回 breaks 列表；失败/无效 → None（fail-open）。"""
        try:
            ai = AICopyGenerator()
            if not ai.enabled:
                return None
            prompt = self._CAUSAL_PROMPT.format(
                count=len(seg_lines), seg_lines='\n'.join(seg_lines))
            for _ in range(int(retry) + 1):
                try:
                    content = ai.chat_json(prompt)
                except Exception:
                    content = None
                if not content:
                    continue
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    i, j = content.find('{'), content.rfind('}')
                    if i < 0 or j <= i:
                        continue
                    try:
                        data = json.loads(content[i:j + 1])
                    except json.JSONDecodeError:
                        continue
                if not isinstance(data, dict):
                    continue
                breaks = data.get('breaks')
                if isinstance(breaks, list):
                    out = []
                    for b in breaks:
                        if isinstance(b, dict):
                            try:
                                out.append({'index': int(b['index']),
                                            'reason': str(
                                                b.get('reason', ''))[:80]})
                            except (KeyError, TypeError, ValueError):
                                continue
                    return out
                return []
            return None
        except Exception:
            return None

    def _causal_replacement_cands(self, eps_hl, old_seg, segs, used_global):
        """为断层次找替换候选：仅同集未用候选（保守，避免引入新跨集跳切）。

        同集优先保证叙事邻近；同集无未用候选 → []（调用方保留原段）。
        候选必须未在 used_global（维持跨版本零复用）。返回按 score 降序。
        """
        out = []
        for e in eps_hl:
            if e.get('episode') != old_seg.get('episode'):
                continue
            for c in e.get('cands', []):
                try:
                    st = float(c.get('start', 0))
                    dur = float(c.get('dur', c.get('duration', 20)))
                except (TypeError, ValueError):
                    continue
                if dur < self.MIN_SEG_DUR:
                    continue
                if used_global is not None and \
                        (e.get('episode'), round(st, 1)) in used_global:
                    continue
                out.append({'episode': e.get('episode'), 'start': st,
                            'dur': dur, 'score': float(c.get('score', 0))})
        out.sort(key=lambda x: -x['score'])
        return out

    def _nonoverlap_cands(self, llm_cands, ep_dur, seg_dur):
        """方案B：把 LLM 候选（可能互相重叠的窗口）重排为互不重叠的 seg_dur 段。

        从 0 起连续切 [i*seg, (i+1)*seg]，段数 = floor(ep_dur / seg_dur)。
        每段选择与它时间重叠最多的 LLM 候选，避免“高分窗口在第 3 段，
        标签却套到第 1 段”的错位。没有候选重叠的容量补齐段标记为 score=0。
        """
        seg_dur = float(seg_dur)
        out = []
        i = 0
        while (i + 1) * seg_dur <= ep_dur + 0.05:
            st = round(i * seg_dur, 1)
            en = st + seg_dur
            best = None
            best_overlap = 0.0
            for cand in llm_cands:
                try:
                    cst = float(cand.get('start', 0) or 0)
                    cen = (float(cand['end']) if 'end' in cand else
                           cst + float(cand.get('dur', 0) or 0))
                except (TypeError, ValueError):
                    continue
                overlap = max(0.0, min(en, cen) - max(st, cst))
                if overlap > best_overlap:
                    best, best_overlap = cand, overlap
            src = best or {}
            out.append({
                'score': src.get('score', 0),
                'start': st,
                'dur': round(seg_dur, 1),
                'src': src.get('src', 'capacity'),
                'hook_type': src.get('hook_type', ''),
                'reason': (src.get('reason', '') if best else
                           '未命中高光窗口，仅用于容量补齐'),
            })
            i += 1
        return out

    def _capacity_version_count(self, edl_eps, drama_name, config=None):
        """素材容量决定版本数。

        默认版本数 = min(集数目标, 独立素材可支撑版本数)，最少保留 1 版。
        config.version_count 显式设定时：reuse_material=True 按设定（≤8，允许跨版本
        复用）；reuse_material=False 则 clamp 到零复用容量，绝不超容被迫复用。
        """
        rc = self._resolve_cut_config(config)
        cfg = CONFIG['edl']
        seg_dur = float(cfg.get('segment_dur', 20.0))
        target = float(cfg.get('target_dur', 200.0))
        segs_per_version = max(1, int(math.ceil(target / seg_dur)))
        total = 0
        try:
            hl = self._load_episode_highlights(edl_eps, drama_name=drama_name)
            total = sum(len(e.get('cands', [])) for e in hl)
        except Exception as e:
            self.logger.log(drama_name, 'WARN',
                            f'方案B容量计算失败，回退集数判定：{e}')
            total = 0
        auto_desired = self.determine_version_count(len(edl_eps))
        desired = (rc['version_count'] if rc['version_count'] is not None
                   else auto_desired)
        allow_reuse = bool(rc['reuse_material']) or bool(
            cfg.get('allow_controlled_reuse', False))
        # 有效版本数边界 = 用户硬性 [1,8] ∩ CONFIG edl.min_versions/max_versions
        # 旋钮（保留既有可配置性，当前默认 1/8 与硬边界一致）。
        _lo_v = max(CUT_CONFIG_ABS['version_min'],
                    int(cfg.get('min_versions', CUT_CONFIG_ABS['version_min'])))
        _hi_v = min(CUT_CONFIG_ABS['version_max'],
                    int(cfg.get('max_versions', CUT_CONFIG_ABS['version_max'])))
        if _hi_v < _lo_v:
            _hi_v = _lo_v
        if total <= 0:
            return max(_lo_v, min(_hi_v, desired))
        zero_reuse_cap = total // segs_per_version
        if allow_reuse:
            count = max(_lo_v, min(_hi_v, desired))
        else:
            count = max(_lo_v, min(_hi_v, desired, zero_reuse_cap))
        self.logger.log(drama_name, 'INFO',
                        f'方案B容量：素材{total}段 / 每版{segs_per_version}段 '
                        f'零复用上限{zero_reuse_cap}版，集数目标{desired}版 '
                        f'→ 计划{count}版'
                        f'（{"允许受控复用" if allow_reuse else "禁止跨版本复用"}）')
        return count

    # 冲突/悬念词库：LLM 不可用时的兜底打分
    # 冲突词权重 2（情绪爆发），悬念词权重 1（信息揭示）
    CONFLICT_WORDS = ('打脸', '傻', '死', '滚', '敢', '凭什么', '不可能', '疯了',
                      '骗', '恨', '杀', '血', '泪', '离婚', '出轨', '复合', '孩子',
                      '怀孕', '亿', '婚约', '下不来台', '后悔')
    SUSPENSE_WORDS = ('发现', '秘密', '真相', '原来', '竟然', '居然', '突然',
                      '没想到', '终于', '七年', '前妻', '复婚')

    # LLM 高光打分提示词。业界共识（字节 agentkit 短剧高光、DramaClip）是
    # 「输出时间窗 + 分数 + 理由」，理由必须可审计。score 用整数便于跨集比较。
    # 丰富叙事切入角度库：导演方案的 angle / 高光 hook_type 共用。
    # 4 类 32 个真实短剧叙事主题，每版取不同角度 → 版本主题/文案/钩子差异化。
    # GLM 按各集实际剧情匹配角度，不硬套；校验白名单与 prompt 选项同源。
    ARC_ANGLES = [
        # ① 冲突逆袭（26）：11 核心 + 15 扩展
        "逆袭", "打脸", "真相", "反转", "高潮", "泪目",
        "复仇", "觉醒", "扮猪吃虎", "双强", "救赎",
        "时间循环", "读心术", "规则怪谈", "断亲反杀", "摆烂通关",
        "鉴宝捡漏", "风水打脸", "神豪开挂", "系统逆袭", "记忆错位",
        "身份互换", "密室逃脱", "监控盲区", "匿名复仇", "反PUA逆袭",
        # ② 情感关系（39）：21 核心 + 18 扩展
        "甜宠", "虐恋", "误会", "破镜重圆", "替身", "契约婚姻",
        "闪婚", "追妻", "团宠", "先婚后爱", "追妻火葬场",
        "双向奔赴", "隐婚", "娇妻", "姐弟恋", "年下",
        "白月光", "竹马", "死对头", "宿敌", "天降",
        "疯批男主", "偏执霸总", "长嫂如母", "继亲和解", "养恩反哺",
        "妯娌斗法", "师徒虐恋", "忘年之交", "隔代亲救赎", "闺蜜反目",
        "家长里短", "嘴替吐槽", "发疯文学", "社恐追爱", "直女拆台",
        "冤种情侣", "沙雕一家人", "双向暗恋",
        # ③ 身份觉醒（23）：11 核心 + 12 扩展
        "重生", "马甲", "隐藏身份", "千金归来", "弃女", "赘婿",
        "大女主", "复仇归来", "逆天改命", "宫斗", "宅斗",
        "穿书逆袭", "双穿联动", "古物觉醒", "末世重生", "星际大佬",
        "平行世界", "相师下山", "国运觉醒", "非遗守艺人", "退休重启",
        "下岗逆袭", "代驾大佬",
        # ④ 题材类型（34）：12 核心 + 22 扩展
        "悬疑", "惊险", "商战", "职场逆袭", "神医", "战神",
        "豪门", "霸总", "带球跑", "萌宝", "绿茶", "渣男",
        "年代怀旧", "乡村振兴", "民国奇案", "民俗志怪", "AI伦理",
        "外卖侠", "网约车谜案", "快递疑云", "社区网格员", "彩礼反杀",
        "文物说话", "古风美食", "书院日常", "敦煌修复", "戏曲破圈",
        "中医下山", "榫卯匠人", "古画修复", "剧本杀DM", "二手车评估师",
        "入殓师", "宠物侦探",
    ]

    _HIGHLIGHT_PROMPT = (
        "你是短剧高光剪辑师。下面是《{drama}》第{ep}集的台词转写（带时间轴）。\n"
        "判断这一集值不值得被剪进 2-4 分钟的推广成片，并给出这一集里最值得剪的"
        "1-3 个互不重叠的高光窗口。\n\n"
        "台词（[句号] [起-止] 秒）：\n{lines}\n\n"
        "只输出一个 JSON 对象，不要解释、不要 markdown 代码块：\n"
        '{{"windows": ['
        '{{"score": 0到10的整数, "reason": "不超过40字，说清这个窗口真实发生了什么", '
        '"seg_idx": 高光句的句号（0-based，必须对应上方台词行的 [i]）, '
        '"hook_type": "类型"}}, '
        '{{"score": 0到10的整数, "reason": "不超过40字", '
        '"seg_idx": 高光句的句号, "hook_type": "类型"}}]}}\n\n'
        "评分标准：\n"
        "9-10 强烈冲突爆发或关键反转，能当开头钩子\n"
        "7-8  明确冲突或对峙\n"
        "5-6  有推进但不够爆\n"
        "3-4  过渡铺垫\n"
        "0-2  平淡无冲突，不值得剪\n\n"
        "要求：\n"
        "- windows 是数组，按 score 从高到低排序，最多 3 个；同一集内各窗口"
        "必须互不重叠（间隔 ≥5s），各自覆盖完整一轮冲突\n"
        "- 只有 1 个爆点就只给 1 个；没有可剪的就给空数组 windows: []\n"
        "- hook_type 只能是：逆袭、打脸、真相、反转、高潮、泪目、甜宠、虐恋、误会、破镜重圆、替身、契约婚姻、闪婚、追妻、团宠、重生、复仇、觉醒、扮猪吃虎、双强、马甲、隐藏身份、千金归来、弃女、赘婿、悬疑、惊险、商战、职场逆袭、神医、战神、救赎、平淡 之一（按本集真实剧情选最贴切，不得硬套）\n"
        "- seg_idx 必须指向真实存在的台词行（[i] 里的 i），禁止编造句号；"
        "窗口以该句为高光核心，前后范围由系统按句号映射，不要自己算秒数\n"
        "- reason 只描述台词里真实发生的事，不要脑补剧情\n"
        "- 没冲突就老实给低分，不要为了好看而拔高"
    )

    # 导演/编剧弧线方案提示词：读各集高光摘要 → 设计 count 个差异化叙事版本。
    # 每版独立主线：不同 opening_ep（开头钩子集）+ 不同切入角度 + 不同叙事顺序，
    # 保证版本间开头/素材/结构都不相同（解决 V1-V8 用同一段素材、开头雷同）。
    _ARC_PROMPT = (
        "你是短剧导演兼编剧。下面是《{drama}》各集的高光摘要（集号：分数/类型/剧情）。\n"
        "请理解整个故事的主线结构（起因→冲突→升级→真相→高潮→结局），"
        "并设计 {count} 个叙事上相互独立的版本方案。\n\n"
        "集摘要：\n{eps_summary}\n\n"
        "只输出一个 JSON 对象，不要解释、不要 markdown 代码块：\n"
        '{{"versions": ['
        '{{"opening_ep": 集号, "angle": "切入角度", "order_type": "类型"}}, '
        '{{"opening_ep": 集号, "angle": "切入角度", "order_type": "类型"}}]}}\n\n'
        "要求：\n"
        "- versions 是数组，长度 {count}，每版一个，叙事上彼此独立\n"
        "- 每版 opening_ep 必须不同（不同集做开头钩子 → 开头差异化）\n"
        "- opening_ep 优先选高光分 ≥7 的爆点集（真相/高潮/反转/打脸集），别选平淡集\n"
        "- angle 是这一版的叙事切入角度：逆袭、打脸、真相、反转、高潮、泪目、甜宠、虐恋、误会、破镜重圆、替身、契约婚姻、闪婚、追妻、团宠、重生、复仇、觉醒、扮猪吃虎、双强、马甲、隐藏身份、千金归来、弃女、赘婿、悬疑、惊险、商战、职场逆袭、神医、战神、救赎 之一，\n"
        "  与 opening_ep 的剧情类型一致（决定这一版讲哪条故事线）；同一方案内各版 angle 必须互不相同（角度差异化）\n"
        "- order_type 只能是：reverse（开头钩子→闪回前因→顺叙到结局）、\n"
        "  flashback（开头钩子→前因闪回→顺叙）、forward（顺叙全片）之一\n"
        "- 每版叙事必须连贯（先钩子后因果，别跨时间线乱跳）\n"
    )

    # A3 Reviewer 审稿：对导演方案 5 维打分（钩子/结构/反转/节奏/情绪），
    # score < REVIEW_SCORE_MIN 带 feedback 反馈 Director 重试（max_retries=2）。
    _REVIEW_PROMPT = (
        "你是短剧审稿人。下面是《{drama}》的 {count} 个版本方案（开头集/角度/"
        "叙事顺序）与各集高光摘要。请从 5 个维度打分，各 0-1：\n"
        "- 钩子强度：开头钩子是否真的炸（高分集、真冲突，不是平淡集）\n"
        "- 结构连贯：版本叙事是否因果连贯、不跳戏\n"
        "- 反转清晰：是否包含关键反转/真相揭示\n"
        "- 节奏适配：片段节奏是否匹配推广成片（钩子→升级→高潮）\n"
        "- 情绪张力：整体情绪曲线是否先抑后扬、够抓人\n\n"
        "版本方案：\n{plan_summary}\n\n"
        "集摘要：\n{eps_summary}\n\n"
        "只输出一个 JSON 对象：\n"
        '{{"score": 0到1的小数, "feedback": "不超过60字，指出最弱 1-2 个维度的问题与修正方向"}}\n\n'
        "要求：\n"
        "- score 是 5 维平均（钩子/结构/反转/节奏/情绪）\n"
        "- feedback 必须具体可执行（如'版本2 开头集平淡，换到第12集反转'）\n"
        "- 无重大问题的给 score ≥ 0.75；只有明显缺陷才给 <0.65\n"
    )

    _CAUSAL_PROMPT = (
        "你是短剧叙事逻辑审稿人。下面是一版成片的有序片段拼接序列，每段是"
        "某集的一个时间窗（含台词摘要）。请判断相邻片段是否构成因果连贯的"
        "叙事链：前一段的剧情/台词是否自然引出后一段，有无逻辑断层、前后"
        "矛盾、时间线冲突（后段时间早于前段却无闪回交代、台词指涉的人物/"
        "事件前文未出现、情绪或情节突兀跳跃）。\n\n"
        "片段序列（{count} 段）：\n{seg_lines}\n\n"
        "只输出一个 JSON 对象：\n"
        '{{"breaks": [{{"index": 3, "reason": "第3段接第4段断层：…"}}]}}\n\n'
        "说明：\n"
        "- breaks 里 index 表示『从段 index-1 过渡到段 index 时不连贯』（1..count-1；"
        "index=0 表示开头钩子自身突兀）；index 唯一。\n"
        "- 全链连贯则 breaks 为空数组 []。\n"
        "- 只标真正的断层（台词/事件接不上、矛盾、时间倒挂），不要因节奏慢或"
        "情绪平而标。\n"
    )

    # 因果链校验：段序列哈希缓存文件名（out_dir 下），命中不重复调 API。
    CAUSAL_CACHE_PREFIX = 'causal_check_v'

    # A3 审稿通过线（5 维均分）。低于此反馈 Director 重试。
    REVIEW_SCORE_MIN = 0.65
    # A3 审稿重试上限（首版评审 + 最多 2 次带反馈重试）
    REVIEW_MAX_RETRY = 2

    def _keyword_cands(self, raw, ep_dur):
        """兜底打分：冲突/悬念词命中 + 4s 内相邻高分句合并成连续窗口。

        关键词只找到 13/45 集（覆盖率 29%）且有假阳性（"死""钱"在平淡对话也命中），
        所以它只是 LLM 失效时的保底，不是主路径。
        """
        hits = []
        for s in raw:
            text = s.get("text") or ""
            score = sum(1 for w in self.CONFLICT_WORDS if w in text) * 2 + \
                    sum(1 for w in self.SUSPENSE_WORDS if w in text)
            if not score:
                continue
            hits.append([float(s.get("start", 0)),
                         float(s.get("end", float(s.get("start", 0)))),
                         score])
        hits.sort()
        merged = []
        for st, en, sc in hits:
            # 4s 以内的空档视为同一场戏（冲突戏是一串快速来回，不是单句）
            if merged and st - merged[-1][1] <= 4.0:
                merged[-1][1] = max(merged[-1][1], en)
                merged[-1][2] += sc
            else:
                merged.append([st, en, sc])
        cands = []
        for st, en, sc in merged:
            start = max(0.0, st - 2.0)          # 前 2s 铺垫
            dur = en - start + 4.0              # 后 4s 反应
            dur = min(dur, max(0.0, ep_dur - start))
            if dur < 15.0:
                continue
            cands.append({"score": sc, "start": round(start, 1),
                          "dur": round(min(dur, 60.0), 1), "src": "kw",
                          "hook_type": "", "reason": "关键词兜底"})
        cands.sort(key=lambda c: -c["score"])
        return cands

    def _score_episode_llm(self, ai, drama_name, ep_num, raw, ep_dur):
        """LLM 给单集打高光分，返回 1-3 个互不重叠候选窗口（按 score 降序）。

        候选窗口用于版本差异化：V1 取候选0、V2 取候选1……避免所有版本
        用同一个高光段。LLM 无效返回 []，由调用方回退关键词。
        """
        lines = "\n".join(
            f"[{i}] [{float(s.get('start', 0)):.1f}-"
            f"{float(s.get('end', 0)):.1f}] "
            f"{s.get('text', '')}" for i, s in enumerate(raw))
        if len(lines) > 3000:      # 防御：超长截断，避免撞上下文上限
            lines = lines[:3000]
        content = ai.chat_json(self._HIGHLIGHT_PROMPT.format(
            drama=drama_name or "", ep=ep_num, lines=lines))
        if not content:
            return []
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # LLM 偶尔多嘴包一层文本，尽力抢救第一个 {...}
            i, j = content.find('{'), content.rfind('}')
            if i < 0 or j <= i:
                return []
            try:
                data = json.loads(content[i:j + 1])
            except json.JSONDecodeError:
                return []
        # 兼容旧格式：LLM 返回单窗口 {score,...} 而非 {windows:[...]}
        if isinstance(data, dict) and 'score' in data and ('start' in data
                                                           or 'seg_idx' in data):
            data = {'windows': [data]}
        windows = data.get('windows') if isinstance(data, dict) else None
        if not isinstance(windows, list) or not windows:
            return []
        cands = []
        for w in windows[:3]:
            try:
                score = max(0, min(10, int(round(float(w['score'])))))
                # A5 词索引防漂移：优先用 seg_idx 映射该句时间戳（LLM 不编秒数）；
                # 无 seg_idx 回退旧格式秒数（兼容旧缓存/旧 LLM）。
                seg_idx = None
                try:
                    seg_idx = int(w['seg_idx'])
                except (KeyError, TypeError, ValueError):
                    seg_idx = None
                if seg_idx is not None and 0 <= seg_idx < len(raw):
                    start = float(raw[seg_idx].get('start', 0))
                    end = float(raw[seg_idx].get('end', start))
                else:
                    start = float(w['start'])
                    end = float(w['end'])
            except (KeyError, TypeError, ValueError):
                continue
            # 窗口必须落在本集时间轴内，否则 LLM 在编时间码
            if not (0 <= start < end <= ep_dur + 0.5):
                continue
            # LLM 常按精确台词边界给短窗口（5-12s）。对外扩成可观看的完整
            # 冲突单元；否则 4 分钟会出现十几个 15s 碎段。
            MIN_WIN = max(self.HIGHLIGHT_WINDOW_MIN, self.MIN_SEG_DUR)
            need = MIN_WIN - (end - start)
            if need > 0:
                add_l = min(need, start)
                add_r = min(need - add_l, max(0.0, ep_dur - end))
                start -= add_l
                end += add_r
            if end - start < 8.0:      # 整集都不到 8s，没法剪
                continue
            cands.append({
                "score": score,
                "reason": str(w.get("reason", ""))[:60],
                "start": round(start, 1),
                "end": round(end, 1),
                "hook_type": str(w.get("hook_type", "高潮"))[:4],
            })
        cands.sort(key=lambda c: -c["score"])
        return cands

    def _save_highlight_cache(self, cache_path, scores):
        """高光打分结果落盘，按剧缓存。重跑不重复调用 API。"""
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            tmp = cache_path + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(scores, f, ensure_ascii=False, indent=1)
            os.replace(tmp, cache_path)
        except Exception as e:
            self.logger.log('system', 'WARN', f'高光打分缓存写入失败：{e}')

    def _load_episode_highlights(self, edl_eps, drama_name=None, force_llm=False):
        """逐集高能窗口候选。主路径 LLM 打分（reason 落日志可审计），
        单集失败回退关键词，转写全缺返回空（由生成器退回随机逻辑）。

        转写文件约定：<src_dir>/transcribe/EP<集文件名去掉.mp4>.json，内容形如
        [{"start":0.0,"end":3.28,"text":"..."}, ...]，时间戳是单集内的相对秒。
        """
        if not edl_eps:
            return []
        self._emit_cut_progress('highlight', 28, '读取 ASR 与高光候选缓存')
        hi_dir = os.path.join(os.path.dirname(os.path.abspath(edl_eps[0])),
                              "transcribe")
        cache_path = os.path.join(hi_dir, "highlight_scores.json")

        # 1) 缓存（按剧持久化，第二个版本起全命中）
        cached = {}
        if not force_llm:
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    disk = json.load(f)
                if isinstance(disk, dict):
                    cached = {int(k): v for k, v in disk.items()
                              if isinstance(v, dict)
                              and ('score' in v or 'cands' in v)}
            except Exception:
                pass

        # 2) 读逐集转写
        eps_data = []
        for ep_file in edl_eps:
            jpath = os.path.join(hi_dir, "EP" +
                                  os.path.splitext(os.path.basename(ep_file))[0] + ".json")
            if not os.path.exists(jpath):
                continue
            try:
                with open(jpath, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
            except Exception:
                continue
            if not isinstance(raw, list) or not raw:
                continue
            # ep_dur 用源文件真实时长（probe，与渲染端一致）而非转写 last end：
            # 转写可能错位/异常（如某集只剩 2 段、end=59.98s 而源文件仅 36.6s），
            # 用转写时长做 clamp 基准会让片段规划超源文件时长，渲染端 probe
            # 校验必然失败（V6-V8 因此失败）。probe 失败才回退转写 last end。
            eps_data.append((self._get_episode_num(ep_file), ep_file, raw,
                             self._episode_real_duration(ep_file, raw)))
        if not eps_data:
            return []

        # 3) LLM 打分（只打缓存里没有的）
        todo = [e for e in eps_data if e[0] not in cached]
        if todo:
            ai = AICopyGenerator()
            if ai.enabled:
                hits = 0
                for done_idx, (ep_num, ep_file, raw, ep_dur) in enumerate(todo, 1):
                    pct = 28 + int(10 * done_idx / max(1, len(todo)))
                    self._emit_cut_progress(
                        'highlight', pct,
                        f'高光分析 {done_idx}/{len(todo)}：第{ep_num}集',
                        episode=ep_num)
                    cands = self._score_episode_llm(ai, drama_name, ep_num, raw, ep_dur)
                    if cands:
                        cached[ep_num] = {"cands": cands, "src": "llm"}
                        hits += 1
                        top = cands[0]
                        self.logger.log(
                            'system', 'INFO',
                            f'高光打分 第{ep_num}集：{top["score"]}分 '
                            f'[{top["hook_type"]}] {top["start"]}-{top["end"]}s '
                            f'｜ {top["reason"]}（{len(cands)}候选）')
                    else:
                        self.logger.log(
                            'system', 'WARN',
                            f'高光打分 第{ep_num}集：LLM 未返回有效结果，该集走关键词兜底')
                self._save_highlight_cache(cache_path, cached)
                self.logger.log('system', 'INFO',
                                f'高光打分完成：{hits}/{len(todo)} 集由 LLM 判定，'
                                f'结果已缓存 {cache_path}')
            else:
                self.logger.log('system', 'WARN',
                                '高光打分：AI 未配置，全部走关键词兜底')
        else:
            self.logger.log('system', 'INFO',
                            f'高光打分：{len(eps_data)} 集全部命中缓存，不调用 API')
        self._emit_cut_progress('highlight', 38,
                                f'高光候选就绪：{len(eps_data)} 集')

        # 4) 组装候选窗口（每集 1-N 个：LLM 多候选，版本轮换用；关键词兜底）
        out = []
        for ep_num, ep_file, raw, ep_dur in eps_data:
            s = cached.get(ep_num)
            cands = []
            if s:
                if isinstance(s, dict) and isinstance(s.get('cands'), list):
                    raw_cands = [c for c in s['cands']
                                 if isinstance(c, dict) and 'score' in c]
                    src = s.get('src', 'llm')
                elif isinstance(s, dict) and 'score' in s:
                    raw_cands = [s]      # 旧单候选缓存兼容
                    src = s.get('src', 'llm')
                else:
                    raw_cands = []
                    src = 'llm'
                for c in raw_cands:
                    try:
                        if 'dur' in c:                   # 关键词格式
                            st = float(c.get('start', 0.0))
                            en = st + float(c['dur'])
                        elif 'end' in c and 'start' in c:  # LLM 格式
                            st = float(c['start'])
                            en = float(c['end'])
                        else:
                            continue
                    except (TypeError, ValueError):
                        continue
                    st = max(0.0, min(st, ep_dur))
                    en = max(st, min(en, ep_dur))

                    # 旧缓存可能保存 A5 修复前的 5-12s 短句窗口。A5 新打分
                    # 会在写入前扩到 HIGHLIGHT_WINDOW_MIN，但缓存命中不会重打分；这里
                    # 再做一次兼容归一化，否则短候选仍会被 add() 全部拒绝。
                    target_min = max(self.HIGHLIGHT_WINDOW_MIN,
                                     self.MIN_SEG_DUR)
                    need = max(0.0, target_min - (en - st))
                    if need > 0:
                        add_l = min(need, st)
                        add_r = min(need - add_l, max(0.0, ep_dur - en))
                        st -= add_l
                        en += add_r
                    if en - st < 8.0:
                        continue
                    cands.append({"score": c['score'], "start": round(st, 1),
                                  "dur": round(en - st, 1),
                                  "src": c.get('src', src),
                                  "hook_type": c.get("hook_type", ""),
                                  "reason": c.get("reason", "")})
            if not cands:
                cands = self._keyword_cands(raw, ep_dur)
            # 方案B：LLM/关键词候选重排为互不重叠的 segment_dur 段（每集 1-N 段），
            # 保证跨版本零复用有足够的、不重叠的独立窗口可用。
            if cands and CONFIG['edl'].get('non_overlap'):
                cands = self._nonoverlap_cands(
                    cands, ep_dur,
                    float(CONFIG['edl'].get('segment_dur', 20.0)))
            if cands:
                out.append({"episode": ep_num, "path": ep_file,
                            "dur": ep_dur, "cands": cands})
        return out

    def _generate_versions_via_llm(self, ai, eps_summary, count, drama_name,
                                   eps_hl, feedback=None):
        """Director GLM 生成版本方案（带可选审稿 feedback 修正重试）。

        失败/无效 → None（调用方回退默认方案）。feedback 非空时把评审
        反馈追加进 prompt，要求 Director 按反馈修正（A3 重试闭环）。
        """
        try:
            prompt = self._ARC_PROMPT.format(
                drama=drama_name or "", count=count, eps_summary=eps_summary)
            if feedback:
                prompt += ("\n\n评审反馈（上一版方案未通过，必须按此修正）：\n"
                           + feedback)
            content = ai.chat_json(prompt)
            if not content:
                return None
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                i, j = content.find('{'), content.rfind('}')
                if i < 0 or j <= i:
                    return None
                try:
                    data = json.loads(content[i:j + 1])
                except json.JSONDecodeError:
                    return None
            versions = self._validate_arc_plan(data, eps_hl, count)
            return versions if versions and len(versions) >= count else None
        except Exception:
            return None

    def _review_arc_plan(self, ai, versions, eps_hl, drama_name):
        """Reviewer GLM 5 维审稿（钩子/结构/反转/节奏/情绪）。

        Returns: (score 0-1, feedback str)；失败/无效 → (None, None)（放行
        不重试——宁可接受首版也不让审稿故障阻塞切片）。
        """
        try:
            plan_summary = "\n".join(
                f"V{i + 1}: 第{v['opening_ep']}集/{v['angle']}/"
                f"{v['order_type']}" for i, v in enumerate(versions))
            eps_summary = "\n".join(
                f"第{e['episode']}集：{e['cands'][0]['score']}分 "
                f"[{e['cands'][0].get('hook_type', '')}] "
                f"{e['cands'][0].get('reason', '')}" for e in eps_hl)
            if len(eps_summary) > 4000:
                eps_summary = eps_summary[:4000]
            content = ai.chat_json(self._REVIEW_PROMPT.format(
                drama=drama_name or "", count=len(versions),
                plan_summary=plan_summary, eps_summary=eps_summary))
            if not content:
                return None, None
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                i, j = content.find('{'), content.rfind('}')
                if i < 0 or j <= i:
                    return None, None
                try:
                    data = json.loads(content[i:j + 1])
                except json.JSONDecodeError:
                    return None, None
            score = max(0.0, min(1.0, float(data['score'])))
            feedback = str(data.get('feedback', ''))[:60]
            return score, feedback
        except Exception:
            return None, None

    def _analyze_drama_arc(self, eps_hl, count, drama_name=None):
        """导演/编剧 GLM：读各集高光摘要 → 设计 count 个差异化叙事版本方案。

        返回 versions 列表，每项 {opening_ep, angle, order_type}：
        - opening_ep：本版开头钩子所在集（不同版不同集 → 开头差异化）
        - angle：本版叙事切入角度（32 类丰富主题，见 ARC_ANGLES）
        - order_type：叙事顺序（reverse/flashback/forward）

        GLM 失败或未配置 → 默认方案（按高光分降序 top-count 集轮换做开头，
        angle 取该集 hook_type，order_type=reverse），保证版本仍差异化开出。
        按剧缓存 <src>/transcribe/arc_plan.json（第二版起 0 API）。
        """
        if not eps_hl:
            return []

        def default_plan():
            ordered = sorted(eps_hl, key=lambda e: -e['cands'][0]['score'])
            versions = []
            used_angles = set()
            orders = ['reverse', 'flashback', 'forward']
            valid = set(self.ARC_ANGLES)
            for i in range(min(count, len(ordered))):
                c = ordered[i]['cands'][0]
                angle = c.get('hook_type') or ''
                # 候选类型合法且未用 → 直接用；否则从角度库取一个未用的（保证差异化）
                if angle not in valid or angle in used_angles:
                    angle = next((a for a in self.ARC_ANGLES
                                  if a not in used_angles), '高潮')
                used_angles.add(angle)
                versions.append({
                    "opening_ep": ordered[i]['episode'],
                    "angle": angle,
                    "order_type": orders[i % len(orders)],
                })
            return versions

        cache_path = os.path.join(
            os.path.dirname(os.path.abspath(eps_hl[0].get('path', ''))),
            'transcribe', 'arc_plan.json')
        try:
            if os.path.exists(cache_path):
                with open(cache_path, 'r', encoding='utf-8') as f:
                    disk = json.load(f)
                if isinstance(disk, dict) and isinstance(disk.get('versions'), list):
                    cached = self._validate_arc_plan(disk, eps_hl, count)
                    if cached and len(cached) >= count:
                        return cached[:count]
        except Exception:
            pass

        ai = AICopyGenerator()
        if not ai.enabled:
            return default_plan()
        eps_summary = "\n".join(
            f"第{e['episode']}集：{e['cands'][0]['score']}分 "
            f"[{e['cands'][0].get('hook_type', '')}] "
            f"{e['cands'][0].get('reason', '')}"
            for e in eps_hl)
        if len(eps_summary) > 4000:
            eps_summary = eps_summary[:4000]
        try:
            versions = self._generate_versions_via_llm(
                ai, eps_summary, count, drama_name, eps_hl)
            if versions:
                # A3 Reviewer 审稿-重试闭环：5 维打分 < REVIEW_SCORE_MIN
                # → 带 feedback 反馈 Director 重试（最多 REVIEW_MAX_RETRY 次）。
                # 审稿失败（None）保守放行——不因审稿故障阻塞切片。
                score = None
                for attempt in range(self.REVIEW_MAX_RETRY + 1):
                    score, feedback = self._review_arc_plan(
                        ai, versions, eps_hl, drama_name)
                    if score is None or score >= self.REVIEW_SCORE_MIN:
                        break
                    if attempt >= self.REVIEW_MAX_RETRY:
                        # 已耗尽重试次数（REVIEW_MAX_RETRY=2 → 初始+2 重试），
                        # 接受当前方案，不再发起重试。
                        break
                    self.logger.log(
                        'system', 'WARN',
                        f'审稿未过（{score:.2f} < {self.REVIEW_SCORE_MIN}），'
                        f'Director 重试 {attempt + 1}：{feedback}')
                    versions = self._generate_versions_via_llm(
                        ai, eps_summary, count, drama_name, eps_hl,
                        feedback=feedback)
                    if not versions:
                        break
                if score is None:
                    self.logger.log(
                        'system', 'WARN', '审稿 GLM 失败，接受当前方案（放行）')
                elif score < self.REVIEW_SCORE_MIN:
                    self.logger.log(
                        'system', 'WARN',
                        f'审稿重试耗尽，接受当前方案（{score:.2f} < '
                        f'{self.REVIEW_SCORE_MIN}，宁可放行不阻塞）')
                else:
                    self.logger.log(
                        'system', 'INFO',
                        f'审稿通过（{score:.2f} ≥ {self.REVIEW_SCORE_MIN}）')
                try:
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    tmp = cache_path + ".tmp"
                    with open(tmp, 'w', encoding='utf-8') as f:
                        json.dump({'versions': versions}, f,
                                  ensure_ascii=False, indent=1)
                    os.replace(tmp, cache_path)
                except Exception:
                    pass
                self.logger.log(
                    'system', 'INFO',
                    f'导演弧线方案生成：{len(versions)}版 '
                    f'（开头集 {[v["opening_ep"] for v in versions]}）')
                return versions
        except Exception as e:
            self.logger.log('system', 'WARN',
                            f'导演弧线方案 GLM 失败，走默认方案：{e}')
        return default_plan()

    def _validate_arc_plan(self, data, eps_hl, count):
        """校验 GLM 弧线方案：opening_ep 必须互不相同、在有效集内、angle/order_type 合法。"""
        if not isinstance(data, dict) or not isinstance(data.get('versions'), list):
            return None
        valid_eps = {e['episode'] for e in eps_hl}
        angles = set(self.ARC_ANGLES) | {'平淡'}
        orders = {'reverse', 'flashback', 'forward'}
        versions = []
        used = set()          # 已用开头集（opening_ep 互异）
        used_angles = set()   # P2: 已用角度（同方案内 angle 必须互不相同）
        for v in data['versions'][:count]:
            if not isinstance(v, dict):
                continue
            try:
                ep = int(v['opening_ep'])
            except (KeyError, TypeError, ValueError):
                continue
            if ep not in valid_eps or ep in used:
                continue
            angle = str(v.get('angle') or '')
            if angle not in angles:
                angle = '高潮'
            # P2: angle 已用 → 从池取一个未用的（确定性兜底），保证版本角度差异化
            if angle in used_angles:
                angle = next((a for a in self.ARC_ANGLES if a not in used_angles), None)
                if angle is None:
                    continue  # 池已无未用角度，跳过该版（版本不足 → 触发重生成）
            order_type = str(v.get('order_type') or 'reverse')
            if order_type not in orders:
                order_type = 'reverse'
            versions.append({'opening_ep': ep, 'angle': angle,
                             'order_type': order_type})
            used.add(ep)
            used_angles.add(angle)
        return versions if len(versions) >= count else None

    def _build_arc_order(self, eps_hl, opening_ep, order_type='reverse'):
        """按导演方案生成叙事集序列（钩子集在前，其余按因果/时间顺序）。

        保证剧情连贯不跳戏：钩子集 → 前因集（<opening，倒序闪回，近因到远因）
        → 后果集（>opening，顺叙到结局）。forward 则钩子 + 全时间顺序。
        """
        eps = sorted(e['episode'] for e in eps_hl)
        if not eps:
            return []
        head = [opening_ep] if opening_ep in eps else []
        rest = [e for e in eps if e != opening_ep]
        before = sorted([e for e in rest if e < opening_ep])
        after = sorted([e for e in rest if e > opening_ep])
        if order_type == 'forward':
            return head + before + after
        return head + list(reversed(before)) + after

    # A1 场景边界缓存：key=(绝对路径, mtime) → 边界列表；失败/禁用 → []
    # 类属性（可变 dict）跨实例共享，key 含 mtime，无陈旧风险。
    _SCENE_CACHE = {}

    def _scene_boundaries(self, src_path):
        """ffmpeg scene 检测镜头边界（A1）。返回边界时间列表（秒）。

        跨版本缓存（同一源文件只检测一次）。失败或禁用 → []（放行不吸附），
        绝不因吸附故障阻塞切片。
        """
        snap = CONFIG.get('scene_snap', {})
        if not snap.get('enabled', True):
            return []
        if not src_path or not os.path.exists(src_path):
            return []
        try:
            key = (os.path.abspath(src_path),
                   os.path.getmtime(src_path))
            if key in self._SCENE_CACHE:
                return self._SCENE_CACHE[key]
            cmd = [
                CONFIG['ffmpeg_path'], '-hide_banner', '-i', src_path,
                '-vf', "select='gt(scene,{})',showinfo".format(
                    float(snap.get('threshold', 0.30))),
                '-f', 'null', '-',
            ]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace',
                               timeout=180)
            boundaries = []
            for line in (r.stderr or '').splitlines():
                m = re.search(r'pts_time:(\d+(?:\.\d+)?)', line)
                if m:
                    t = float(m.group(1))
                    if t > 0.05:      # 忽略 t≈0 的伪边界
                        boundaries.append(round(t, 2))
            self._SCENE_CACHE[key] = boundaries
            return boundaries
        except Exception:
            return []

    def _snap_scene_start(self, start, dur, total, boundaries, tol=None):
        """把切点 start 吸附到最近镜头边界（A1），clamp 保证 end ≤ 源时长。

        吸附方向优先就近；若向后吸附会超界（end > total），只允许向前
        （不超界），向前也无候选则保持原值。boundaries 空 → 原值。
        """
        if tol is None:
            tol = float(CONFIG.get('scene_snap', {}).get('tol', 0.15))
        if not boundaries or not total or start is None:
            return start
        best, best_d = None, None
        for b in boundaries:
            d = abs(b - start)
            if d <= tol and (best_d is None or d < best_d):
                best, best_d = b, d
        if best is None:
            return start
        # clamp：向后吸附会超源时长 → 只允许向前；向前无候选则保持原值
        if best + dur > total - 0.05:
            prev = [b for b in boundaries
                    if b <= start and b + dur <= total - 0.05]
            return round(max(prev), 2) if prev else start
        # 边界已 round(t,2)（2 位精度）；吸附结果保留 2 位，绝不再降 1 位：
        # round(2.05,1) 因浮点表示（2.04999...）会舍成 2.0，把吸附抹掉。
        return round(best, 2)

    def _snap_segment_start(self, ep_num, start, dur, eps_hl):
        """按源文件 scene 边界吸附切点（A1）。eps_hl 提供 path/dur。

        检测失败或禁用 → 原值。返回吸附后的 start。
        """
        e = next((x for x in eps_hl if x['episode'] == ep_num), None)
        if e is None or not e.get('path'):
            return start
        boundaries = self._scene_boundaries(e['path'])
        if not boundaries:
            return start
        return self._snap_scene_start(start, dur, e.get('dur', 0),
                                      boundaries)

    def _generate_reverse_segments(self, edl_eps, v_idx, drama_name=None,
                                   plan=None, used_global=None, config=None):
        """倒叙结构：开头上钩子，随后按叙事顺序铺开每集的高能窗口。

        高能点主路径是 LLM 逐集打分（带理由，落日志可审计），关键词兜底。

        plan（导演方案）驱动版本差异化：
        - 开头钩子 = plan[v_idx].opening_ep 所在集（不同版不同集 → 开头不同）
        - 该集候选轮换 = cands[v_idx % len(cands)]（不同版不同候选段）
        - 叙事顺序 = _build_arc_order（钩子→前因闪回→顺叙结局，剧情连贯不跳戏）
        无 plan 回退原逻辑（全局最高分钩子 + 集号逆序）。

        used_global（方案B跨版本去重）：跨版本共享的 (集,起点) 已用集合；
        任何版本都跳过已用段 → 版本间零复用同一个素材片段。None 关闭。

        config：本剧切制参数（时长预算用 config.min_dur/max_dur）。
        """
        eps_hl = self._load_episode_highlights(edl_eps, drama_name=drama_name)
        # 剧情驱动时长：按本版导演方案计算规划预算（随机兜底路径也用它）
        self._active_seg_target = self._compute_version_budget(
            plan, v_idx, eps_hl, config=config)
        if not eps_hl:
            # 没有 ASR 转写（未跑通或素材缺转写）：退回随机逻辑，保证版本开得出
            return self._generate_random_segments(edl_eps, v_idx,
                                                  used_global=used_global)

        segments = []

        def add(ep_num, start, dur, reuse=False):
            """按累计时长预算追加片段；预算耗尽返回 False。
            reuse=True（P4 方案1受控复用）：忽略全局已用集，允许次轮版本复用首轮素材
            （调用方已在 fill-up 里按 reuse_ratio 限额，本函数只管落地）。"""
            left = self._seg_budget_left(segments)
            if left <= self.MIN_SEG_DUR:
                return False
            d = round(min(dur, left), 1)
            if d < self.MIN_SEG_DUR:
                return False
            s = round(max(0.0, start), 1)
            # A1 镜头边界吸附：转写 start 是音频起点，常带 1-3s 前一镜头；
            # 吸附到最近 scene 边界修复半帧切。检测失败保守放行（不吸附）。
            s = self._snap_segment_start(ep_num, s, d, eps_hl)
            if s is None:   # 防御：吸附异常/类型推断，绝不把 None 写进片段
                s = 0.0
            # 方案B：跨版本零复用——同一 (集,起点) 全局只允许用一次（吸附后键）；
            # reuse=True 跳过该检查（受控复用由调用方限额，避免整版复刻）。
            if used_global is not None and not reuse:
                key = (ep_num, round(float(s), 1))
                if key in used_global:
                    return False
                used_global.add(key)
            segments.append({
                "episode": ep_num,
                "start": s,
                "duration": d,
            })
            return True

        # 1. 开头钩子。优先取导演方案指定的 opening_ep（版本差异化）；
        #    无方案回退原逻辑（结局集 ≥ 全局最高 70% 锁结局，否则用最炸）。
        vplan = plan[v_idx] if (plan and v_idx < len(plan)) else None
        opening = None
        if vplan:
            opening = next(
                (e for e in eps_hl if e["episode"] == vplan["opening_ep"]), None)
        if opening is None:
            pool = [e for e in eps_hl if e["cands"][0].get("src") == "llm"] or eps_hl
            best = max(pool, key=lambda e: e["cands"][0]["score"])
            last_ep = max(e["episode"] for e in eps_hl)
            last = next((e for e in eps_hl if e["episode"] == last_ep), None)
            opening, ratio = best, 0.0
            if (last is not None and last["cands"][0].get("src") == "llm"
                    and best["cands"][0]["score"] > 0):
                ratio = last["cands"][0]["score"] / best["cands"][0]["score"]
                if ratio >= 0.7:
                    opening = last
        # 候选轮换：优先匹配本版 angle 的实际 hook_type；没有匹配标签时再按
        # 版本偏移轮换。这样 angle 不只是标题标签，也会影响开头素材选择。
        opening_cands = opening["cands"]
        if vplan:
            matched = [c for c in opening_cands
                       if c.get('hook_type') == vplan.get('angle')]
            if matched:
                opening_cands = matched
        oc = opening_cands[v_idx % len(opening_cands)]
        # 方案B：开头钩子段被全局（其他版本）占用 → 顺延该集下一个未用段，
        # 保证每个版本的开头钩子都是独立的素材片段。
        if used_global is not None and len(opening["cands"]) > 1:
            for c2 in opening_cands:
                if (opening["episode"], round(float(c2["start"]), 1)) not in used_global:
                    oc = c2
                    break
        if vplan:
            self.logger.log(
                'system', 'INFO',
                f'开头钩子：第{opening["episode"]}集 {oc["score"]}分 '
                f'[{oc.get("hook_type", "")}] {oc.get("reason", "")}'
                f'（方案v{v_idx + 1} {vplan["angle"]}，候选{v_idx % len(opening_cands) + 1}）')
        else:
            self.logger.log(
                'system', 'INFO',
                f'开头钩子：第{opening["episode"]}集 {oc["score"]}分 '
                f'[{oc.get("hook_type", "")}] {oc.get("reason", "")}')
        if not add(opening["episode"], oc["start"], oc["dur"]):
            # P4 方案1：零复用失败（该开头段已被前面版本占用）时受控复用兜底，
            # 保证次轮版本也有开头钩子；预算耗尽则仍失败（reuse 也救不了）。
            # 兜底须受 allow_controlled_reuse 门控：默认 False（禁止跨版本复用）
            # 时保持绝对零复用，不因开头占用而强制复刻（否则违背用户去重配置）。
            allow_reuse = bool(CONFIG['edl'].get('allow_controlled_reuse', False))
            if allow_reuse and not add(opening["episode"], oc["start"],
                                       oc["dur"], reuse=True):
                # P2-3: LLM 候选只过滤 dur>=8.0，但 add 要求 >=MIN_SEG_DUR(15s)，
                # 8-15s 候选会静默丢失导致开头退化为非计划集——显式告警不再静默。
                self.logger.log('system', 'WARN',
                                f'开头钩子第{opening["episode"]}集 {oc["start"]}s/{oc["dur"]}s '
                                f'未入段（<{self.MIN_SEG_DUR}s 最短段或预算耗尽），开头钩子跳过')
            elif not allow_reuse:
                self.logger.log('system', 'INFO',
                                f'开头钩子第{opening["episode"]}集 {oc["start"]}s/{oc["dur"]}s '
                                f'已被占用且禁止跨版本复用，跳过（保持零复用）')

        # 2. 叙事顺序：导演方案 order_type（钩子→前因闪回→顺叙结局）；
        #    无方案回退集号逆序（原逻辑）。
        used = {(opening["episode"], oc["start"])}
        used_eps = {opening["episode"]}   # 本版已用集（补足阶段避免同集二次出现）
        if vplan:
            order = self._build_arc_order(eps_hl, opening["episode"],
                                          vplan.get('order_type', 'reverse'))
        else:
            order = [e["episode"] for e in
                     sorted(eps_hl, key=lambda x: -x["episode"])]
        for e_num in order:
            if e_num == opening["episode"]:
                continue
            if self._seg_budget_left(segments) <= self.MIN_SEG_DUR:
                break
            e = next((x for x in eps_hl if x["episode"] == e_num), None)
            if e is None or not e["cands"]:
                continue
            # 每集候选轮换：v_idx 偏移取该集候选，被本地/全局占用则顺延下一个未用候选
            cands = e["cands"]
            c = cands[v_idx % len(cands)]
            if (e_num, c["start"]) in used or (
                    used_global is not None
                    and (e_num, round(float(c["start"]), 1)) in used_global):
                picked = None
                for c2 in cands:
                    if (e_num, c2["start"]) in used:
                        continue
                    if (used_global is not None
                            and (e_num, round(float(c2["start"]), 1)) in used_global):
                        continue
                    picked = c2
                    break
                if picked is None:
                    continue
                c = picked
            used.add((e_num, c["start"]))
            used_eps.add(e_num)
            if add(e_num, c["start"], c["dur"]):
                continue

        # 方案B补足：arc 遍历完但预算未满（如最后一版素材被前面版本分散占用），
        # 从剩余全局未用段按分数降序补足到目标时长，避免末版时长跌破 3 分钟。
        # 两遍：先取本版未出现过的集（避免同集二次出现观感重复），无果再放宽。
        # 仅方案B（dedup 启用，used_global 非 None）生效；旧逻辑/测试不受影响。
        if (CONFIG['edl'].get('non_overlap') and used_global is not None
                and self._seg_budget_left(segments) > self.MIN_SEG_DUR):
            ranked = sorted(eps_hl,
                            key=lambda x: -max((c['score'] for c in x['cands']),
                                               default=0))
            # P4 方案1：受控复用限额——按目标段数而非当前段数计算，
            # 保证次轮版本能补足到目标时长（当前 len(segments) 可能只有开头 1 段，
            # 若按它算 max_reused 会=0，导致复用版本无法补足到 3 分钟）。
            # 0.9 让复用版本补足到 ~200s（渲染后 ~190s，安全高于 180s 门限，
            # 避免重蹈 V3 171.6s<180s 被拒的覆辙）；R9 内容去重负责剔除克隆。
            allow_controlled_reuse = bool(
                CONFIG['edl'].get('allow_controlled_reuse', False))
            reuse_ratio = float(CONFIG['edl'].get('reuse_ratio', 0.0))
            target_segs = max(1, int(math.ceil(
                float(CONFIG['edl'].get('target_dur', 200.0)) /
                float(CONFIG['edl'].get('segment_dur', 20.0)))))
            max_reused = int(reuse_ratio * target_segs)
            reused = 0
            # 两遍零复用（先本版未用集、再放宽同集）；只有显式开启配置时
            # 才增加第三遍受控复用，避免默认用 90% 已用片段硬凑版本数。
            passes = [(False, False), (True, False)]
            if allow_controlled_reuse and max_reused > 0:
                passes.append((True, True))
            for allow_reused_ep, allow_global_reuse in passes:
                if self._seg_budget_left(segments) <= self.MIN_SEG_DUR:
                    break
                for e in ranked:
                    if self._seg_budget_left(segments) <= self.MIN_SEG_DUR:
                        break
                    if e['episode'] == opening['episode']:
                        continue
                    if e['episode'] in used_eps and not allow_reused_ep:
                        continue
                    for c in e['cands']:
                        key = (e['episode'], round(float(c['start']), 1))
                        is_reuse = (used_global is not None and key in used_global)
                        if is_reuse and not allow_global_reuse:
                            continue
                        if is_reuse and reused >= max_reused:
                            continue
                        if add(e['episode'], c['start'], c['dur'],
                               reuse=allow_global_reuse):
                            used_eps.add(e['episode'])
                            if is_reuse:
                                reused += 1
                            break

        return segments

    def _generate_random_segments(self, edl_eps, v_idx, used_global=None):
        """生成随机结构的segments - 检查时长有效性"""
        segments = []
        
        eps = list(edl_eps)
        random.shuffle(eps)
        eps = eps[:10]  # 最多10集
        
        for ep_file in eps:
            duration = self._get_video_duration(ep_file)
            # 跳过损坏或过短的文件
            if duration < 30:
                continue
            
            num_segments = random.randint(2, 3)
            start = random.uniform(5, 15)
            ep_num = self._get_episode_num(ep_file)
            
            for _ in range(num_segments):
                # 累计时长预算用完就停手，保证成片落在 210-300s 窗口内
                left = self._seg_budget_left(segments)
                if left <= self.MIN_SEG_DUR:
                    break
                # 确保片段不超过剩余时长
                remaining = duration - start - 5
                if remaining < 15:
                    break
                # 先按预算 clamp 再 round：round 可能向上舍入，会让累计值悄悄越过预算
                seg_duration = round(min(random.uniform(30, 75), remaining, left), 1)
                if seg_duration < 15:
                    break
                # A1 镜头边界吸附（随机模式无转写，直接用源文件 scene 边界）
                s = start
                boundaries = self._scene_boundaries(ep_file)
                if boundaries:
                    s = self._snap_scene_start(start, seg_duration, duration,
                                               boundaries)
                # 方案B：跨版本零复用——跳过全局已用 (集,起点) 段
                if used_global is not None:
                    key = (ep_num, round(float(s or 0.0), 1))
                    if key in used_global:
                        start += seg_duration + random.uniform(1, 3)
                        continue
                    used_global.add(key)
                segments.append({
                    "episode": ep_num,
                    "start": round(float(s or 0.0), 1),
                    "duration": round(seg_duration, 1)
                })
                start += seg_duration + random.uniform(1, 3)

        return segments

    def _get_episode_num(self, filepath):
        """从文件名获取集数 - 兼容两种格式"""
        fname = os.path.basename(filepath)
        ep = self.parse_episode_number(fname)
        return ep if ep is not None else 1

    def _update_config_artifacts(self, out_dir, version, key, value):
        """回写 edl_config_v{version}.json 的 artifacts[key]=value（幂等）。

        用于渲染/封面生成后，把实际落盘的路径写进配置，供上传与清理读取，
        不再靠上传端猜测文件名。
        """
        try:
            config_path = os.path.join(out_dir, f'edl_config_v{version}.json')
            if not os.path.exists(config_path):
                return
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            artifacts = config.get('artifacts') or {}
            artifacts[key] = value
            config['artifacts'] = artifacts
            tmp = config_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, config_path)
        except Exception as e:
            self.logger.log('system', 'WARN', f'回写 artifacts.{key} 失败: {e}')

    def _select_best_videos(self, all_videos, config=None):
        """筛选最佳版本。

        时长窗口改用本剧 config（方案A：每版在 [min_dur, max_dur]），默认
        180~330s。原来硬编码 CONFIG['duration']['min_final'/'max_final']
        =210/1200 正是 21 分钟成片被"接受"的窗口，已随本剧时长配置一并收敛。
        """
        rc = self._resolve_cut_config(config)
        # 与 _compute_version_budget 一致：duration.enabled=False（全局关掉剧情
        # 驱动时长）时回退旧宽松窗口 [min_final, max_final]，否则用本剧窗口。
        if not CONFIG.get('duration', {}).get('enabled', True):
            _dcfg = CONFIG.get('duration', {})
            _lo = float(_dcfg.get('min_final', 210))
            _hi = float(_dcfg.get('max_final', 1200))
        else:
            _lo = float(rc['min_dur'])
            _hi = float(rc['max_dur'])
        videos = [v for v in all_videos if "测试" not in v.get("filename", "") and "_test" not in v.get("filename", "")]
        
        valid_videos = []
        for v in videos:
            duration = self._get_video_duration(v.get("path", ""))
            # 业务要求成片在 [min_dur, max_dur]（本剧可配，默认 180~330s）。
            # 渲染会 atempo=1.05 压缩，故以成片实测 duration 判定。
            if _lo <= duration <= _hi:
                v["duration"] = duration
                valid_videos.append(v)
        
        if len(valid_videos) < 6:
            others = [v for v in videos if v not in valid_videos]
            valid_videos.extend(others)
        
        return valid_videos[:8]
    
    def _parse_version_from_filename(self, fname):
        """从交付文件名解析版本号：{drama}_{n}_{主题}_去重版.mp4 或 V{n}。"""
        if not fname:
            return None
        m = re.search(r'_(\d+)_[^_]*_去重版\.mp4$', fname)
        if m:
            return int(m.group(1))
        m = re.search(r'V(\d+)', fname)
        return int(m.group(1)) if m else None

    def _dedup_versions_by_content(self, best_videos, drama_name, out_dir,
                                   src_dir=None):
        """内容级去重（设计 2.3①）：版本间感知哈希相似度检测。

        在 _select_best_videos 之后、_post_process 之前调用。对 best_videos
        两两算内容相似度：> hard 剔除差异化弱者；soft~hard 保留但告警；
        剔除后保底 MIN_DELIVER 个版本。指纹计算失败保守放行（不剔除）。

        Returns: (kept_videos, report_dict)  report_dict 供 _write_report。
        """
        try:
            from content_fingerprint import (video_fingerprint, similarity,
                                             internal_duplicate,
                                             FingerprintStore,
                                             dedup_thresholds)
        except Exception as e:
            self.logger.log('system', 'WARN',
                            f'内容去重模块不可用，跳过：{e}')
            return best_videos, {'error': str(e)}

        th = dedup_thresholds(CONFIG)
        hard, soft = th['hard'], th['soft']
        min_deliver = int(CONFIG.get('content_dedup', {}).get('min_deliver', 3))

        store = None
        try:
            store = FingerprintStore(
                os.path.join(CONFIG.get('data_dir', 'data'), 'upload.db'))
        except Exception as e:
            self.logger.log('system', 'WARN',
                            f'指纹库不可用（仅检测不剔除）：{e}')

        # 1. 逐版本算指纹 + 存库（重切覆盖）
        fps = {}
        account = self.assign_account(drama_name)
        for v in best_videos:
            fname = v.get('filename', '')
            path = v.get('path') or v.get('final_path')
            if not path or not os.path.isfile(path):
                fps[fname] = None
                continue
            try:
                seq = video_fingerprint(path)
            except Exception as e:
                self.logger.log(drama_name, 'WARN',
                                f'V{fname} 指纹计算失败（放行）：{e}')
                seq = None
            fps[fname] = seq
            if seq and store:
                try:
                    dur = v.get('duration') or self._get_video_duration(path)
                    store.upsert(
                        drama_name, path, seq,
                        version=self._parse_version_from_filename(fname),
                        account=account, filename=fname,
                        duration=dur, size_bytes=os.path.getsize(path),
                        source='cut')
                except Exception as e:
                    self.logger.log('system', 'WARN',
                                    f'V{fname} 指纹存库失败：{e}')

        # 2. 成片内部重复（告警，不拦截——短剧剧情合法回环）
        internal_dups = []
        for v in best_videos:
            seq = fps.get(v.get('filename', ''))
            if not seq:
                continue
            try:
                for d in internal_duplicate(seq):
                    internal_dups.append({
                        'filename': v.get('filename', ''),
                        'start_a': d['start_a'], 'start_b': d['start_b'],
                        'similarity': d['similarity'],
                    })
            except Exception:
                pass
        if internal_dups:
            self.logger.log(
                drama_name, 'WARN',
                f'内容检测：{len(internal_dups)}处成片内部重复片段（{internal_dups[:3]}）')

        # 3. 版本间相似度（剔除硬重复，告警疑似）
        kept = list(best_videos)
        removed = []
        warnings = []

        pairs = []
        for i in range(len(kept)):
            for j in range(i + 1, len(kept)):
                a = fps.get(kept[i].get('filename', ''))
                b = fps.get(kept[j].get('filename', ''))
                if not a or not b:
                    continue
                try:
                    s = similarity(a, b)
                except Exception:
                    continue
                if s >= hard:
                    pairs.append((i, j, s))
                elif s >= soft:
                    warnings.append(
                        f'V{i + 1}与V{j + 1}疑似重复（相似度{s:.2f}），已保留待人工复核')
        # 剔除：相似度降序；保底 min_deliver；保留索引更小者（_select_best_videos
        # 已把 210-300s 时长合格者排前，即列表靠前更优）
        pairs.sort(key=lambda x: -x[2])
        dropped = set()
        for (i, j, s) in pairs:
            if len(kept) - len(dropped) <= min_deliver:
                break
            if i in dropped or j in dropped:
                continue
            victim = max(i, j)
            keep = min(i, j)
            dropped.add(victim)
            removed.append({
                'filename': kept[victim].get('filename', ''),
                'version': self._parse_version_from_filename(
                    kept[victim].get('filename', '')),
                'vs_filename': kept[keep].get('filename', ''),
                'similarity': round(s, 3),
                'reason': 'content_duplicate',
            })
            self.logger.log(
                drama_name, 'WARN',
                f'内容去重：剔除 {kept[victim].get("filename")}（与'
                f'{kept[keep].get("filename")} 相似度 {s:.2f} ≥ {hard}）')
        kept = [v for idx, v in enumerate(kept) if idx not in dropped]

        report = {
            'hard': hard, 'soft': soft, 'min_deliver': min_deliver,
            'removed': removed, 'warnings': warnings,
            'internal_duplicates': internal_dups,
        }
        if removed:
            self.logger.log(drama_name, 'INFO',
                            f'内容去重完成：剔除{len(removed)}个高重复版本，'
                            f'交付{len(kept)}个')
        return kept, report

    def _get_video_duration(self, video_path):
        """获取视频时长"""
        try:
            ffprobe = os.path.join(CONFIG['ffmpeg_dir'], 'ffprobe.exe')
            cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", video_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except Exception:
            return 0

    def _write_content_risk_audit(self, out_dir, drama_name):
        """把 Web 同源的内容风险审计快照落到剧目录，便于离线复核。"""
        try:
            from cut_insights import build_cut_insights
            data = build_cut_insights(
                CONFIG['output_dir'], drama_name,
                source_root=CONFIG.get('download_base_dir'),
                db_path=os.path.join(CONFIG.get('data_dir', 'data'),
                                     'upload.db'))
            if data is None:
                return
            path = os.path.join(out_dir, '内容风险审计.json')
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            self.logger.log(drama_name, 'WARN',
                            f'内容风险审计快照写入失败（不阻断交付）：{e}')

    def _validate_delivery_media(self, video_path):
        """R6 媒体质量门禁：拒收 0 字节/无视频流/无音轨/时长异常的产物。

        Returns: {'ok': bool, 'reason': str, ...}（见 delivery_quality.validate_media）。
        """
        try:
            from delivery_quality import validate_media
            return validate_media(
                video_path,
                ffprobe_path=CONFIG.get('ffprobe_path') or
                os.path.join(CONFIG.get('ffmpeg_dir', ''), 'ffprobe.exe'),
                min_duration=CONFIG.get('quality', {}).get('min_duration'),
                max_duration=CONFIG.get('quality', {}).get('max_duration'),
            )
        except Exception as exc:
            # 自动化链路没有人工确认环节：无法验证时必须拒收，不能把未知当通过。
            return {
                'ok': False,
                'status': 'unknown',
                'reason': f'门禁异常，无法验证：{exc}',
            }

    def _episode_real_duration(self, ep_file, raw):
        """单集真实时长：probe 权威（与渲染端 render_generic 一致），
        防止 EDL 规划超源文件时长；probe 失败回退转写 last end（保底不丢集）。"""
        dur = self._get_video_duration(ep_file)
        if dur > 0:
            return dur
        return max((float(s.get("end", 0)) for s in raw), default=0.0)
    
    def _post_process(self, input_path, output_path, src_dir=None, drama_name=None):
        """后处理 - 抗查重 + 四角循环水印"""
        # 根据输出目录获取水印名称
        # 路径格式: output/福桃说剧/剧名/文件.mp4 或 output/小桃追剧中/剧名/文件.mp4
        parts = output_path.replace("\\", "/").split("/")
        if len(parts) >= 5:
            watermark = parts[-3]  # 获取水印名称（目录第2级）
        else:
            watermark = "小桃追剧中"
        
        # 四角循环位置
        version_idx = self._get_version_idx(output_path)
        positions = [
            ("w-text_w-30", "30"),           # 右上角
            ("30", "30"),                    # 左上角
            ("w-text_w-30", "h-text_h-30"),  # 右下角
            ("30", "h-text_h-30"),           # 左下角
        ]
        pos = positions[version_idx % 4]
        
        # 抗查重只在「输入还没抗查重」时做。EDL 渲染阶段 render_generic.apply_anti_dup
        # 已经跑过完整抗查重链（产物名带 _去重版.mp4），再跑一遍等于：
        #   时长 354.9s → 338.0s（又 ÷1.05）、306MB → 713MB（码率 7→16.9Mbps）、
        #   两次有损转码（noise 加噪再压一次）、每版多花 ~3 分钟。
        # 实测「断情七载」8 个版本因此多跑了约 24 分钟且体积翻 2.3 倍。
        already_dedup = "_去重版.mp4" in os.path.basename(input_path)
        if already_dedup:
            self.logger.log('system', 'INFO',
                            f'跳过重复抗查重（{os.path.basename(input_path)} 已在渲染阶段完成）')
        else:
            vf = (
                "setpts=PTS/1.05,"
                "crop=1080:1860:0:30,pad=1080:1920:0:30:black,"
                "eq=contrast=1.05:saturation=1.1:brightness=0.02,"
                "noise=alls=8:allf=t,rotate=0.008:fillcolor=black:bilinear=0,fps=29.97,"
                f"drawtext=fontfile='C:/Windows/Fonts/msyh.ttc':"
                f"text='{watermark}':fontsize=36:fontcolor=white@0.3:"
                f"x={pos[0]}:y={pos[1]}"
            )
            af = "atempo=1.05,equalizer=f=3000:t=h:width=1000:g=3," \
                 "equalizer=f=100:t=h:width=50:g=-2," \
                 "loudnorm=I=-14:TP=-1:LRA=11"
            
            # 输入输出同路径会让ffmpeg边读边写导致失败/损坏，先写临时文件再原子替换
            tmp_path = output_path + ".post.tmp.mp4"
            cmd = [
                CONFIG['ffmpeg_path'], "-y", "-i", input_path,
                "-vf", vf, "-af", af,
                "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23", "-rc", "vbr", "-c:a", "aac", "-b:a", "128k",
                tmp_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            if result.returncode != 0:
                stderr_text = result.stderr.decode("utf-8", errors="ignore")[:300]
                self.logger.log('system', 'ERROR', f'后处理失败(抗查重未完成，跳过此条不裸发): {stderr_text}')
                # 不再 copy 原文件——裸视频直接发会被平台判重。
                # 只清理临时文件，绝不删除渲染好的原始成片。
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                return False
            
            # 后处理成功，用临时文件原子替换最终成片
            try:
                os.replace(tmp_path, output_path)
            except Exception as e:
                self.logger.log('system', 'ERROR', f'替换成片失败：{str(e)}')
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                return False
        
        # 生成封面
        self._generate_cover(input_path, output_path,
                           drama_name=drama_name,
                           src_dir=src_dir,
                           version_idx=version_idx)
        return True
    
    # 交付文件名格式：{剧名安全名}_{版本号}_{钩子关键词}_去重版.mp4
    # 剧名可能含数字，所以锚定「_数字_单token_去重版.mp4$」，不能用裸 V\d+。
    # 之前用 re.search(r'V(\d+)') 永远匹配不到（EDL 文件名没有 V 前缀）→ 恒返回 1
    # → positions[1] 左上角 → 8 个版本水印全挤同一角，四角轮换是死代码。
    _VERSION_RE = re.compile(r'_(\d+)_[^_]+_去重版\.mp4(?:\.post\.tmp\.mp4)?$')

    def _get_version_idx(self, filepath):
        """从交付文件名解析版本序号；解析不到返回 1"""
        try:
            fname = os.path.basename(filepath)
            match = self._VERSION_RE.search(fname)
            if not match:
                match = re.search(r'V(\d+)', fname)  # 兼容 AI 版本的 V1.mp4 命名
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return 1
    
    def _get_watermark_from_path(self, path):
        """从路径获取水印名称"""
        # 路径格式: output/水印名称/剧名/文件.mp4
        parts = path.replace("\\", "/").split("/")
        if len(parts) >= 5:
            return parts[-3]
        return "小桃追剧中"
    
    def _generate_cover(self, input_path, video_path, drama_name=None, src_dir=None, version_idx=1):
        """生成封面图 - 优先用官方封面，无则跳过（用户规则：不生成封面）
        封面保存到 covers/ 子目录，文件名：{剧名}_{版本}_封面.jpg
        """
        try:
            # 封面保存到 covers/ 子目录
            out_dir = os.path.dirname(video_path)
            covers_dir = os.path.join(out_dir, 'covers')
            os.makedirs(covers_dir, exist_ok=True)
            
            if drama_name:
                cover_path = os.path.join(covers_dir, f'{drama_name}_{version_idx}_封面.jpg')
            else:
                # 无剧名时从视频文件名提取
                video_base = os.path.basename(video_path).replace('.mp4', '')
                cover_path = os.path.join(covers_dir, f'{video_base}_封面.jpg')
            
            # 1. 先找素材目录里的官方封面
            if src_dir:
                official_cover = self._find_official_cover(src_dir, drama_name)
                if official_cover:
                    # 复制官方封面
                    shutil.copy(official_cover, cover_path)
                    self.logger.log("system", 'INFO', f'使用官方封面：{os.path.basename(official_cover)}')
                    
                    # 2. 所有版本加角标
                    self._add_cover_badge(cover_path, version_idx)
                    
                    # 2b. 回写实际 cover_path 到 artifacts，供上传/清理读取
                    if os.path.exists(cover_path):
                        self._update_config_artifacts(out_dir, version_idx, 'cover_path', cover_path)
                    
                    return
            
            # 3. 无官方封面：跳过封面生成（用户规则：不生成封面，需程序判断）
            self.logger.log("system", 'INFO', f'无官方封面，跳过封面生成：{os.path.basename(cover_path)}')
            return
            
        except Exception as e:
            self.logger.log("system", 'WARN', f'封面生成失败：{str(e)}')
    
    def _find_official_cover(self, src_dir, drama_name=None):
        """查找官方封面图"""
        # 优先匹配规则
        patterns = ['sph', '视频号', '封面图', '封面', '海报']
        
        # 扫描所有图片
        images = []
        try:
            for f in os.listdir(src_dir):
                if f.lower().endswith(('.png', '.jpg', '.jpeg')):
                    images.append(f)
        except Exception as e:
            self.logger.log('system', 'WARN', f'扫描封面图片失败：{str(e)}')
            return None
        
        # 第一层：按文件名匹配
        for pattern in patterns:
            for img in images:
                if pattern.lower() in img.lower():
                    return os.path.join(src_dir, img)
        
        # 第二层：文件名和剧名一致
        if drama_name:
            for img in images:
                if drama_name in img and '集' not in img:
                    return os.path.join(src_dir, img)
        
        # 第三层：数字开头的图片（如 1.png, 0.png）
        for img in images:
            if img[0].isdigit() and len(img) < 20:
                fpath = os.path.join(src_dir, img)
                size = os.path.getsize(fpath) / (1024*1024)
                if 0.3 <= size <= 2.0:
                    return fpath
        
        return None
    
    def _add_cover_badge(self, cover_path, version_idx):
        """给封面加角标。

        P1-A4：原实现 ffmpeg 直接以 cover_path 同时作输入和输出（与已修掉的
        _post_process 同类 bug），且 subprocess.run 不看 returncode——角标实际
        从未加上过，日志却永远显示"角标已添加"。
        现改为：先写 cover_path.tmp.jpg → returncode 为 0 才 os.replace 落正式名；
        失败时保留无角标原图交付，但日志带 ffmpeg stderr 说清楚。
        """
        badges = {1: "第1集", 2: "第2集", 3: "第3集", 4: "第4集", 5: "完整版",
                  6: "大结局", 7: "加长版", 8: "精编版"}
        badge_text = badges.get(version_idx, f"精编版V{version_idx}")
        tmp_path = cover_path + ".tmp.jpg"

        try:
            cmd = [
                CONFIG['ffmpeg_path'], "-y",
                "-i", cover_path,
                "-vf", f"drawtext=fontfile='C:\\:/Windows/Fonts/msyhbd.ttc':"
                       f"text='{badge_text}':fontsize=52:fontcolor=white:"
                       f"borderw=3:bordercolor=black:"
                       f"x=w-text_w-30:y=h-text_h-30",
                "-q:v", "2",
                tmp_path
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                # 角标失败不静默：交付无角标原图，但必须留下真实原因
                self.logger.log("system", 'WARN',
                                f'角标添加失败({badge_text})：{(r.stderr or "").strip()[-200:]}')
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                return False
            os.replace(tmp_path, cover_path)
            self.logger.log("system", 'INFO', f'角标已添加：{badge_text}')
            return True
        except Exception as e:
            self.logger.log("system", 'WARN', f'角标添加失败：{str(e)}')
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False
    
    def _cleanup_test_versions(self, out_dir):
        """清理测试版本"""
        test_patterns = ["_测试", "_test", "_temp", "_raw"]
        
        try:
            for f in os.listdir(out_dir):
                if any(p in f for p in test_patterns):
                    fpath = os.path.join(out_dir, f)
                    if os.path.isfile(fpath):
                        os.remove(fpath)
        except Exception as e:
            self.logger.log('system', 'WARN', f'清理测试版本失败：{str(e)}')
    
    def _generate_qc_report(self, best_videos, out_dir, drama_name,
                            src_dir=None):
        """A7 QC 报告：每交付版本 blackdetect/响度/词中切断 → {剧}_QC报告.md。

        审计报告非门禁：QC 模块/转写/ffmpeg 任一不可用都跳过对应项，
        绝不阻塞交付。segments 从保留的 edl_config_v{n}.json 读取。
        """
        try:
            from qc_report import generate_qc
        except Exception as e:
            self.logger.log('system', 'WARN',
                            f'QC 模块不可用，跳过 QC 报告：{e}')
            return

        def load_transcripts():
            """按集加载转写 {ep: [{start,end,text}]}；缺失返回 {}。"""
            if not src_dir:
                return {}
            hi_dir = os.path.join(src_dir, 'transcribe')
            if not os.path.isdir(hi_dir):
                return {}
            out = {}
            for jp in glob.glob(os.path.join(hi_dir, 'EP*.json')):
                base = os.path.basename(jp)
                ep = self._get_episode_num(base[2:] if base.startswith('EP')
                                           else base)
                if ep is None or ep in out:
                    continue
                try:
                    with open(jp, 'r', encoding='utf-8') as f:
                        raw = json.load(f)
                    if isinstance(raw, list) and raw:
                        out[ep] = raw
                except Exception:
                    continue
            return out

        def load_segments(vid):
            """读该版本 EDL config 的 segments；缺失返回 None。"""
            try:
                cfg_path = os.path.join(out_dir, f'edl_config_v{vid}.json')
                if not os.path.exists(cfg_path):
                    return None
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                videos = config.get('videos') or []
                if videos and videos[0].get('segments'):
                    return videos[0]['segments']
            except Exception:
                pass
            return None

        transcripts = load_transcripts()
        lines = [f"# {drama_name} QC 报告\n",
                 f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
                 f"- 检查项：黑帧（blackdetect）、响度（EBU R128 -14 LUFS）、"
                 f"词中切断（切点落在转写句中间）\n",
                 "- 状态口径：通过 / 异常 / 未检测；未检测不会计为通过\n"]
        try:
            for v in best_videos:
                vid = self._parse_version_from_filename(v.get('filename', ''))
                vid = vid or 1
                segments = load_segments(vid)
                qc = generate_qc(v.get('path', ''), CONFIG['ffmpeg_path'],
                                 segments=segments,
                                 transcripts=transcripts if segments else None)
                lines.append(f"\n### V{vid}: {v.get('filename', '')}\n")
                # 黑帧
                bs = qc.get('black_segments') or []
                black_status = qc.get('black_status') or 'unknown'
                if black_status == 'unknown':
                    reason = qc.get('black_error') or '原因未知'
                    lines.append(f"- ⚪ 黑帧检测未完成：{reason}\n")
                elif bs:
                    fmt = '、'.join(f"{b['start']:.1f}-{b['end']:.1f}s"
                                    for b in bs)
                    lines.append(f"- ⚠️ 黑帧段：{fmt}（{len(bs)}处）\n")
                else:
                    lines.append("- ✅ 无黑帧段\n")
                # 响度
                ln = qc.get('loudness')
                loudness_status = qc.get('loudness_status') or 'unknown'
                if loudness_status == 'unknown':
                    reason = qc.get('loudness_error') or '原因未知'
                    lines.append(f"- ⚪ 响度测量未完成：{reason}\n")
                elif ln and ln.get('i') is not None:
                    dev = abs(ln['i'] + 14.0)
                    flag = '⚠️' if loudness_status == 'warning' else '✅'
                    tp = f" / 真峰值 {ln['tp']} dBTP" if ln.get('tp') is not None else ""
                    lines.append(
                        f"- {flag} 综合响度 {ln['i']:.1f} LUFS{tp}"
                        f"（目标 -14，偏差 {dev:.1f}）\n")
                else:
                    lines.append("- ⚪ 响度测量未完成\n")
                # 词中切断
                mc = qc.get('mid_word_cuts') or []
                mid_word_status = qc.get('mid_word_cuts_status') or 'unknown'
                if mid_word_status == 'unknown':
                    reason = qc.get('mid_word_cuts_error') or '原因未知'
                    lines.append(f"- ⚪ 词中切断检测未完成：{reason}\n")
                elif mc:
                    fmt = '、'.join(
                        f"Ep{c['episode']} {c['t']}s「{c['text']}」"
                        f"(余量{c['margin']}s)" for c in mc)
                    lines.append(f"- ⚠️ 词中切断：{fmt}（{len(mc)}处）\n")
                else:
                    lines.append("- ✅ 无词中切断\n")
                overall = qc.get('overall_status') or 'unknown'
                overall_label = {'pass': '✅ 通过', 'warning': '⚠️ 有异常',
                                 'unknown': '⚪ 有未检测项'}.get(
                                     overall, '⚪ 有未检测项')
                lines.append(f"- {overall_label}\n")
        except Exception as e:
            self.logger.log('system', 'WARN',
                            f'QC 报告生成中断（保留已生成部分）：{e}')
        try:
            report_path = os.path.join(out_dir, f'{drama_name}_QC报告.md')
            tmp = report_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            os.replace(tmp, report_path)
            self.logger.log('system', 'INFO',
                            f'QC 报告生成：{report_path}')
        except Exception as e:
            self.logger.log('system', 'WARN', f'QC 报告写入失败：{e}')

    def _write_render_report(self, out_dir, drama_name, verdicts,
                             expected_count=None):
        """P8：按每版交付 verdict 写渲染报告（覆盖 render_generic 的每版覆写结果）。

        render_generic 每次调用都覆写同一份 {drama}_渲染报告.md（只剩最后一版），
        且只写「渲染成功」，不反映交付质量门禁的拒收结论。本方法在版本循环
        结束后统一重写：每版标注 accepted/rejected/render_failed，拒收版标
        REJECTED（含原因）而非 OK。
        """
        try:
            verdicts = verdicts or {}
            if not verdicts and not expected_count:
                return
            ok_n = sum(1 for v in verdicts.values() if v.get('status') == 'accepted')
            rej_n = sum(1 for v in verdicts.values() if v.get('status') == 'rejected')
            fail_n = sum(1 for v in verdicts.values() if v.get('status') == 'render_failed')
            missing_n = sum(1 for v in verdicts.values()
                            if v.get('status') == 'not_rendered')
            expected_count = int(expected_count or len(verdicts))
            absent_n = max(0, expected_count - len(verdicts))
            lines = [
                f'# {drama_name} 渲染报告（交付结论）', '',
                f'- 剧名: {drama_name}',
                f'- 计划版本数: {expected_count}',
                f'- 成功: {ok_n}',
                f'- 拒收: {rej_n}',
                f'- 渲染失败: {fail_n}',
                f'- 未生成/未执行: {missing_n + absent_n}', '',
                '## 交付质量结论', '',
                '| 版本 | 结论 |',
                '|---|---|',
            ]
            for v_idx in range(expected_count):
                v = verdicts.get(v_idx) or {
                    'status': 'not_rendered', 'reason': '未执行（循环提前结束）'}
                status = v.get('status', 'unknown')
                reason = v.get('reason', '')
                if status == 'accepted':
                    label = '✅ ACCEPTED'
                elif status == 'rejected':
                    label = f'⛔ REJECTED（{reason}）'
                elif status == 'not_rendered':
                    label = f'⚪ NOT_RENDERED（{reason}）'
                else:
                    label = f'❌ RENDER_FAILED（{reason}）'
                lines.append(f'| V{v_idx + 1} | {label} |')
            report = '\n'.join(lines) + '\n'
            report_path = os.path.join(out_dir, f'{drama_name}_渲染报告.md')
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report)
            self.logger.log('system', 'INFO',
                            f'渲染报告（交付结论）已写入：{report_path}')
        except Exception as e:
            self.logger.log('system', 'WARN', f'渲染报告（交付结论）写入失败：{e}')

    def _resolve_actual_cover(self, out_dir, drama_name, version_no,
                              config=None, video_filename=''):
        """返回实际存在的封面绝对路径；不存在返回 None，不伪造报告路径。"""
        candidates = []
        artifacts = (config or {}).get('artifacts') or {}
        if artifacts.get('cover_path'):
            candidates.append(artifacts['cover_path'])
        covers_dir = os.path.join(out_dir, 'covers')
        candidates.extend([
            os.path.join(covers_dir, f'{drama_name}_{version_no}_封面.jpg'),
        ])
        if video_filename:
            candidates.append(os.path.join(
                covers_dir, f'{os.path.splitext(video_filename)[0]}_封面.jpg'))
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return os.path.abspath(candidate)
        try:
            patterns = (
                f'{drama_name}_{version_no}_*.jpg',
                f'*_{version_no}_封面.jpg',
            )
            for pattern in patterns:
                hits = sorted(glob.glob(os.path.join(covers_dir, pattern)))
                if hits:
                    return os.path.abspath(hits[0])
        except Exception:
            pass
        return None

    def _write_report(self, out_dir, drama_name, best_videos, cut_method='未知',
                      dedup_report=None):
        """写交付报告 - 从 edl_config_v{i}.json 读取实际文案

        只列交付集 best_videos（真实交付），不再扫磁盘：交付清理发生在
        写报告之后，磁盘此刻仍含未交付/被剔除的渲染产物，按磁盘最大编号
        会把未交付版本误报为已交付。版本号从文件名解析（新命名
        {drama}_{n}_{主题}_去重版.mp4；旧命名 V{n} 兜底）。

        dedup_report（可选）：R9 内容去重检测结果，非空时追加"内容去重"节。
        """
        try:
            # 交付集：版本号 -> 文件名（以实际交付为准，不扫磁盘）
            delivered = {}
            for v in best_videos:
                fname = v.get('filename', '') if isinstance(v, dict) else ''
                if not fname:
                    continue
                m = re.search(r'_(\d+)_[^_]*_去重版\.mp4$', fname) or re.search(r'V(\d+)', fname)
                if m:
                    delivered[int(m.group(1))] = fname
            # 解析不出版本号（异常命名）时退化为按序编号，保证报告有内容
            if not delivered:
                for idx, v in enumerate(best_videos, 1):
                    fname = v.get('filename', '') if isinstance(v, dict) else ''
                    if fname:
                        delivered[idx] = fname
            count = len(delivered)
            sorted_vnos = sorted(delivered.keys())
            # P7: 账号取实际输出目录（output/{账号}/{剧名}），不再用独立分配
            # （assign_account 可能与实际目录不符——本剧目录福桃说剧却曾写小桃追剧中）。
            try:
                acc_dir = os.path.dirname(out_dir.rstrip('/\\'))
                account = os.path.basename(acc_dir)
                if not account or account == 'output':
                    account = self.assign_account(drama_name)
            except Exception:
                account = self.assign_account(drama_name)
            report = f"""# {drama_name} 交付报告
- 账号：{account}
- 切完时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}
- 共{count}条成片
- 切法：{cut_method}

## 发布文案
"""
            for vno in sorted_vnos:
                report += f"\n### V{vno}\n"

                # 从 edl_config_v{vno}.json 读取文案
                config_path = os.path.join(out_dir, f"edl_config_v{vno}.json")
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)

                    # 提取文案信息
                    # 文案可能在 videos[0] 中
                    if 'videos' in config and len(config['videos']) > 0:
                        video_info = config['videos'][0]
                        title = video_info.get('title', '无标题')
                        description = video_info.get('description', '无描述')
                        # 话题字段可能是 hashtags 或 topics
                        topics = video_info.get('hashtags', video_info.get('topics', []))
                    else:
                        title = config.get('title', '无标题')
                        description = config.get('description', '无描述')
                        topics = config.get('hashtags', config.get('topics', []))

                    # 格式化话题
                    if isinstance(topics, list):
                        topics_str = ' '.join([f'#{t}' if not t.startswith('#') else t for t in topics])
                    else:
                        topics_str = f'#{topics}'

                    fname = delivered.get(vno, '（本版未产出）')
                    # P9: 只写磁盘上实际存在的封面；不再拼一个不存在的推测路径。
                    cover_rel = ''
                    cover_abs = self._resolve_actual_cover(
                        out_dir, drama_name, vno, config,
                        '' if fname == '（本版未产出）' else fname)
                    if cover_abs:
                        cover_rel = os.path.relpath(cover_abs, out_dir).replace(
                            os.sep, '/')
                    else:
                        self.logger.log('system', 'WARN',
                                        f'V{vno} 交付报告未找到实际封面，标记未产出')
                    report += f"**标题：** {title}\n"
                    report += f"**描述：** {description}\n"
                    report += f"**话题：** {topics_str}\n"
                    report += f"**文件：** {fname}\n"
                    report += f"**封面：** {cover_rel if cover_rel else '（本版未产出）'}\n"

                except Exception as e:
                    # 如果读取配置失败，使用默认值
                    fname = delivered.get(vno, '（本版未产出）')
                    report += f"**标题：** 待补充\n"
                    report += f"**描述：** 待补充\n"
                    report += f"**话题：** #短剧 #短剧推荐 #追剧 #热门短剧 #好剧推荐\n"
                    report += f"**文件：** {fname}\n"
                    report += "**封面：** （未找到实际封面）\n"
                    self.logger.log('system', 'WARN', f'读取V{vno}配置失败：{str(e)}')

            # R9：内容去重检测结果（写入交付报告，便于溯源）
            if dedup_report:
                report += "\n## 内容去重\n"
                if dedup_report.get('error'):
                    report += f"- 检测不可用：{dedup_report['error']}\n"
                elif not (dedup_report.get('removed')
                          or dedup_report.get('warnings')
                          or dedup_report.get('internal_duplicates')):
                    report += "- 未发现内容重复（版本间相似度均低于阈值）\n"
                if dedup_report.get('removed'):
                    report += (f"- 剔除高重复版本（硬阈值 "
                               f"{dedup_report.get('hard')}）：\n")
                    for r in dedup_report['removed']:
                        report += (f"  - {r['filename']}（与{r['vs_filename']} "
                                   f"相似度 {r['similarity']}，"
                                   f"{r.get('reason')}）\n")
                if dedup_report.get('warnings'):
                    report += (f"- 疑似重复（软阈值 "
                               f"{dedup_report.get('soft')}），已保留待人工复核：\n")
                    for w in dedup_report['warnings']:
                        report += f"  - {w}\n"
                if dedup_report.get('internal_duplicates'):
                    report += (f"- 成片内部重复片段："
                               f"{len(dedup_report['internal_duplicates'])}处\n")
                    for d in dedup_report['internal_duplicates'][:5]:
                        report += (f"  - {d['filename']} {d['start_a']}s↔"
                                   f"{d['start_b']}s（{d['similarity']}）\n")

            with open(os.path.join(out_dir, f"{drama_name}_交付报告.md"), "w", encoding="utf-8") as f:
                f.write(report)

            self.logger.log('system', 'INFO', f'交付报告已生成：{count}个版本')
        except Exception as e:
            self.logger.log('system', 'WARN', f'写报告失败：{str(e)}')
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
