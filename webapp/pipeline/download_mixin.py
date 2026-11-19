# -*- coding: utf-8 -*-
"""PipelineProcessor 的 download 域实现（P1-5 拆分，方法体原样搬迁）。"""
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
from pipeline import duration as _duration_mod
from pipeline import edl_approval as _edl_mod


class DownloadMixin:
    _AUTO_RESUME_FAIL_MARKERS = ('下载卡顿', '下载超时', '下载不完整',
                                 '下载完成但未找到文件', '下载异常',
                                 'Failed after')

    _AUTO_RESUME_EXCLUDE_MARKERS = ('转存失败', '文件已存在于网盘但未找到目录')

    _AUTO_RESUME_MAX = 3  # 同一任务自动续传上限，防永久失效源无限重试

    TRANSFER_ERRNO_HINTS = {
        '-12': '分享链接不存在或已失效（链接可能被截断或已被删除）',
    }

    PERMANENT_ERROR_MARKERS = ('分享链接不存在', '分享链接已失效', '提取码错误',
                               '密码错误', '文件已失效', '文件已取消')

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
                        'attempts': attempts,
                        # 关键字段必须透传：切片按 drama_name 归档、P7 完整性闸门
                        # 按 episode_complete/total_episodes 判定。旧实现丢字段导致
                        # ①切片状态写进 link_id 的重复档案行 ②残缺下载不再被门控。
                        'drama_name': result.get('drama_name'),
                        'target_dir': result.get('target_dir'),
                        'episode_complete': result.get('episode_complete'),
                        'total_episodes': result.get('total_episodes'),
                        'missing_episodes': result.get('missing_episodes'),
                        'transferred': result.get('transferred'),
                    }
                else:
                    last_error = result.get('error', 'Unknown error')
                    # 单次失败后立即标记 failed，让下一次重试不被旧目录防重挡住；
                    # 最终成功时会被 done 覆盖。
                    try:
                        from archive import upsert_archive as _arch_up
                        from archive import get_archive_name_by_source as _arch_name_by_src
                        _fail_name = (_arch_name_by_src(link_info.get('link')) or
                                      result.get('drama_name') or
                                      link_info['link_id'])
                        _arch_up(_fail_name, download_state='failed',
                                 source_url=link_info.get('link'),
                                 source_passwd=link_info.get('pwd'),
                                 download_error=str(last_error)[:500])
                    except Exception as _e:
                        self.logger.log(link_info['link_id'], 'WARN',
                                        f'archive 下载重试状态回填失败：{_e}')
                    # 永久错误（链接不存在/已失效/提取码错误）重试无意义，立即结束
                    if self._is_permanent_download_error(last_error):
                        self.logger.log(link_info['link_id'], 'ERROR',
                                        f'永久错误，不再重试：{last_error}')
                        break
            
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
            from archive import get_archive_name_by_source as _arch_name_by_src
            _fail_name = (_arch_name_by_src(link_info.get('link')) or
                          ((last_result or {}).get('drama_name') or
                           link_info['link_id'] or
                           link_info['link'].split('/')[-1].split('?')[0]))
            _arch_up(_fail_name, download_state='failed',
                     source_url=link_info.get('link'),
                     source_passwd=link_info.get('pwd'),
                     download_error=f'Failed after {attempts} attempts: {last_error}')
        except Exception as _e:
            self.logger.log(link_info['link_id'], 'WARN', f'archive 下载失败标记失败：{_e}')
        return {
            'link': link_info['link'],
            'link_id': link_info['link_id'],
            'pwd': link_info['pwd'],
            'success': False,
            'error': f'Failed after {attempts} attempts: {last_error}',
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
                _drama_guess, _local, source_url=link,
                min_files=int(CONFIG['download'].get('min_manual_files', 2)))
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
                    material_dirs = self._find_dir_in_cloud('/短剧素材')
                    if dir_name in material_dirs:
                        # /短剧素材/ 已有同名目录：本次转存只是重复副本，
                        # 直接 mv 会形成 剧名/剧名 嵌套，必须先清掉副本再复用已有目录
                        self.logger.log(dir_name, 'INFO', '网盘已存在同名目录，清理本次重复转存的副本')
                        self._delete_cloud_dir(dir_name)
                    else:
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
                # 重复转存：只认"链接→剧名"的权威映射（候选库/档案失败行/分享码），
                # 绝不按"本地哪个目录存在就选哪个"猜测，否则会把别的剧当成本次目标。
                self.logger.log(link_dir_name, 'INFO', '分享文件已在网盘（重复），按链接映射定位目录...')
                dir_name = self._resolve_cloud_dir_for_link(link, link_dir_name)
                if not dir_name:
                    raw = (process.stdout or process.stderr or '').strip()[:200]
                    self.logger.log(link_dir_name, 'ERROR',
                                    f'网盘已存在但无法确定剧名，禁止猜测：{raw}')
                    return {
                        'success': False,
                        'downloaded_files': [],
                        'target_dir': None,
                        'drama_name': link_dir_name,
                        'error': '网盘文件已存在但无法确定对应剧名，请人工确认后重试',
                        'transferred': False
                    }
                self.logger.log(dir_name, 'INFO', f'按链接映射定位到网盘目录：{dir_name}')
            else:
                error_msg = process.stdout[:300] if process.stdout else process.stderr[:300]
                error_msg = self._translate_transfer_error(error_msg)
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
            if not downloaded_files:
                # 修复竞态：命令返回后文件上移/落盘可能滞后数百毫秒，
                # 旧实现立即判「下载完成但未找到文件」→ 触发重试→重复转存，
                # 网盘里多出一份副本。这里最多等 10s 并反复整理暂存区。
                for _wait in range(5):
                    time.sleep(2)
                    try:
                        self._normalize_download_tree(staging_dir, target_dir, dir_name)
                    except Exception:
                        pass
                    downloaded_files = self._scan_files_recursive(target_dir)
                    if downloaded_files:
                        self.logger.log(
                            dir_name, 'INFO',
                            f'等待{(_wait + 1) * 2}s后素材落地，继续后续校验')
                        break
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
                self.logger.log(dir_name, 'INFO', '开始清理网盘文件...')
                self._delete_cloud_dir(dir_name, reason='下载完成')
                
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
                # 永久错误不自动重试（即使被 'Failed after' 包装命中瞬态标记）。
                if any(m in err for m in self._AUTO_RESUME_EXCLUDE_MARKERS):
                    continue
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

    def resolve_orphan_download_job(self, job_id, reason):
        """启动恢复把「素材已就绪」的孤儿下载任务标记为已解决，不再续传。

        崩溃遗留的孤儿（status=downloading、心跳冻结）若其素材已被后续任务
        下载完成，启动恢复会在每次重启都命中 stale_jobs 并重复提交同一条
        链接，触发「跳过：状态文件显示已完成」噪音。此方法把孤儿置为
        failed + auto_resume 满额（双保险），使 find_orphaned_download_jobs
        不再把它当待续传任务。
        """
        try:
            store = self._download_job_store()
            job = store.get(job_id)
            if not job:
                return
            params = dict(job.get('params') or {})
            params['auto_resume'] = self._AUTO_RESUME_MAX
            store.update(job_id, status='failed', error=reason,
                         finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                         params=params)
        except Exception as e:
            self.logger.log('system', 'WARN', f'标记孤儿任务已解决失败：{e}')

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

    def _translate_transfer_error(self, raw):
        """把 BaiduPCS-Go 的裸错误码翻成可操作提示，原始信息保留。"""
        text = str(raw or '').strip()
        m = re.search(r'错误码\s*(-?\d+)', text)
        if not m:
            return text
        hint = self.TRANSFER_ERRNO_HINTS.get(m.group(1))
        return f'{hint}（原始：{text}）' if hint else text

    def _is_permanent_download_error(self, err):
        """链接失效/提取码类错误重试无意义。"""
        text = str(err or '')
        if '错误码-12' in text:
            return True
        return any(marker in text for marker in self.PERMANENT_ERROR_MARKERS)

    def _cloud_dir_where(self, dir_name):
        """返回目录在网盘中的真实路径（根目录优先），不存在返回 None。"""
        dir_name = (dir_name or '').strip().rstrip('/')
        if not dir_name:
            return None
        if dir_name in self._find_dir_in_cloud('/'):
            return f'/{dir_name}'
        if dir_name in self._find_dir_in_cloud('/短剧素材'):
            return f'/短剧素材/{dir_name}'
        return None

    def _delete_cloud_dir(self, dir_name, reason=''):
        """删除网盘目录并回查确认。

        BaiduPCS-Go 对失败也返回 0（且旧版代码用的 delete 命令根本不存在），
        所以只能以「目录是否真的从父目录列表消失」作为成功判据。
        """
        path = self._cloud_dir_where(dir_name)
        if not path:
            self.logger.log(dir_name, 'WARN', f'清理网盘跳过：目录不存在（{reason}）')
            return True
        try:
            proc = subprocess.run(
                [CONFIG['baidupcs_path'], 'rm', path],
                capture_output=True, text=True, encoding='utf-8',
                timeout=CONFIG['download'].get('delete_timeout', 120),
                cwd=os.path.dirname(CONFIG['baidupcs_path']))
            # 网盘目录列表有延迟，删除后回查要留重试窗口
            for idx in range(3):
                if not self._cloud_dir_where(dir_name):
                    self.logger.log(dir_name, 'INFO', f'网盘文件已清理（{path}）')
                    return True
                if idx < 2:
                    time.sleep(2)
                return True
            detail = (proc.stdout or proc.stderr or '').strip()[:120]
            self.logger.log(dir_name, 'WARN', f'清理网盘未生效：{path} 仍存在（{detail}）')
            return False
        except Exception as e:
            self.logger.log(dir_name, 'WARN', f'清理网盘异常：{e}')
            return False

    def _resolve_cloud_dir_for_link(self, link, fallback_name):
        """重复转存时按「链接→剧名」的权威映射定位网盘目录，无法唯一确定就返回 None。

        顺序：候选库剧名 → 档案里未成功的行（名字来自真实下载过程）→ 分享码目录名。
        只接受确实存在于网盘目录列表里的名字，禁止「本地哪个目录存在就选哪个」的猜测。
        """
        names = []
        try:
            from candidate_library import titles_by_source
            names.extend(titles_by_source(link))
        except Exception as e:
            self.logger.log('system', 'WARN', f'候选库链接映射查询失败：{e}')
        try:
            from archive import get_archive_rows_by_source
            for row in get_archive_rows_by_source(link):
                # 完成行可能被历史写入污染，只认失败/进行中的行
                if row.get('download_state') != 'done':
                    names.append(row.get('drama_name'))
        except Exception as e:
            self.logger.log('system', 'WARN', f'档案链接映射查询失败：{e}')
        names.append(fallback_name)

        cloud_dirs = set(self._find_dir_in_cloud('/')) | set(self._find_dir_in_cloud('/短剧素材'))
        for name in names:
            name = str(name or '').strip().rstrip('/')
            if name and name in cloud_dirs:
                return name
        return None

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

    def _resolve_download_drama_name(self, d):
        """解析下载结果的权威剧名：drama_name → 素材目录名 → link_id。

        修复：_download_with_retry 旧实现丢掉 drama_name，cut_batch 便用
        link_id 当剧名写档案，drama_archive 出现分享码键的重复行，真实剧名
        行的 slice_state 永远停在 none。
        """
        name = (d.get('drama_name') or '').strip()
        files = d.get('downloaded_files') or []
        file_dir = os.path.basename(os.path.dirname(files[0])) if files else ''
        target_dir = (d.get('target_dir') or '').rstrip('/\\')
        td_base = os.path.basename(target_dir) if target_dir else ''
        base = os.path.basename(CONFIG['download_base_dir'])
        for cand in (name, file_dir, td_base):
            if cand and cand != base and not self._looks_like_link_id(cand):
                return cand
        return name or d.get('link_id', '')

    @staticmethod
    def _looks_like_link_id(name):
        """分享码形态（纯字母数字/下划线、无中文）→ 不是剧名。"""
        if not name:
            return False
        return bool(re.fullmatch(r'[0-9A-Za-z_\-]{20,}', name))

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
