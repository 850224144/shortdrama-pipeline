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
    'mediakit_path': r'D:\短剧工坊\tools\mediakit-cli.exe',
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
    
    'ai': {
        'min_duration': 180,
        'max_duration': 300,
        'count': 3,
        'retry': 2
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

    BAIDU_PAN_PATTERN = r'https?://pan\.baidu\.com/s/[a-zA-Z0-9]+(?:\?pwd=[a-zA-Z0-9]+)?'

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
            link_id = re.search(r'pan\.baidu\.com/s/([a-zA-Z0-9]+)', match)
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
    
    def split_materials(self, all_eps):
        """分配素材给AI和EDL"""
        total = len(all_eps)
        
        # 素材太少，都用全部
        if total <= 20:
            return all_eps, all_eps
        
        # 按比例分配
        ai_count = int(total * 0.6)
        ai_eps = all_eps[:ai_count]
        edl_eps = all_eps[ai_count:]
        
        # EDL素材不足，补充前面的
        if len(edl_eps) < 10:
            supplement = all_eps[:10]
            edl_eps = supplement + edl_eps
        
        return ai_eps, edl_eps
    
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
            
            # 分配素材
            ai_eps, edl_eps = self.split_materials(all_eps)
            
            self.logger.log(drama_name, 'INFO',
                           f'AI素材：{len(ai_eps)}集，EDL素材：{len(edl_eps)}集')
            
            # 随机选择切法：倒叙或随机
            use_reverse = random.random() < 0.5
            cut_method = '倒叙结构' if use_reverse else '随机结构'
            self.logger.log(drama_name, 'INFO', f'使用切法：{cut_method}')
            
            # AI高光智剪
            self._update_state('processing', drama_name, 30, 'AI高光智剪中')
            ai_videos = self._generate_ai_highlights(ai_eps, drama_name, out_dir)
            
            # EDL手动切片
            edl_count = version_count - len(ai_videos)
            self._update_state('processing', drama_name, 60, 'EDL切片中')
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
            
            # 写交付报告
            self._write_report(out_dir, drama_name, len(best_videos), cut_method)
            
            # 标记完成
            account = self.assign_account(drama_name)
            self.state_manager.add_completed(drama_name, account, {
                'ai': len(ai_videos),
                'edl': len(edl_videos),
                'total': len(best_videos)
            }, out_dir)
            
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
    
    def _generate_ai_highlights(self, episode_paths, drama_name, out_dir):
        """生成AI高光版本 - 调用mediakit-cli"""
        self.logger.log(drama_name, 'INFO', f'生成AI版本（{len(episode_paths)}集素材）')
        
        # 检查认证状态
        try:
            check_cmd = [CONFIG['mediakit_path'], 'doctor']
            check_result = subprocess.run(check_cmd, capture_output=True, text=True, encoding='utf-8', timeout=10)
            if check_result.returncode != 0:
                self.logger.log(drama_name, 'WARN', 'AI认证未配置，跳过AI切片，仅使用EDL切片')
                return []
        except Exception as e:
            self.logger.log(drama_name, 'WARN', f'AI检查失败：{str(e)}，跳过AI切片')
            return []
        
        # 提交高光智剪任务
        batch = episode_paths[:20]
        params = json.dumps({
            "max_duration": 230,
            "min_duration": 210,
            "max_number": 3,
            "cut_mode": "Mixed"
        })
        
        cmd = [
            CONFIG['mediakit_path'], "video", "generate-highlights-microdrama",
            "--video-urls", ",".join(batch),
            "--highlight-cuts-param", params,
            "--enable-return-poster=true"
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=300)
            if result.returncode != 0:
                self.logger.log(drama_name, 'ERROR', f'mediakit-cli提交失败：{result.stderr[:200]}')
                return []
            
            data = json.loads(result.stdout)
            task_id = data.get("task_id")
            if not task_id:
                self.logger.log(drama_name, 'ERROR', f'未获取到task_id：{result.stdout[:200]}')
                return []
            
            self.logger.log(drama_name, 'INFO', f'任务已提交：{task_id}')
            
            # 轮询等待结果
            res = None
            for i in range(60):  # 最多等20分钟
                time.sleep(20)
                query_cmd = [CONFIG['mediakit_path'], "shared", "query-task", "--task-id", task_id]
                query_result = subprocess.run(query_cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
                
                if query_result.returncode == 0:
                    res = json.loads(query_result.stdout)
                    if res.get("status") == "completed":
                        self.logger.log(drama_name, 'INFO', f'任务完成，共{len(res.get("mixvideo_info", []))}个版本')
                        break
                    elif res.get("status") == "failed":
                        self.logger.log(drama_name, 'ERROR', f'任务失败：{res.get("error", "")}')
                        return []
            
            if not res or res.get("status") != "completed":
                self.logger.log(drama_name, 'ERROR', '任务超时')
                return []
            
            # 下载并返回视频
            videos = []
            for idx, video in enumerate(res.get("mixvideo_info", [])):
                url = video["video_url"]
                save_path = os.path.join(out_dir, f"raw_{idx+1}.mp4")
                try:
                    urllib.request.urlretrieve(url, save_path)
                    final_path = os.path.join(out_dir, f"{drama_name}_V{idx+1}_AI.mp4")
                    shutil.copy(save_path, final_path)
                    os.remove(save_path)
                    videos.append({
                        'path': final_path,
                        'final_path': final_path,
                        'filename': f"V{idx+1}_AI.mp4",
                        'type': 'ai'
                    })
                    self.logger.log(drama_name, 'INFO', f'V{idx+1} AI版本已下载')
                except Exception as e:
                    self.logger.log(drama_name, 'ERROR', f'下载V{idx+1}失败：{str(e)}')
            
            return videos
            
        except Exception as e:
            self.logger.log(drama_name, 'ERROR', f'AI高光智剪异常：{str(e)}')
            return []
    
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
                segments = self._generate_reverse_segments(edl_eps, v_idx)
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
            
            self.logger.log(drama_name, 'INFO', f'生成EDL配置V{v_idx+1}：{len(segments)}个片段')
            
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
                    output_files = [f for f in os.listdir(out_dir) if f.endswith(".mp4") and f"V{v_idx+1}" in f]
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
    
    def _generate_reverse_segments(self, edl_eps, v_idx):
        """生成倒叙结构的segments"""
        segments = []
        
        # 倒叙结构：开头炸点→倒回起因→发展→高潮→再升级→结尾钩子
        # 从后往前取素材
        eps = list(edl_eps)
        random.shuffle(eps)
        eps = eps[:10]  # 最多10集
        
        if not eps:
            return []
        
        # 开头炸点（最后一集）
        last_ep = eps[-1] if eps else None
        if last_ep:
            duration = self._get_video_duration(last_ep)
            # 跳过损坏的文件
            if duration >= 30:
                # 确保片段在文件范围内
                max_start = duration - 5
                seg_duration = min(random.uniform(30, 50), duration - 10)
                seg_start = random.uniform(0, max(max_start - seg_duration, 0))
                segments.append({
                    "episode": self._get_episode_num(last_ep),
                    "start": round(seg_start, 1),
                    "duration": round(seg_duration, 1)
                })
        
        # 中间部分（倒序）
        for ep_file in reversed(eps[:-1]):
            duration = self._get_video_duration(ep_file)
            # 跳过损坏或过短的文件
            if duration < 30:
                continue
            
            num_segments = random.randint(2, 3)
            start = random.uniform(5, 15)
            
            for _ in range(num_segments):
                # 确保片段不超过剩余时长
                remaining = duration - start - 5
                if remaining < 15:
                    break
                seg_duration = min(random.uniform(30, 75), remaining)
                if seg_duration < 15:
                    break
                segments.append({
                    "episode": self._get_episode_num(ep_file),
                    "start": round(start, 1),
                    "duration": round(seg_duration, 1)
                })
                start += seg_duration + random.uniform(1, 3)
        
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
                # 确保片段不超过剩余时长
                remaining = duration - start - 5
                if remaining < 15:
                    break
                seg_duration = min(random.uniform(30, 75), remaining)
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
        
        vf = (
            "setpts=PTS/1.05,"
            "crop=1080:1860:0:30,pad=1080:1920:0:30:black,"
            "eq=contrast=1.05:saturation=1.1:brightness=0.02,"
            "noise=alls=8:allf=t,rotate=0.008:fillcolor=black,fps=29.97,"
            f"drawtext=fontfile='C:/Windows/Fonts/msyh.ttc':"
            f"text='{watermark}':fontsize=36:fontcolor=white@0.3:"
            f"x={pos[0]}:y={pos[1]}"
        )
        af = "atempo=1.05,equalizer=f=3000:t=h:width=1000:g=3," \
             "equalizer=f=100:t=h:width=50:g=-2"
        
        cmd = [
            CONFIG['ffmpeg_path'], "-y", "-i", input_path,
            "-vf", vf, "-af", af,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            stderr_text = result.stderr.decode("utf-8", errors="ignore")[:300]
            self.logger.log('system', 'ERROR', f'后处理失败(抗查重未完成，跳过此条不裸发): {stderr_text}')
            # 不再 copy 原文件——裸视频直接发会被平台判重。
            # 删掉半成品 output，让外层跳过这条。
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            return False

        # 生成封面
        self._generate_cover(input_path, output_path,
                           drama_name=drama_name,
                           src_dir=src_dir,
                           version_idx=version_idx)
        return True
    
    def _get_version_idx(self, filepath):
        """从文件名获取版本序号"""
        try:
            fname = os.path.basename(filepath)
            match = re.search(r'V(\d+)', fname)
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
            
            # 3. 兜底：从视频第5秒截取
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
        """给封面加角标"""
        badges = {1: "第1集", 2: "第2集", 3: "第3集", 4: "第4集", 5: "完整版",
                  6: "大结局", 7: "加长版", 8: "精编版"}
        badge_text = badges.get(version_idx, f"精编版V{version_idx}")
        
        try:
            cmd = [
                CONFIG['ffmpeg_path'], "-y",
                "-i", cover_path,
                "-vf", f"drawtext=fontfile='C:\\:/Windows/Fonts/msyhbd.ttc':"
                       f"text='{badge_text}':fontsize=52:fontcolor=white:"
                       f"borderw=3:bordercolor=black:"
                       f"x=w-text_w-30:y=h-text_h-30",
                "-q:v", "2",
                cover_path
            ]
            subprocess.run(cmd, capture_output=True, timeout=30)
            self.logger.log("system", 'INFO', f'角标已添加：{badge_text}')
        except Exception as e:
            self.logger.log("system", 'WARN', f'角标添加失败：{str(e)}')
    
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