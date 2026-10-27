# -*- coding: utf-8 -*-
"""
短剧工坊 流水线处理器
功能：下载、切片、去重、错误处理、进度监控
"""
import os
import re
import json
import time
import shutil
import subprocess
import sys
import threading
import random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 导入AI文案生成器
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_copy_generator import AICopyGenerator

# 路径配置
CONFIG = {
    'baidupcs_path': r'D:\短剧工坊\素材\BaiduPCS-Go\BaiduPCS-Go-v4.0.2-windows-x64\BaiduPCS-Go.exe',
    'download_base_dir': r'E:\短剧素材',
    'scripts_dir': r'D:\短剧工坊\scripts',
    'output_dir': r'D:\短剧工作区\output',
    'data_dir': r'D:\短剧工坊\data',
    'log_dir': r'D:\短剧工坊\logs',
    'cache_path': r'D:\短剧工坊\data\dramaid_cache.json',
    'state_file': r'D:\短剧工坊\data\pipeline_state.json',
    'temp_dir': os.environ.get('TEMP', r'C:\Users\86159\AppData\Local\Temp'),
    'ffmpeg_path': r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe',
    'ffmpeg_dir': r'D:\ffmpeg-master-latest-win64-gpl-shared\bin',
    'asr_script': r'D:\短剧工坊\webapp\transcribe_dir.py',
    
    'download': {
        'max_workers': 2,  # 同一百度账号CLI并发不宜过高，5会冲突
        'max_retries': 3,
        'timeout': 1800,
        'transfer_timeout': 120,
        'delete_timeout': 60,
        'default_pwd': '9999',  # 默认提取码
        'min_manual_files': 2,  # 手动素材最少文件数
        'min_download_files': 10,  # 已下载最少文件数
    },
    
    
    'edl': {
        'min_duration': 180,
        'max_duration': 300,
        'count': 5,
        'retry': 2,
        'segment_duration': [30, 75]
    },
    
    'quality': {
        'min_duration': 180,
        'max_duration': 300,
        'min_size_mb': 10
    },
    
    'resource': {
        'min_disk_gb': 100,
        'max_concurrent': 2
    }
}

# 标点符号列表
PUNCTUATIONS = ['，', '。', '？', '！', '、', '：', '；', '“', '”', '（', '）', ' ', '\t']

# 题材分类规则
GENRE_RULES = [
    (["萌宝", "三宝", "崽", "双胞胎", "找爹", "三岁"], "小桃追剧中"),
    (["糙汉", "宠妻", "甜宠", "二婚", "闪婚", "先婚后爱"], "小桃追剧中"),
    (["婆婆", "妈", "娘家", "养母", "孝道", "亲情"], "小桃追剧中"),
    (["年代", "七零", "八零", "种田", "山村", "农村"], "小桃追剧中"),
    (["真千金", "假千金", "抱错", "认亲"], "福桃追剧"),
    (["打脸", "逆袭", "复仇", "重生", "离婚"], "福桃追剧"),
    (["前夫", "追妻", "后悔", "复婚", "前妻"], "福桃追剧"),
    (["总裁", "豪门", "陆总", "傅少"], "福桃追剧"),
    (["战神", "兵王", "赘婿", "龙王", "回归"], "福桃追剧"),
]


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
    
    def log(self, drama_name, level, message):
        """记录日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = {
            'timestamp': timestamp,
            'drama_name': drama_name,
            'level': level,
            'message': message
        }
        
        with self.lock:
            self.logs.append(log_entry)
            
            # 写入文件
            log_file = os.path.join(self.log_dir, f"{datetime.now().strftime('%Y%m%d')}.log")
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] [{level}] [{drama_name}] {message}\n")
        
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
        """保存状态"""
        try:
            with self.lock:
                with open(self.state_file, 'w', encoding='utf-8') as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
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
        self.lock = threading.Lock()
        
        # 初始化目录
        os.makedirs(CONFIG['download_base_dir'], exist_ok=True)
        os.makedirs(CONFIG['output_dir'], exist_ok=True)
    
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
    
    def should_process(self, drama_name, src_dir, out_dir):
        """检查是否应该处理"""
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
                if len(mp4s) >= 3:
                    return False, f"输出目录已有成品：{len(mp4s)}个"
            except Exception as e:
                self.logger.log('system', 'WARN', f'检查输出目录失败：{str(e)}')
        
        # 第4层：模糊去重
        for name in completed_names:
            if self.is_fuzzy_duplicate(drama_name, name):
                return False, f"与已完成剧重复：{name}"
        
        return True, "可以处理"
    
    def assign_account(self, drama_name, submit_order=None):
        """按题材分配账号（方案3：关键词匹配 + 交替分配）
        
        Args:
            drama_name: 剧名
            submit_order: 提交顺序（从0开始），用于交替分配
            
        Returns:
            账号名称："小桃追剧中" 或 "福桃追剧"
        """
        # 1. 关键词精确匹配（优先级最高）
        for keywords, account in GENRE_RULES:
            for kw in keywords:
                if kw in drama_name:
                    return account
        
        # 2. 按提交顺序交替分配（保证均衡）
        if submit_order is not None:
            return "小桃追剧中" if submit_order % 2 == 0 else "福桃追剧"
        
        # 3. 兜底默认
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
        # 福桃追剧 → 福桃说剧 → output/福桃说剧/
        # 小桃追剧中 → 小桃追剧中 → output/小桃追剧中/
        if account == "福桃追剧":
            return "福桃说剧"
        return "小桃追剧中"
    
    def get_output_subdir(self, drama_name, submit_order=None):
        """获取输出目录的子目录（按水印分类）"""
        account = self.assign_account(drama_name, submit_order)
        return self.get_watermark_name(account)
    
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
    
    
    def determine_version_count(self, ep_count):
        """判断版本数"""
        if ep_count >= 30:
            return 8
        return 6
    
    def process_message(self, message):
        """处理消息"""
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
                    return self.manual_cut(message[len(prefix):].strip())
            
            # 提取链接
            links = LinkExtractor.extract(message)
            if not links:
                self.logger.log('system', 'WARN', f'未找到链接')
                return {'status': 'failed', 'error': '未找到百度网盘链接'}
            
            self.logger.log('system', 'INFO', f'找到{len(links)}个链接，开始下载...')
            
            # 下载所有链接
            download_results = self.download_batch(links)
            
            successful_downloads = [r for r in download_results if r['success']]
            failed_downloads = [r for r in download_results if not r['success']]
            
            self.logger.log('system', 'INFO',
                           f'下载完成: 成功{len(successful_downloads)}, 失败{len(failed_downloads)}')
            
            if not successful_downloads:
                return {
                    'status': 'failed',
                    'error': '所有下载失败',
                    'failed_downloads': failed_downloads
                }
            
            # 切片所有剧
            cut_results = self.cut_batch(successful_downloads)
            
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
            
        except Exception as e:
            self.logger.log('system', 'ERROR', f'处理失败: {str(e)}')
            return {'status': 'failed', 'error': str(e)}
        
        finally:
            with self.lock:
                self.processing = False
    
    def manual_cut(self, drama_name):
        """手动切片 - 从素材目录直接切片"""
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
        mp4_files = [f for f in files if f.endswith('.mp4') and '.downloading' not in f]
        
        if len(mp4_files) < 2:
            self.logger.log('system', 'ERROR', f'素材文件不足：{len(mp4_files)}个')
            return {'status': 'failed', 'error': f'素材文件不足：{len(mp4_files)}个'}
        
        self.logger.log('system', 'INFO', f'找到{len(mp4_files)}集素材，开始切片...')

        # 重复切片检查：output 下已有该剧成片则跳过
        for account_dir in os.listdir(CONFIG['output_dir']) if os.path.exists(CONFIG['output_dir']) else []:
            existing = os.path.join(CONFIG['output_dir'], account_dir, drama_name)
            if os.path.isdir(existing):
                done = [f for f in os.listdir(existing) if f.endswith('_去重版.mp4')]
                if done:
                    self.logger.log('system', 'INFO', f'已有{len(done)}个成片，跳过重复切片：{existing}')
                    return {'status': 'skipped', 'reason': 'already_cut', 'output_dir': existing, 'existing': done}

        # 构建下载结果
        download_result = {
            'drama_name': drama_name,
            'link_id': drama_name,
            'downloaded_files': mp4_files,
            'src_dir': src_dir,
            'success': True,
            'error': None
        }
        
        # 切片
        result = self.cut_batch([download_result])
        
        successful_cuts = [r for r in result if r['success']]
        failed_cuts = [r for r in result if not r['success']]
        
        return {
            'status': 'success' if successful_cuts else 'failed',
            'drama_name': drama_name,
            'episodes': len(mp4_files),
            'cut': len(successful_cuts),
            'failed_cuts': len(failed_cuts),
            'output_dirs': [r['output_dir'] for r in successful_cuts]
        }
    
    def download_batch(self, links):
        """并发下载所有链接"""
        results = []
        
        with ThreadPoolExecutor(max_workers=CONFIG['download']['max_workers']) as executor:
            future_to_link = {
                executor.submit(self._download_with_retry, link_info, CONFIG['download']['max_retries']): link_info
                for link_info in links
            }
            
            for future in as_completed(future_to_link):
                link_info = future_to_link[future]
                try:
                    result = future.result()
                    results.append(result)
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
    
    def _download_with_retry(self, link_info, max_retries):
        """带重试的单次下载"""
        attempts = 0
        last_error = None
        
        while attempts < max_retries:
            attempts += 1
            
            try:
                result = self._download_once(link_info)
                
                if result['success']:
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
            
            except Exception as e:
                last_error = str(e)
            
            # 重试前等待
            if attempts < max_retries:
                wait_time = 2 ** attempts
                time.sleep(wait_time)
        
        return {
            'link': link_info['link'],
            'link_id': link_info['link_id'],
            'pwd': link_info['pwd'],
            'success': False,
            'error': f'Failed after {max_retries} attempts: {last_error}',
            'downloaded_files': [],
            'attempts': attempts
        }
    
    def _download_once(self, link_info):
        """单次下载 - 转存到/短剧素材/目录，下载后清理"""
        link = link_info['link']
        link_id = link_info['link_id']
        
        # 从链接提取ID（用于日志）
        link_dir_name = link.split('/')[-1].split('?')[0]
        
        # 1. 检查用户是否已手动放置素材
        # 检查所有可能的目录名
        potential_names = [link_dir_name, link_id]
        for name in potential_names:
            manual_dir = os.path.join(CONFIG['download_base_dir'], name)
            if os.path.exists(manual_dir):
                existing_files = self._scan_files_recursive(manual_dir)
                existing_mp4 = [f for f in existing_files if f.endswith('.mp4') and '.downloading' not in f]
                if len(existing_mp4) >= CONFIG['download']['min_manual_files']:
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
            process = subprocess.run(
                transfer_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=CONFIG['download']['transfer_timeout'],
                cwd=os.path.dirname(CONFIG['baidupcs_path'])
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
            
            # 4. 创建本地目标目录
            target_dir = os.path.join(CONFIG['download_base_dir'], dir_name)
            os.makedirs(target_dir, exist_ok=True)
            
            # 检查是否已下载完成：不再用固定阈值早退（半拉下载会被误判完成），
            # 直接走 download 命令，它带 --overwrite=never，已下好的文件会自动跳过、续传剩余的。

            # 5. 从网盘下载
            download_cmd = [
                CONFIG['baidupcs_path'], "download",
                f"/短剧素材/{dir_name}",
                "--saveto", target_dir,
                "--overwrite=never"
            ]
            
            self.logger.log(dir_name, 'INFO', f'开始下载：{dir_name}')
            
            # 启动进度监控线程
            stop_monitor = threading.Event()
            monitor_thread = threading.Thread(
                target=self._monitor_download_progress, 
                args=(target_dir, dir_name, stop_monitor)
            )
            monitor_thread.start()
            
            process = subprocess.run(
                download_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=CONFIG['download']['timeout'],
                cwd=os.path.dirname(CONFIG['baidupcs_path'])
            )
            
            # 停止监控
            stop_monitor.set()
            monitor_thread.join(timeout=5)
            
            # 6. 扫描下载的文件
            downloaded_files = self._scan_files_recursive(target_dir)
            
            if downloaded_files:
                mp4_files = [f for f in downloaded_files if f.endswith('.mp4')]
                self.logger.log(dir_name, 'INFO', f'下载完成：{len(downloaded_files)}个文件（{len(mp4_files)}集视频）')
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
                    return {
                        'success': False,
                        'downloaded_files': [],
                        'target_dir': target_dir,
                        'drama_name': dir_name,
                        'error': f'下载不完整：mp4={len(mp4_files)}，损坏{len(too_small)}',
                        'transferred': True
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
                
                return {
                    'success': True,
                    'downloaded_files': downloaded_files,
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
                return {
                    'success': False,
                    'downloaded_files': [],
                    'target_dir': target_dir,
                    'drama_name': dir_name,
                    'error': '下载完成但未找到文件',
                    'transferred': True
                }
            
        except subprocess.TimeoutExpired:
            # 如果已转存，尝试清理网盘
            if 'dir_name' in locals() and dir_name != link_dir_name:
                try:
                    delete_cmd = [
                        CONFIG['baidupcs_path'], "delete",
                        f"/短剧素材/{dir_name}",
                        "--force"
                    ]
                    subprocess.run(delete_cmd, timeout=30, 
                                  cwd=os.path.dirname(CONFIG['baidupcs_path']))
                    self.logger.log(dir_name, 'INFO', '网盘文件已清理（超时）')
                except Exception:
                    pass
            return {
                'success': False,
                'downloaded_files': [],
                'target_dir': None,
                'drama_name': link_dir_name,
                'error': 'Timeout',
                'transferred': False
            }
        except Exception as e:
            # 如果已转存，尝试清理网盘
            if 'dir_name' in locals() and dir_name != link_dir_name:
                try:
                    delete_cmd = [
                        CONFIG['baidupcs_path'], "delete",
                        f"/短剧素材/{dir_name}",
                        "--force"
                    ]
                    subprocess.run(delete_cmd, timeout=30, 
                                  cwd=os.path.dirname(CONFIG['baidupcs_path']))
                    self.logger.log(dir_name, 'INFO', '网盘文件已清理（异常）')
                except Exception:
                    pass
            return {
                'success': False,
                'downloaded_files': [],
                'target_dir': None,
                'drama_name': link_dir_name,
                'error': str(e),
                'transferred': False
            }
    
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
    
    def _monitor_download_progress(self, target_dir, dir_name, stop_event):
        """监控下载进度，记录已下载的文件"""
        last_count = 0
        try:
            while not stop_event.is_set():
                try:
                    files = self._scan_files_recursive(target_dir)
                    mp4_files = [f for f in files if f.endswith('.mp4') and '.downloading' not in f]
                    total_size = sum(os.path.getsize(f) for f in mp4_files)
                    
                    if len(mp4_files) > last_count:
                        self.logger.log(dir_name, 'INFO', 
                            f'下载中：{len(mp4_files)}集 ({total_size/1024/1024:.1f}MB)')
                        last_count = len(mp4_files)
                    
                    stop_event.wait(timeout=10)
                except Exception as e:
                    self.logger.log(dir_name, 'WARN', f'监控异常：{str(e)}')
                    stop_event.wait(timeout=10)
        finally:
            self.logger.log(dir_name, 'INFO', f'监控线程结束')
    
    def _scan_files_recursive(self, directory):
        """递归扫描文件"""
        files = []
        try:
            for root, dirs, filenames in os.walk(directory):
                for f in filenames:
                    if f.lower().endswith('.mp4') and not f.endswith('.downloading'):
                        files.append(os.path.join(root, f))
        except Exception as e:
            self.logger.log('system', 'WARN', f'扫描文件失败：{str(e)}')
        return files
    
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

    def cut_batch(self, downloads):
        """批量切片"""
        results = []
        failed = []
        
        # 第一轮：处理所有
        for d in downloads:
            result = self._cut_once(d)
            result['retried'] = False
            
            if result['success']:
                results.append(result)
            else:
                error_msg = result.get("error", "未知错误") or "未知错误"
                self.logger.log('system', 'WARN', f'切片失败，将重试：{result.get("name", "未知")} - {error_msg}')
                failed.append({'download': d, 'error': error_msg})
        
        # 第二轮：重试失败的
        for item in failed:
            d = item['download']
            self.logger.log('system', 'INFO', f'重试切片：{d.get("link_id", "未知")}')
            result = self._cut_once(d)
            result['retried'] = True
            
            if result['success']:
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
    
    def _cut_once(self, download_result):
        """单次切片"""
        # 从下载目录提取实际剧名
        drama_name = download_result.get('drama_name') or download_result.get('link_id', '')
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
        output_subdir = self.get_output_subdir(drama_name, submit_order)
        out_dir = os.path.join(CONFIG['output_dir'], output_subdir, drama_name)
        
        # 去重检查
        should, reason = self.should_process(drama_name, src_dir, out_dir)
        if not should:
            self.logger.log(drama_name, 'WARN', f'切片跳过：{reason}')
            self._update_state('idle', '', 0, f'跳过：{reason}')
            return {
                'name': drama_name,
                'success': False,
                'output_dir': None,
                'error': f'跳过：{reason}'
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

            # P1-A2/A3：run 开始先清掉历史失败 run 的残留产物。
            # 能走到这里说明本剧未被标记完成（state/已切完/去重闸门都已过），
            # out_dir 里的旧 mp4 只可能是未过后处理的"假成品"：不清则
            # should_process 第三层会把重试误判为"已有成品"，收集按 _{n}_
            # 子串还会把旧件混进本次交付。
            swept = self._clean_partial_outputs(out_dir)
            if swept:
                self.logger.log(drama_name, 'WARN', f'run开始清理：删除{swept}个历史残留渲染产物')
            
            # 收集素材 - 使用新的list_episodes方法
            episodes = self.list_episodes(src_dir)
            all_eps = [ep[1] for ep in episodes]  # 只取文件路径
            
            ep_count = len(all_eps)
            version_count = self.determine_version_count(ep_count)
            
            self.logger.log(drama_name, 'INFO', f'总集数：{ep_count}，计划生成{version_count}个版本')
            
            # ASR转写
            self._update_state('processing', drama_name, 10, 'ASR转写中')
            transcript_path = self._run_asr(src_dir, drama_name)
            short_titles, long_titles = self._extract_quotes(transcript_path, 10)
            
            # 全部素材交给 EDL 切片。
            # AI 高光智剪已移除：它依赖火山引擎 mediakit-cli 的认证，项目未配置该 key，
            # 每次运行都会白等一次 doctor + 最长 20 分钟任务轮询然后空转。
            # 版本差异化由 EDL 的倒叙/随机结构 + 钩子主题 + ASR 高光台词保证。
            edl_eps = all_eps
            
            self.logger.log(drama_name, 'INFO',
                           f'EDL素材：{len(edl_eps)}集（全量）')
            
            # 随机选择切法：倒叙或随机
            use_reverse = random.random() < 0.5
            cut_method = '倒叙结构' if use_reverse else '随机结构'
            self.logger.log(drama_name, 'INFO', f'使用切法：{cut_method}')
            
            # EDL手动切片（AI 高光已移除，ai_videos 恒为空，保留变量以兼容报告/状态字段）
            ai_videos = []
            edl_count = version_count
            self._update_state('processing', drama_name, 30, 'EDL切片中')
            edl_videos = self._generate_edl_versions(drama_name, src_dir, edl_eps, edl_count, out_dir, use_reverse, short_titles, long_titles)
            
            # 合并版本
            all_videos = ai_videos + edl_videos
            
            # 筛选最佳版本
            best_videos = self._select_best_videos(all_videos)
            
            # 后处理
            self._update_state('processing', drama_name, 80, '后处理中')
            processed_videos = []
            for i, video in enumerate(best_videos):
                ok = self._post_process(video['path'], video['final_path'],
                                        src_dir=src_dir, drama_name=drama_name)
                if ok and os.path.exists(video['final_path']):
                    processed_videos.append(video)
                else:
                    self.logger.log(drama_name, 'WARN',
                        f'V{i+1} 后处理失败，已跳过（不裸发）')
            best_videos = processed_videos
            
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
            self._write_report(out_dir, drama_name, len(best_videos), cut_method)
            
            # 标记完成
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
            
            self._update_state('idle', '', 100, '完成')
            
            # 创建素材标记
            try:
                with open(os.path.join(src_dir, "已切完.txt"), "w") as f:
                    f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as e:
                self.logger.log('system', 'WARN', f'写已切完标记失败：{str(e)}')
            
            self.logger.log(drama_name, 'INFO', f'完成：{len(best_videos)}个版本')
            
            return {
                'name': drama_name,
                'success': True,
                'output_dir': out_dir,
                'error': None
            }
        
        except Exception as e:
            self.logger.log(drama_name, 'ERROR', f'切片失败：{str(e)}')
            self.state_manager.add_failed(drama_name, str(e))
            # P1-A2：失败路径清掉本次 run 的渲染产物，瞬时故障不升级为永久卡死。
            # out_dir 是单剧目录且 run 开始时已清过，此刻的 mp4 全是本次渲染的
            # 未交付半成品；留着它们，重试会被"已有成品/已切完"闸门永久跳过。
            swept = self._clean_partial_outputs(out_dir)
            if swept:
                self.logger.log(drama_name, 'WARN', f'失败清理：已删除{swept}个未交付渲染产物')
            return {
                'name': drama_name,
                'success': False,
                'output_dir': None,
                'error': str(e)
            }
        
        finally:
            self._release_lock(lock)
    
    def _run_asr(self, src_dir, drama_name):
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
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
                cwd=src_dir
            )
            
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
        """列出所有集数，返回 [(ep_num, filepath), ...] - 递归扫描子目录"""
        episodes = []
        try:
            # 使用递归扫描，处理嵌套目录结构
            for f in self._scan_files_recursive(src_dir):
                if f.endswith('.mp4') and not f.endswith('.downloading'):
                    fname = os.path.basename(f)
                    ep_num = self.parse_episode_number(fname)
                    if ep_num is not None:
                        episodes.append((ep_num, f))
        except Exception as e:
            self.logger.log('system', 'WARN', f'列出剧集失败：{str(e)}')
        episodes.sort()
        return episodes
    
    def _generate_edl_versions(self, drama_name, src_dir, edl_eps, count, out_dir, use_reverse=False, short_titles=None, long_titles=None):
        """生成EDL版本 - 支持倒叙结构和随机结构，使用AI生成文案"""
        self.logger.log(drama_name, 'INFO', f'生成EDL版本（{count}个）')
        
        # 初始化AI文案生成器
        ai_copy = AICopyGenerator()
        
        # 获取剧集列表
        episodes = self.list_episodes(src_dir)
        ep_nums = [ep[0] for ep in episodes]
        
        if not ep_nums:
            self.logger.log(drama_name, 'ERROR', '未找到有效的剧集文件')
            return []
        
        # 使用AI生成文案
        titles = []
        descriptions = []
        hashtags_list = []
        
        keywords = ["逆袭", "打脸", "真相", "反转", "高潮", "泪目"]
        videos = []
        
        for v_idx in range(min(count, len(ep_nums))):
            # 使用AI生成文案
            hook = keywords[v_idx % len(keywords)]
            # 把ASR抽出的真实金句喂给AI，不再编假plot_summary
            quote = long_titles[v_idx % len(long_titles)] if long_titles else ''
            try:
                ai_result = ai_copy.generate_copy(
                    drama_name=drama_name,
                    version_idx=v_idx + 1,
                    version_hook=hook,
                    plot_summary=f'本剧高光台词参考：{quote}' if quote else '',
                    drama_genre='',
                    quotes=long_titles[:5]
                )
                titles.append(ai_result['title'])
                descriptions.append(ai_result['description'])
                hashtags_list.append(ai_result['hashtags'])
                self.logger.log(drama_name, 'INFO', f'V{v_idx+1} AI文案生成成功')
            except Exception as e:
                # AI失败时使用模板
                self.logger.log(drama_name, 'WARN', f'V{v_idx+1} AI文案生成失败，使用模板: {e}')
                titles.append(f'{drama_name}{hook}瞬间')
                descriptions.append(f'{drama_name}精彩剧情，{hook}瞬间，不容错过！')
                hashtags_list.append(["#短剧", "#短剧推荐", "#热门", f"#{hook}"])
        
        for v_idx in range(min(count, len(ep_nums))):
            # 根据倒叙或随机生成segments
            if use_reverse:
                segments = self._generate_reverse_segments(edl_eps, v_idx,
                                                           drama_name=drama_name)
            else:
                segments = self._generate_random_segments(edl_eps, v_idx)
            
            if not segments:
                continue
            
            # 创建EDL配置 - 动态source_pattern
            # 检测文件命名格式
            sample_files = [f for f in os.listdir(src_dir) if f.endswith('.mp4')][:3]
            if sample_files:
                # 检查文件名格式
                first_file = sample_files[0]
                if "第" in first_file and "集" in first_file:
                    # 检查是否有空格
                    if " - " in first_file:
                        source_pattern = "{name} - 第{ep}集.mp4"
                    elif "-" in first_file:
                        source_pattern = "{name}-第{ep}集.mp4"
                    else:
                        source_pattern = "{name}第{ep}集.mp4"
                elif re.match(r'\d+\.mp4', first_file):
                    source_pattern = "{ep}.mp4"
                else:
                    source_pattern = "{ep}.mp4"
            else:
                source_pattern = "{ep}.mp4"
            
            edl_config = {
                "drama_name": drama_name,
                "source_dir": src_dir,
                "source_pattern": source_pattern,
                "output_dir": out_dir,
                "videos": [
                    {
                        "id": v_idx + 1,
                        "hook_keyword": keywords[v_idx % len(keywords)],
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
            
            # 调用render_generic.py
            try:
                result = subprocess.run(
                    [sys.executable, os.path.join(CONFIG['scripts_dir'], "render_generic.py"), config_path],
                    capture_output=True, text=True, encoding="utf-8", timeout=600,
                    cwd=os.path.dirname(CONFIG['scripts_dir'])
                )
                
                if result.returncode == 0:
                    self.logger.log(drama_name, 'INFO', f'V{v_idx+1} EDL渲染成功')
                    # 查找输出文件
                    output_files = [f for f in os.listdir(out_dir) if f.endswith(".mp4") and f"_{v_idx+1}_" in f]
                    for output_file in output_files:
                        videos.append({
                            'path': os.path.join(out_dir, output_file),
                            'final_path': os.path.join(out_dir, output_file),
                            'filename': output_file,
                            'type': 'edl'
                        })
                else:
                    self.logger.log(drama_name, 'ERROR', f'V{v_idx+1} EDL渲染失败：{result.stderr[:200]}')
                    
            except Exception as e:
                self.logger.log(drama_name, 'ERROR', f'V{v_idx+1} EDL异常：{str(e)}')
        
        return videos
    
    # 成片业务窗口 210-300s（见 _select_best_videos）。渲染会跑 atempo=1.05 轻微加速，
    # 所以 EDL 规划总时长收敛到 230-285s，留出余量确保成片落进窗口。
    # 之前两个生成器都不看累计时长：10 集能排到 ~370s，成片 350-360s 全部落在窗口外——
    # 实测「断情七载，前夫求复合」V1 规划 371.5s、成片 354.9s，8 个版本渲染约 48 分钟
    # 才发现一个都不达标（只有 _select_best_videos 的不足 6 个补全兜底才没归零）。
    PLANNED_SEGS_MIN = 230.0
    PLANNED_SEGS_MAX = 285.0
    MIN_SEG_DUR = 15.0

    def _seg_budget_left(self, segments):
        """规划总时长预算还剩多少秒（相对 PLANNED_SEGS_MAX）。"""
        return self.PLANNED_SEGS_MAX - sum(s["duration"] for s in segments)

    # 冲突/悬念词库：LLM 不可用时的兜底打分
    # 冲突词权重 2（情绪爆发），悬念词权重 1（信息揭示）
    CONFLICT_WORDS = ('打脸', '傻', '死', '滚', '敢', '凭什么', '不可能', '疯了',
                      '骗', '恨', '杀', '血', '泪', '离婚', '出轨', '复合', '孩子',
                      '怀孕', '亿', '婚约', '下不来台', '后悔')
    SUSPENSE_WORDS = ('发现', '秘密', '真相', '原来', '竟然', '居然', '突然',
                      '没想到', '终于', '七年', '前妻', '复婚')

    # LLM 高光打分提示词。业界共识（字节 agentkit 短剧高光、DramaClip）是
    # 「输出时间窗 + 分数 + 理由」，理由必须可审计。score 用整数便于跨集比较。
    _HIGHLIGHT_PROMPT = (
        "你是短剧高光剪辑师。下面是《{drama}》第{ep}集的台词转写（带时间轴）。\n"
        "判断这一集值不值得被剪进 2-4 分钟的推广成片。\n\n"
        "台词（[起-止] 秒）：\n{lines}\n\n"
        "只输出一个 JSON 对象，不要解释、不要 markdown 代码块：\n"
        '{{"score": 0到10的整数, "reason": "不超过40字，说清这一集真实发生了什么", '
        '"start": 窗口起点秒, "end": 窗口终点秒, "hook_type": "类型"}}\n\n'
        "评分标准：\n"
        "9-10 强烈冲突爆发或关键反转，能当开头钩子\n"
        "7-8  明确冲突或对峙\n"
        "5-6  有推进但不够爆\n"
        "3-4  过渡铺垫\n"
        "0-2  平淡无冲突，不值得剪\n\n"
        "要求：\n"
        "- hook_type 只能是：逆袭、打脸、真相、反转、高潮、泪目、平淡 七选一\n"
        "- start/end 必须落在台词时间范围内，窗口 12 到 40 秒，尽量覆盖完整一轮冲突\n"
        "- reason 只描述台词里真实发生的事，不要脑补剧情\n"
        "- 没冲突就老实给低分，不要为了好看而拔高"
    )

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
        """LLM 给单集打高光分；失败返回 None，由调用方回退关键词。"""
        lines = "\n".join(
            f"[{float(s.get('start', 0)):.1f}-{float(s.get('end', 0)):.1f}] "
            f"{s.get('text', '')}" for s in raw)
        if len(lines) > 3000:      # 防御：超长截断，避免撞上下文上限
            lines = lines[:3000]
        content = ai.chat_json(self._HIGHLIGHT_PROMPT.format(
            drama=drama_name or "", ep=ep_num, lines=lines))
        if not content:
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # LLM 偶尔多嘴包一层文本，尽力抢救第一个 {...}
            i, j = content.find('{'), content.rfind('}')
            if i < 0 or j <= i:
                return None
            try:
                data = json.loads(content[i:j + 1])
            except json.JSONDecodeError:
                return None
        try:
            score = max(0, min(10, int(round(float(data['score'])))))
            start = float(data['start'])
            end = float(data['end'])
        except (KeyError, TypeError, ValueError):
            return None
        # 窗口必须落在本集时间轴内，否则 LLM 在编时间码
        if not (0 <= start < end <= ep_dur + 0.5):
            return None
        # LLM 常按精确台词边界给短窗口（5-9s）。短是它判断得准，不该丢弃——
        # 往外扩到至少 12s（前铺垫+后反应），扩不动才放弃。
        MIN_WIN = 12.0
        need = MIN_WIN - (end - start)
        if need > 0:
            add_l = min(need, start)
            add_r = min(need - add_l, max(0.0, ep_dur - end))
            start -= add_l
            end += add_r
        if end - start < 8.0:      # 整集都不到 8s，没法剪
            return None
        return {
            "score": score,
            "reason": str(data.get("reason", ""))[:60],
            "start": round(start, 1),
            "end": round(end, 1),
            "hook_type": str(data.get("hook_type", "高潮"))[:4],
        }

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
                              if isinstance(v, dict) and 'score' in v}
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
            eps_data.append((self._get_episode_num(ep_file), ep_file, raw,
                             max((float(s.get("end", 0)) for s in raw), default=0.0)))
        if not eps_data:
            return []

        # 3) LLM 打分（只打缓存里没有的）
        todo = [e for e in eps_data if e[0] not in cached]
        if todo:
            ai = AICopyGenerator()
            if ai.enabled:
                hits = 0
                for ep_num, ep_file, raw, ep_dur in todo:
                    res = self._score_episode_llm(ai, drama_name, ep_num, raw, ep_dur)
                    if res:
                        res = dict(res, src="llm")
                        cached[ep_num] = res
                        hits += 1
                        self.logger.log(
                            'system', 'INFO',
                            f'高光打分 第{ep_num}集：{res["score"]}分 '
                            f'[{res["hook_type"]}] {res["start"]}-{res["end"]}s '
                            f'｜ {res["reason"]}')
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

        # 4) 组装候选窗口
        out = []
        for ep_num, ep_file, raw, ep_dur in eps_data:
            s = cached.get(ep_num)
            cands = []
            if s:
                # LLM 给的是绝对窗口，外扩一点前后文再夹进本集时长
                start = max(0.0, s["start"] - 1.5)
                dur = min(s["end"] - start + 3.0, ep_dur - start)
                if dur >= 12.0:
                    cands = [{"score": s["score"], "start": round(start, 1),
                              "dur": round(dur, 1), "src": s.get("src", "llm"),
                              "hook_type": s.get("hook_type", ""),
                              "reason": s.get("reason", "")}]
            if not cands:
                cands = self._keyword_cands(raw, ep_dur)
            if cands:
                out.append({"episode": ep_num, "path": ep_file,
                            "dur": ep_dur, "cands": cands})
        return out

    def _generate_reverse_segments(self, edl_eps, v_idx, drama_name=None):
        """倒叙结构：开头上钩子，随后按集号逆序铺开每集的高能窗口。

        高能点主路径是 LLM 逐集打分（带理由，落日志可审计），关键词兜底。
        开头的选型取业界标准与倒叙结构折中：业界（DramaClip 片头解说、字节
        WithOpeningHook）是「最炸场面前置」；但结局那集本身够炸时，锁最后一集
        倒叙感更强。所以：结局集分数 >= 全局最高的 70% 就锁结局集，否则用最炸。
        """
        eps_hl = self._load_episode_highlights(edl_eps, drama_name=drama_name)
        if not eps_hl:
            # 没有 ASR 转写（未跑通或素材缺转写）：退回随机逻辑，保证版本开得出
            return self._generate_random_segments(edl_eps, v_idx)

        segments = []

        def add(ep_num, start, dur):
            """按累计时长预算追加片段；预算耗尽返回 False"""
            left = self._seg_budget_left(segments)
            if left <= self.MIN_SEG_DUR:
                return False
            d = round(min(dur, left), 1)
            if d < self.MIN_SEG_DUR:
                return False
            segments.append({
                "episode": ep_num,
                "start": round(max(0.0, start), 1),
                "duration": d,
            })
            return True

        # 1. 开头钩子。只在同一打分体系内比较（LLM 0-10 分与关键词 2-14 分不同量纲）
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
        oc = opening["cands"][0]
        self.logger.log(
            'system', 'INFO',
            f'开头钩子：第{opening["episode"]}集 {oc["score"]}分 '
            f'[{oc.get("hook_type", "")}] {oc.get("reason", "")}'
            f'（结局集/全局最高 = {ratio:.0%}，{"锁结局" if opening is last else "用最炸"}）')
        add(opening["episode"], oc["start"], oc["dur"])

        # 2. 按集号逆序铺开，每集取该集未被占用的高能窗口
        used = {(opening["episode"], oc["start"])}
        for e in sorted(eps_hl, key=lambda x: -x["episode"]):
            if self._seg_budget_left(segments) <= self.MIN_SEG_DUR:
                break
            for c in e["cands"]:
                key = (e["episode"], c["start"])
                if key in used:
                    continue
                used.add(key)
                if add(e["episode"], c["start"], c["dur"]):
                    break

        return segments

    def _generate_random_segments(self, edl_eps, v_idx):
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
                segments.append({
                    "episode": self._get_episode_num(ep_file),
                    "start": round(start, 1),
                    "duration": round(seg_duration, 1)
                })
                start += seg_duration + random.uniform(1, 3)

        return segments

    def _get_episode_num(self, filepath):
        """从文件名获取集数 - 兼容两种格式"""
        fname = os.path.basename(filepath)
        ep = self.parse_episode_number(fname)
        return ep if ep is not None else 1
    
    def _select_best_videos(self, all_videos):
        """筛选最佳版本"""
        videos = [v for v in all_videos if "测试" not in v.get("filename", "") and "_test" not in v.get("filename", "")]
        
        valid_videos = []
        for v in videos:
            duration = self._get_video_duration(v.get("path", ""))
            # 业务要求成片>=210秒(3.5分钟)，原来错写成110-130，会把短的当最佳
            if 210 <= duration <= 300:
                v["duration"] = duration
                valid_videos.append(v)
        
        if len(valid_videos) < 6:
            others = [v for v in videos if v not in valid_videos]
            valid_videos.extend(others)
        
        return valid_videos[:8]
    
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
                 "equalizer=f=100:t=h:width=50:g=-2"
            
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
        """生成封面图 - 优先用官方封面，否则从视频截取
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
                    
                    return
            
            # 3. 无官方封面：跳过封面生成（用户规则：不生成封面，需程序判断）
            self.logger.log("system", 'INFO', f'无官方封面，跳过封面生成：{os.path.basename(cover_path)}')
            return
            # 兜底：从视频第5秒截取（默认关闭，仅在需要时启用）
            cmd = [
                CONFIG['ffmpeg_path'], "-y",
                "-i", input_path,
                "-ss", "5",
                "-vframes", "1",
                "-q:v", "2",
                cover_path
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                self.logger.log("system", 'WARN', f'封面截取失败：{result.stderr.decode("utf-8", errors="ignore")[:200]}')
                return
            self.logger.log("system", 'INFO', f'封面已生成（视频截取）：{os.path.basename(cover_path)}')
            
            # 所有版本加角标
            self._add_cover_badge(cover_path, version_idx)
            
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
    
    def _write_report(self, out_dir, drama_name, count, cut_method='未知'):
        """写交付报告 - 从 edl_config_v{i}.json 读取实际文案"""
        try:
            # 扫描 out_dir 里实际存在的 mp4，建 V编号→文件名 映射，不再写死
            v_to_file = {}
            try:
                for f in os.listdir(out_dir):
                    m = re.search(r'V(\d+)', f)
                    if m and f.endswith('.mp4') and 'test' not in f.lower():
                        v_to_file[int(m.group(1))] = f
            except Exception:
                pass
            report = f"""# {drama_name} 交付报告
- 账号：{self.assign_account(drama_name)}
- 切完时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}
- 共{count}条成片
- 切法：{cut_method}

## 发布文案
"""
            for i in range(count):
                report += f"\n### V{i+1}\n"
                
                # 从 edl_config_v{i+1}.json 读取文案
                config_path = os.path.join(out_dir, f"edl_config_v{i+1}.json")
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
                    
                    fname = v_to_file.get(i+1, f'{drama_name}_V{i+1}.mp4')
                    report += f"**标题：** {title}\n"
                    report += f"**描述：** {description}\n"
                    report += f"**话题：** {topics_str}\n"
                    report += f"**文件：** {fname}\n"
                    report += f"**封面：** covers/{os.path.splitext(fname)[0]}_封面.jpg\n"
                    
                except Exception as e:
                    # 如果读取配置失败，使用默认值
                    fname = v_to_file.get(i+1, f'{drama_name}_V{i+1}.mp4')
                    report += f"**标题：** 待补充\n"
                    report += f"**描述：** 待补充\n"
                    report += f"**话题：** #短剧 #短剧推荐 #追剧 #热门短剧 #好剧推荐\n"
                    report += f"**文件：** {fname}\n"
                    report += f"**封面：** covers/{os.path.splitext(fname)[0]}_封面.jpg\n"
                    self.logger.log('system', 'WARN', f'读取V{i+1}配置失败：{str(e)}')
            
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
        try:
            import msvcrt
            f = open(lock_path, 'w')
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except Exception:
            return None
    
    def _release_lock(self, lock_file):
        """释放文件锁"""
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