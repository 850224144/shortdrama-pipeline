# -*- coding: utf-8 -*-
"""独立的候选选择、下载和现有切片队列编排入口。"""
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime

import candidate_library as library
from job_runtime import execution_gate


ACCOUNTS = ('小桃追剧中', '福桃说剧')


class AutomaticSelection:
    """串行逐剧下载、以现有门控判定，再只启动本轮新增切片任务。"""

    def __init__(self, processor, cut_manager, config, logger):
        self.processor = processor
        self.cut_manager = cut_manager
        self.config = config
        self.logger = logger
        self.lock = threading.Lock()
        self._start_lock = threading.Lock()
        self.thread = None
        self.active = False
        self._cut_waiting = False
        self.run_id = None
        self.cancel_event = None
        self._pipeline_held = False

    def status(self):
        run = library.get_auto_run(self.run_id) if self.run_id else library.get_auto_run()
        active = bool(self.active or self._cut_waiting or
                      (self.thread and self.thread.is_alive()))
        if run:
            run = self._refresh_run_cut_states(run)
        return {'running': active, 'run': run}

    def _refresh_run_cut_states(self, run):
        task_map = {task.get('id'): task for task in self.cut_manager.queue}
        changed = False
        for item in run.get('items', []):
            task = task_map.get(item.get('cut_task_id'))
            if not task:
                continue
            state = ('cut_done' if task.get('status') == 'completed' else
                     'cut_failed' if task.get('status') == 'failed' else
                     'cutting' if task.get('status') == 'cutting' else None)
            if state and state != item.get('status'):
                fields = {'status': state}
                if state == 'cut_failed':
                    fields['error'] = task.get('error')
                library.update_run_item(item['id'], **fields)
                item.update(fields)
                changed = True
        if changed:
            run = library.get_auto_run(run.get('id')) or run
        active_items = any(item.get('status') in ('queued', 'cutting')
                           for item in run.get('items', []))
        if not active_items and not self.active and not self._cut_waiting and run.get('status') in (
                'cutting', 'selecting', 'downloading'):
            failed = any(item.get('status') in (
                'cut_failed', 'queue_failed', 'failed', 'no_candidate')
                         for item in run.get('items', []))
            status = 'partial_failure' if failed else 'completed'
            library.update_auto_run(run['id'], status=status,
                                    finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            run = library.get_auto_run(run['id']) or run
        return run

    def start(self, count=2):
        try:
            count = max(1, min(int(count or 2), 2))
        except (TypeError, ValueError):
            count = 2
        if not self._start_lock.acquire(blocking=False):
            return {'status': 'failed', 'error': '自动选剧任务正在启动'}, 409
        execution_gate_acquired = False
        try:
            with self.lock:
                try:
                    if getattr(self, '_starting', False) or self.active or (self.thread and self.thread.is_alive()):
                        return {'status': 'failed', 'error': '自动选剧任务正在运行',
                                'run_id': self.run_id}, 409
                    self.run_id = None
                    if self.cut_manager.cutting or (self.cut_manager.cut_thread and
                                                    self.cut_manager.cut_thread.is_alive()):
                        return {'status': 'failed', 'error': '现有切片任务正在运行，请完成后再启动'}, 409
                    if not execution_gate.try_acquire_pipeline():
                        return {'status': 'failed', 'error': '处理资源正被其他任务占用'}, 409
                    execution_gate_acquired = True
                    self._starting = True
                    ok, reason = self._check_pipeline_resources()
                    if not ok:
                        return {'status': 'failed', 'error': reason}, 409
                    candidates = library.eligible_candidates()
                    if not candidates:
                        return {'status': 'failed', 'error': '没有符合近30天、未发布且带有效提取码的候选'}, 200
                    ready = []
                    duplicate_links = set()
                    for candidate in candidates:
                        if not self._valid_title(candidate.get('title')):
                            continue
                        already, _reason = self._is_local_or_archived(candidate)
                        if already or self._content_fingerprint_exists(candidate['title']):
                            continue
                        link_info = self._parse_link_info(candidate)
                        if not link_info:
                            continue
                        source_url = link_info.get('link')
                        if not source_url or source_url in duplicate_links:
                            continue
                        duplicate_links.add(source_url)
                        candidate['link_info'] = link_info
                        ready.append(candidate)
                    candidates = ready
                    if not candidates:
                        return {'status': 'failed', 'error':
                                '近30天候选均已下载/切片/发布、缺少提取码或无法识别有效链接'}, 200
                    login_ok, login_info = self._baidu_login_status()
                    if not login_ok:
                        return {'status': 'failed',
                                'error': f'BaiduPCS-Go 未登录或不可用：{login_info}'}, 409
                    run_id = uuid.uuid4().hex
                    library.add_auto_run(run_id, count)
                    self.run_id = run_id
                    self.active = True
                    self.cancel_event = threading.Event()
                    self._pipeline_held = True
                    self.thread = threading.Thread(target=self._run,
                                                   args=(run_id, count, candidates),
                                                   name=f'auto-selection-{run_id[:8]}',
                                                   daemon=True)
                    self.thread.start()
                    self._starting = False
                    # Keep the pipeline reservation across the download -> cut handoff.
                    execution_gate_acquired = False
                    return {'status': 'started', 'run_id': run_id,
                            'message': '自动选剧已启动；下载成功并通过门控的剧会进入切片队列'}, 202
                except Exception as exc:
                    self._starting = False
                    if self.run_id and self.active and not (self.thread and self.thread.is_alive()):
                        self.active = False
                        self._pipeline_held = False
                        self.cancel_event = None
                        try:
                            library.update_auto_run(
                                self.run_id, status='failed', error=str(exc)[:1000],
                                finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                        except Exception:
                            pass
                    return {'status': 'failed', 'error': f'自动选剧启动失败：{exc}'}, 500
        finally:
            if execution_gate_acquired:
                execution_gate.release_pipeline()
            self._starting = False
            self._start_lock.release()

    def _baidu_login_status(self):
        exe = self.config.get('baidupcs_path')
        if not exe or not os.path.isfile(exe):
            return False, '找不到 BaiduPCS-Go'
        try:
            result = subprocess.run([exe, 'who'], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=10,
                                    cwd=os.path.dirname(exe))
            output = (result.stdout or '') + (result.stderr or '')
            return (result.returncode == 0 and
                    ('uid' in output.lower() or '账号' in output),
                    output[:200] or '未发现登录账号')
        except Exception as exc:
            return False, str(exc)

    def stop(self):
        if not self.cancel_event or not (self.active or self._cut_waiting) or not self.thread or not self.thread.is_alive():
            return {'status': 'failed', 'error': '没有正在运行的自动选剧任务'}, 409
        if self.active:
            self.cancel_event.set()
        if self.cut_manager.cutting:
            self.cut_manager.stop_cutting()
        elif self._cut_waiting and self.cut_manager.paused:
            self.cut_manager.resume_cutting()
        return {'status': 'success', 'message': '已请求停止；当前下载或切片命令退出后停止后续工作'} , 200

    def _min_free_gb(self):
        paths = [self.config.get('download_base_dir'), self.config.get('output_dir')]
        free = []
        for path in paths:
            if not path:
                return None
            try:
                free.append(shutil.disk_usage(path).free / (1024 ** 3))
            except OSError:
                return None
        return min(free) if free else None

    def _check_pipeline_resources(self):
        ok, reason = self.processor.check_resources()
        if not ok:
            return False, reason
        paths = [self.config.get('download_base_dir'), self.config.get('output_dir')]
        if not all(paths):
            return False, '下载目录或输出目录未配置'
        minimum = float((self.config.get('resource') or {}).get('min_disk_gb', 100))
        try:
            checked = {}
            for path in paths:
                drive = os.path.splitdrive(os.path.abspath(path))[0].casefold()
                if drive and drive in checked:
                    continue
                free = shutil.disk_usage(path).free / (1024 ** 3)
                checked[drive or os.path.abspath(path)] = free
                if free < minimum:
                    return False, f'磁盘空间不足：{path} 剩余 {free:.1f}GB，要求至少 {minimum:.0f}GB'
            return True, '资源充足'
        except OSError as exc:
            return False, f'磁盘空间检查失败：{exc}'

    @staticmethod
    def _published_in_archive(candidate):
        """正式发布须看 CSV 发布列与归档发布标记，不能把草稿当成已发布。"""
        try:
            from archive import get_archive
            row = get_archive(candidate['title'])
            if row and row.get('notes') and '正式发布' in row['notes']:
                return True
        except Exception:
            return False
        return False

    def _is_local_or_archived(self, candidate):
        title = candidate['title']
        local = os.path.join(self.config['download_base_dir'], title)
        row = None
        try:
            from archive import get_archive, get_archive_by_source
            row = get_archive(title)
            if row and row.get('download_state') == 'done':
                return True, '档案已有下载记录'
            if row and row.get('slice_state') == 'done':
                return True, '档案已有切片记录'
            source = get_archive_by_source(candidate['source_url'])
            if source and source.get('download_state') == 'done':
                return True, '来源链接已有成功下载记录'
        except Exception:
            pass
        if os.path.isdir(local) and not (row and row.get('download_state') == 'failed'):
            return True, '本地素材目录已存在'
        if row and row.get('download_state') == 'downloading':
            return True, '档案记录显示下载中'
        if row and row.get('slice_state') == 'cutting':
            return True, '档案记录显示切片中'
        if self._published_in_archive(candidate):
            return True, '档案已记录正式发布'
        if self._has_published_record(title):
            return True, '归档上传记录显示已发布'
        return False, ''

    def _has_published_record(self, title):
        """Use the existing upload archive as a second source for publish history."""
        import sqlite3
        db_path = os.path.join(self.config.get('data_dir', ''), 'upload.db')
        if not os.path.isfile(db_path):
            return False
        try:
            with sqlite3.connect(db_path, timeout=2) as conn:
                return conn.execute(
                    "SELECT 1 FROM upload_records WHERE drama_name=? "
                    "AND status='completed' LIMIT 1", (title,)).fetchone() is not None
        except sqlite3.Error:
            return False

    @staticmethod
    def _valid_title(title):
        return bool(title and not any(ch in title for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|'))
                    and '..' not in title)

    def _parse_link_info(self, candidate):
        if candidate.get('link_info'):
            return dict(candidate['link_info'])
        url = candidate.get('source_url') or ''
        pwd = candidate.get('source_passwd') or ''
        if not url or not pwd:
            return None
        from processor import LinkExtractor
        extracted = LinkExtractor.extract(url)
        if not extracted:
            return None
        info = extracted[0]
        info['link'] = url
        info['pwd'] = pwd
        info['candidate_title'] = candidate['title']
        return info

    def _content_fingerprint_exists(self, title):
        key = library.normalize_title(title)
        try:
            from archive import get_archive, get_archive_by_source
            candidate = library.candidate_by_title(title) or {}
            if candidate.get('source_url'):
                source_record = get_archive_by_source(candidate['source_url'])
                if source_record and source_record.get('download_state') == 'done':
                    return True
            if library.DB_PATH and os.path.isfile(library.DB_PATH):
                conn = library._connect()
                try:
                    row = conn.execute('SELECT 1 FROM source_fingerprints WHERE title_key=?',
                                       (key,)).fetchone()
                    if row:
                        return True
                finally:
                    conn.close()
            record = get_archive(title) or {}
            local_dir = record.get('local_dir') or os.path.join(
                self.config['download_base_dir'], title)
            upload_db = os.path.join(self.config['data_dir'], 'upload.db')
            if os.path.isfile(upload_db):
                import sqlite3
                conn = sqlite3.connect(upload_db, timeout=2)
                try:
                    if conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='content_fingerprints'").fetchone():
                        rows = conn.execute(
                            'SELECT drama_name FROM content_fingerprints WHERE drama_name=? LIMIT 1',
                            (title,)).fetchone()
                        if rows:
                            return True
                finally:
                    conn.close()
            if os.path.isdir(local_dir) and not (
                    (record or {}).get('download_state') == 'failed'):
                for root, _dirs, files in os.walk(local_dir):
                    if any(name.lower().endswith('.mp4') and
                           '.downloading' not in name.lower() for name in files):
                        return True
            return False
        except Exception as exc:
            # If optional fingerprint storage is unhealthy, avoid downloading a
            # possible duplicate silently; surface an explicit skip reason.
            self.logger.log(title, 'WARN', f'内容指纹检查失败，保守跳过：{exc}')
            return True

    def _download(self, candidate):
        if not self._valid_title(candidate['title']):
            return None, '剧名含不安全路径字符'
        link_info = self._parse_link_info(candidate)
        if not link_info:
            return None, '链接格式或提取码无效'
        result = None
        error = None
        attempts = 1
        for attempt in range(attempts):
            if self.cancel_event.is_set():
                return None, '用户已停止任务'
            try:
                result = self.processor._download_once(link_info,
                                                       cancel_event=self.cancel_event)
            except Exception as exc:
                result = {'success': False, 'error': str(exc),
                          'drama_name': candidate['title']}
            if result.get('success'):
                break
            error = result.get('error') or '下载失败'
            if self._is_permanent_download_error(error):
                break
            if attempt + 1 < attempts:
                self.cancel_event.wait(min(2 ** attempt, 8))
        if not result.get('success'):
            return None, error or result.get('error') or '下载失败'
        downloaded = [p for p in result.get('downloaded_files', [])
                      if p.lower().endswith('.mp4') and '.downloading' not in p.lower()]
        if not downloaded:
            return None, '完整性门控失败：未找到 MP4 文件'
        if any(not os.path.isfile(path) or os.path.getsize(path) <= 5 * 1024 * 1024
               for path in downloaded):
            return None, '完整性门控失败：MP4 不存在或小于 5MB'
        # 复用主处理流程 P7 对异常 incomplete 标记的阈值，不改原有流程。
        total = result.get('total_episodes') or 0
        ratio = len(downloaded) / total if total else 1.0
        if result.get('episode_complete') is False:
            min_eps = int(self.config.get('download', {}).get('min_download_files', 10))
            min_ratio = float(self.config.get('download', {}).get('min_completeness', 0.5))
            if len(downloaded) < min_eps or ratio < min_ratio:
                return None, f'完整性门控失败：{len(downloaded)}/{total} 集'
        checker = self.config.get('ffprobe_path')
        if not checker or not os.path.isfile(checker):
            checker = shutil.which('ffprobe')
        if not checker:
            return None, '完整性门控失败：系统找不到 ffprobe'
        for path in downloaded:
            try:
                probe = subprocess.run(
                    [checker, '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', path],
                    capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=30)
                if probe.returncode != 0 or float((probe.stdout or '0').strip() or 0) <= 0:
                    return None, '完整性门控失败：ffprobe 无法读取视频'
            except Exception as exc:
                return None, f'完整性门控失败：{exc}'
        title = os.path.basename(os.path.dirname(downloaded[0]))
        result['actual_dir_name'] = title
        result['cloud_drama_name'] = result.get('drama_name') or title
        result['drama_name'] = candidate['title']
        result['downloaded_files'] = downloaded
        result['target_dir'] = os.path.dirname(downloaded[0])
        result['archive_name'] = result['cloud_drama_name']
        return result, None

    @staticmethod
    def _is_permanent_download_error(error):
        text = str(error or '').casefold()
        return any(marker in text for marker in (
            '提取码', '密码错误', '分享已取消', '分享文件已取消',
            '分享链接不存在', '分享文件不存在', '分享已过期', '链接失效',
            '鉴权失败', '不存在', '404'))

    def _enqueue_cut(self, candidate, download_result, account, run_id, item_id):
        actual = download_result['drama_name']
        source_dir = download_result.get('target_dir') or os.path.dirname(
            download_result['downloaded_files'][0])
        target_dir = os.path.join(self.config['download_base_dir'], candidate['title'])
        if os.path.abspath(source_dir) != os.path.abspath(target_dir):
            if os.path.exists(target_dir):
                return None, '候选标题目录已存在，安全停止以免覆盖素材'
            shutil.move(source_dir, target_dir)
            source_dir = target_dir
            download_result['downloaded_files'] = [
                os.path.join(target_dir, os.path.basename(path))
                for path in download_result['downloaded_files']]
            download_result['target_dir'] = target_dir
        queued = self.cut_manager.add_to_queue(
            candidate['title'], account=account, auto_selected=True,
            auto_run_id=run_id)
        if queued.get('status') != 'success':
            return None, queued.get('error') or '切片入队失败'
        library.update_run_item(item_id, status='queued', archive_name=candidate['title'],
                                cut_task_id=queued.get('id'))
        return queued.get('id'), None

    def _run(self, run_id, count, candidates):
        errors = []
        warnings = []
        successful = []
        items_to_cut = []
        attempted = set()
        acquired = self._pipeline_held
        cut_started = False
        attempted_title_keys = set()
        try:
            # The orchestrator keeps the shared gate through download and cut handoff.
            for slot in range(count):
                account = ACCOUNTS[slot]
                chosen = None
                item_id = None
                failure_count = 0
                for candidate in candidates:
                    key = candidate['title_key']
                    if key in attempted or key in attempted_title_keys:
                        continue
                    attempted.add(key)
                    if self._content_fingerprint_exists(candidate['title']):
                        self.logger.log(candidate['title'], 'INFO',
                                        '自动选剧跳过：档案/本地/内容指纹已存在')
                        continue
                    if self.cancel_event.is_set():
                        break
                    attempted_title_keys.add(key)
                    _, entry = self._record_attempt(run_id, slot + 1, account,
                                                    candidate, 'downloading')
                    library.update_auto_run(run_id, status='downloading')
                    try:
                        from archive import upsert_archive
                        archive_fields = {
                            'download_state': 'downloading',
                            'source_url': candidate.get('source_url'),
                            'source_passwd': candidate.get('source_passwd'),
                        }
                        if candidate.get('rating'):
                            archive_fields['rating'] = candidate['rating']
                        if candidate.get('category'):
                            archive_fields['category'] = candidate['category']
                        upsert_archive(candidate['title'], **archive_fields)
                    except Exception as exc:
                        self.logger.log(candidate['title'], 'WARN',
                                        f'下载开始时写入剧档案失败：{exc}')
                    failure_count += 1
                    self.logger.log(candidate['title'], 'INFO',
                                    f'自动选剧：开始下载，账号 {account}')
                    result, error = self._download(candidate)
                    if error:
                        if self.cancel_event.is_set():
                            library.update_run_item(entry, status='download_stopped',
                                                    attempts=1, error=error)
                            break
                        library.update_run_item(entry, status='failed', attempts=1, error=error)
                        try:
                            from archive import upsert_archive
                            from archive import get_archive_by_source, rename_download_archive
                            source_row = get_archive_by_source(candidate.get('source_url'))
                            provisional = (source_row or {}).get('drama_name')
                            if provisional and provisional != candidate['title']:
                                renamed = rename_download_archive(
                                    provisional, candidate['title'],
                                    source_url=candidate.get('source_url'))
                            else:
                                renamed = True
                            if not renamed:
                                self.logger.log(candidate['title'], 'WARN',
                                                '下载失败档案仍以网盘目录名保存，避免覆盖候选档案')
                            upsert_archive(candidate['title'], download_state='failed',
                                           source_url=candidate.get('source_url'),
                                           source_passwd=candidate.get('source_passwd'),
                                           download_error=str(error)[:500])
                        except Exception:
                            pass
                        warnings.append(f"{candidate['title']}：{error}（已跳过并尝试替补）")
                        self.logger.log(candidate['title'], 'WARN', f'自动选剧跳过：{error}')
                        continue
                    actual = candidate['title']
                    cloud_name = result.get('cloud_drama_name') or result.get('actual_dir_name')
                    target_dir = os.path.join(self.config['download_base_dir'],
                                              candidate['title'])
                    if cloud_name and cloud_name != candidate['title']:
                        actual_dir = result.get('target_dir') or os.path.dirname(
                            result['downloaded_files'][0])
                        if os.path.exists(target_dir) and os.path.abspath(target_dir) != os.path.abspath(actual_dir):
                            error = '下载目录与候选标题目录冲突，已停止以避免覆盖'
                            library.update_run_item(entry, status='failed', attempts=1,
                                                    error=error)
                            warnings.append(f"{candidate['title']}：{error}")
                            continue
                        if os.path.abspath(actual_dir) != os.path.abspath(target_dir):
                            try:
                                shutil.move(actual_dir, target_dir)
                                result['downloaded_files'] = [
                                    os.path.join(target_dir, os.path.basename(path))
                                    for path in result['downloaded_files']]
                                result['target_dir'] = target_dir
                            except Exception as exc:
                                error = f'下载目录整理失败：{exc}'
                                library.update_run_item(entry, status='failed', attempts=1,
                                                        error=error)
                                warnings.append(f"{candidate['title']}：{error}")
                                continue
                    try:
                        from archive import merge_auto_download_archive
                        old_key = result.get('archive_name') or cloud_name
                        if old_key:
                            if not merge_auto_download_archive(
                                    old_key, candidate['title'],
                                    candidate.get('source_url'), local_dir=target_dir):
                                raise RuntimeError('来源档案与候选预留记录无法安全合并')
                    except Exception as exc:
                        self.logger.log(candidate['title'], 'WARN',
                                        f'下载档案标题归一失败：{exc}')
                    result['target_dir'] = target_dir
                    result['drama_name'] = candidate['title']
                    chosen, item_id = candidate, entry
                    # 候选信息补写入档案，下载器已先写真实成功状态。
                    library.update_run_item(item_id, status='downloaded', attempts=1,
                                            archive_name=actual)
                    successful.append((candidate, result, account, item_id))
                    try:
                        from archive import upsert_archive
                        mp4s = result['downloaded_files']
                        size = sum(os.path.getsize(path) for path in mp4s
                                   if os.path.isfile(path)) // (1024 * 1024)
                        archive_fields = {
                            'download_state': 'done',
                            'source_url': candidate.get('source_url'),
                            'source_passwd': candidate.get('source_passwd'),
                            'local_dir': result.get('target_dir'),
                            'download_files': len(mp4s),
                            'episode_count': len(mp4s),
                            'download_size': size,
                            'account': account,
                            'downloaded_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'download_error': None,
                        }
                        for field in ('rating', 'category'):
                            if candidate.get(field):
                                archive_fields[field] = candidate[field]
                        upsert_archive(candidate['title'], **archive_fields)
                    except Exception as exc:
                        self.logger.log(candidate['title'], 'WARN',
                                        f'下载后写入剧级档案失败：{exc}')
                    for first_video in result['downloaded_files']:
                        try:
                            from content_fingerprint import extract_gray_frames, frame_dhash
                            frames = extract_gray_frames(first_video)
                            if frames is None or not len(frames):
                                continue
                            seq = [frame_dhash(frame) for frame in frames]
                            matches = library.find_source_matches(seq, candidate['title'])
                            library.save_source_fingerprint(
                                candidate['title'], first_video, seq,
                                size_bytes=os.path.getsize(first_video))
                            for match in matches:
                                library.save_duplicate_pair(candidate['title'],
                                                            match['title'],
                                                            match['similarity'])
                                library.update_candidate_flags(match['title'],
                                                               duplicate_marked=True)
                                library.update_candidate_flags(candidate['title'],
                                                               duplicate_marked=True)
                            break
                        except Exception as exc:
                            self.logger.log(candidate['title'], 'WARN',
                                            f'原始素材换皮重复检查未完成：{exc}')
                            break
                    break
                if self.cancel_event.is_set():
                    break
                if chosen is None:
                    has_slot_record = any(item.get('run_id') == run_id and
                                          item.get('slot') == slot + 1
                                          for item in library.get_auto_run(run_id)['items'])
                    if has_slot_record:
                        continue
                    failure_summary = '无候选可分配' if failure_count == 0 else \
                        f'检查或下载失败 {failure_count} 部，均未成功'
                    no_match = library.add_run_item(
                        run_id, slot + 1, account,
                        {'title': '未选出候选', 'title_key': ''},
                        status='no_candidate', error=failure_summary)
                    errors.append(f'{account}：没有可用候选替补（检查/失败 {failure_count} 部）')

            if self.cancel_event.is_set():
                for candidate, _result, _account, entry in successful:
                    library.update_run_item(entry, status='downloaded_stopped')
                    # A completed download remains completed; stopping only
                    # prevents automatic slice enqueue/start.
                for item in library.get_auto_run(run_id)['items']:
                    if item.get('status') == 'downloading':
                        library.update_run_item(item['id'], status='download_stopped')
                library.update_auto_run(run_id, status='cancelled', error='用户请求停止',
                                        finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                return

            for candidate, result, account, item_id in successful:
                task_id, error = self._enqueue_cut(candidate, result, account, run_id, item_id)
                if error:
                    library.update_run_item(item_id, status='queue_failed', error=error)
                    errors.append(f"{candidate['title']}：{error}")
                else:
                    items_to_cut.append(item_id)

            if items_to_cut:
                this_run_ids = [item['cut_task_id'] for item in
                                library.get_auto_run(run_id)['items']
                                if item.get('cut_task_id') and item.get('status') == 'queued']
                library.update_auto_run(run_id, status='cutting')
                start = self.cut_manager.start_cutting(
                    task_ids=this_run_ids, pipeline_acquired=acquired)
                if start.get('status') != 'success':
                    errors.append('自动切片未启动：' + (start.get('error') or '未知原因'))
                    for item_id in items_to_cut:
                        library.update_run_item(item_id, status='queue_failed',
                                                error=start.get('error'))
                    status = 'partial_failure' if errors else 'failed'
                    library.update_auto_run(run_id, status=status,
                                            error='；'.join(errors),
                                            finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    return
                cut_started = True
                self._cut_waiting = True
                acquired = False
                self._pipeline_held = False
                # worker 异步；监督线程等待本轮任务终态，再恢复其他人工 pending 项。
                while (self.cut_manager.cutting or
                       (self.cut_manager.cut_thread and
                        self.cut_manager.cut_thread.is_alive())):
                    self.cancel_event.wait(1)
                if self.cut_manager.cut_thread and self.cut_manager.cut_thread.is_alive():
                    self.cut_manager.cut_thread.join(timeout=5)
                status_snapshot = self.cut_manager.get_status()
                run_items = library.get_auto_run(run_id)['items']
                for item in run_items:
                    task = next((t for t in status_snapshot.get('completed', []) +
                                 status_snapshot.get('failed', [])
                                 if t.get('id') == item.get('cut_task_id')), None)
                    if task and task.get('auto_run_id') == run_id:
                        end_state = 'cut_done' if task.get('status') == 'completed' else 'cut_failed'
                        library.update_run_item(item['id'], status=end_state,
                                                error=task.get('error'))
                run_record = library.get_auto_run(run_id) or {}
                has_cut_failure = any(item.get('status') == 'cut_failed'
                                      for item in run_record.get('items', []))
                if has_cut_failure:
                    errors.append('有切片任务失败，详见切片队列与剧档案')
            final_items = library.get_auto_run(run_id)['items']
            cut_pending = any(item.get('status') in ('queued', 'cutting')
                              for item in final_items)
            if self.cancel_event.is_set():
                status = 'cancelled'
            elif cut_pending:
                status = 'cutting'
            else:
                finished_items = library.get_auto_run(run_id)['items']
                had_success = any(item.get('status') in ('downloaded', 'queued', 'cut_done')
                                  for item in finished_items)
                status = ('completed' if not errors else
                          'partial_failure' if had_success else 'failed')
            library.update_auto_run(run_id, status=status,
                                    summary=(f'候选下载成功 {len(successful)} 部；本轮切片任务 {len(items_to_cut)} 部' +
                                             (f'；替补跳过 {len(warnings)} 部' if warnings else '')),
                                    error='；'.join(errors) or None,
                                    finished_at=(None if status == 'cutting' else
                                                 datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        except Exception as exc:
            self.logger.log('system', 'ERROR', f'自动选剧任务异常：{exc}')
            library.update_auto_run(run_id, status='failed', error=str(exc)[:1000],
                                    finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        finally:
            if not cut_started:
                queued_ids = [item.get('cut_task_id') for item in
                              library.get_auto_run(run_id).get('items', [])
                              if item.get('cut_task_id')]
                self._remove_auto_queue_items(queued_ids)
            self.active = False
            if acquired and not cut_started:
                execution_gate.release_pipeline()
                self._pipeline_held = False
            if self.cut_manager.cut_thread and self.cut_manager.cut_thread.is_alive():
                self.cut_manager.cut_thread.join(timeout=5)
            if cut_started:
                self._cut_waiting = False

    def _record_attempt(self, run_id, slot, account, candidate, status):
        item_id = library.add_run_item(run_id, slot, account, candidate, status=status)
        return candidate, item_id

    def _remove_auto_queue_items(self, task_ids):
        task_ids = set(filter(None, task_ids or []))
        if not task_ids:
            return
        with self.cut_manager.lock:
            self.cut_manager.queue = [
                task for task in self.cut_manager.queue
                if not (task.get('id') in task_ids and task.get('status') == 'pending')]
            try:
                conn = self.cut_manager.store._connect()
                conn.execute('DELETE FROM jobs WHERE job_type=? AND id IN (%s)' %
                             ','.join('?' for _ in task_ids),
                             ['cut', *task_ids])
                conn.commit()
                conn.close()
            except Exception:
                # Preserve the queue in storage if this JobStore version does not
                # expose a compatible SQLite accessor.
                pass

