# -*- coding: utf-8 -*-
"""PipelineProcessor 的 render 域实现（P1-5 拆分，方法体原样搬迁）。"""
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


class RenderMixin:
    def _render_versions_serial(self, drama_name, out_dir, pending, verdicts,
                                videos, expected_count, cancel_event):
        """逐版串行渲染（默认）：保留原有 fail-fast（连续同类失败即停）。"""
        consecutive_fail = 0
        last_fail_cat = None
        for v_idx, config_path, planned, nseg in pending:
            self._raise_if_cancelled(cancel_event)
            render_pct = 48 + int(40 * v_idx / max(1, expected_count))
            self._emit_cut_progress(
                'render', render_pct,
                f'渲染 V{v_idx + 1}/{expected_count}',
                version=v_idx + 1, segments=nseg, duration=round(planned, 1))
            # 渲染超时随本版规划时长缩放：长版（用户调大 max_dur）不再固定 600s 超时。
            render_timeout = max(600, int(planned * 2.5))
            video, fail_cat, verdict = self._run_render_one(
                drama_name, out_dir, v_idx, config_path,
                cancel_event=cancel_event, timeout=render_timeout)
            self._emit_cut_progress(
                'render', 48 + int(40 * (v_idx + 1) / max(1, expected_count)),
                f'V{v_idx + 1}/{expected_count} '
                f'{"渲染完成" if video else "渲染未通过"}',
                version=v_idx + 1, accepted=bool(video))
            verdicts[v_idx] = verdict
            if video:
                videos.append(video)
                consecutive_fail = 0
                last_fail_cat = None
                self._mark_resume_version(
                    out_dir, v_idx, status='post_done',
                    artifact=video.get('filename'))
            else:
                # fail-fast ②：连续同类别失败 = 系统性错误（源文件缺失/编码器故障
                # 等），剩余版本必然同样失败，立即停止。不同类/单次失败视为偶发。
                if fail_cat and fail_cat == last_fail_cat:
                    consecutive_fail += 1
                else:
                    consecutive_fail = 1
                    last_fail_cat = fail_cat
                if (fail_cat and fail_cat not in ('other', 'quality')
                        and consecutive_fail >= 2):
                    self.logger.log(drama_name, 'ERROR',
                        f'连续{consecutive_fail}个版本同类失败（{fail_cat}），判定系统性错误，'
                        f'停止剩余版本（已成功{len(videos)}个）')
                    break

    def _render_versions_parallel(self, drama_name, out_dir, pending, verdicts,
                                  videos, expected_count, cancel_event,
                                  workers):
        """并行渲染（render_workers>1）：两条 NVENC/CPU 管线同时出片。

        并行时已在跑的版本无法撤回，因此不做“连续失败即停”；失败按版独立记录，
        后续 _select_best_videos / 报告照常按 verdict 处理。
        """
        self.logger.log(drama_name, 'INFO',
                        f'并行渲染：{len(pending)} 版 / {workers} 路')
        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for v_idx, config_path, planned, nseg in pending:
                if cancel_event is not None and cancel_event.is_set():
                    verdicts[v_idx] = {'status': 'not_rendered',
                                       'reason': '用户已停止'}
                    continue
                render_timeout = max(600, int(planned * 2.5))
                fut = pool.submit(self._run_render_one, drama_name, out_dir,
                                  v_idx, config_path,
                                  cancel_event=cancel_event,
                                  timeout=render_timeout)
                futures[fut] = (v_idx, nseg, planned)
            done = 0
            for fut in as_completed(futures):
                v_idx, nseg, planned = futures[fut]
                try:
                    video, fail_cat, verdict = fut.result()
                except Exception as e:  # 单版异常不拖垮整批
                    video = None
                    verdict = {'status': 'render_failed', 'reason': f'渲染异常：{e}'}
                done += 1
                self._emit_cut_progress(
                    'render', 48 + int(40 * done / max(1, len(futures))),
                    f'V{v_idx + 1}/{expected_count} '
                    f'{"渲染完成" if video else "渲染未通过"}',
                    version=v_idx + 1, accepted=bool(video))
                verdicts[v_idx] = verdict
                if video:
                    videos.append(video)
                    self._mark_resume_version(
                        out_dir, v_idx, status='post_done',
                        artifact=video.get('filename'))

    def _run_render_one(self, drama_name, out_dir, v_idx, config_path,
                        cancel_event=None, timeout=600):
        """对单个 edl_config_v*.json 调 render_generic.py 渲染。

        公共方法：_generate_edl_versions（全量生成）与 _re_render_from_configs
        （失败续跑）共用，保证渲染/日志行为一致。

        timeout：本版渲染上限（秒）。默认 600；调用方按本版规划时长缩放
        （max(600, 规划秒数×2.5)），长版不再 600s 固定超时。

        Returns: video dict（path/final_path/filename/type）或 None（失败）。
        """
        # P2-9：渲染前校验 EDL 批准状态，EDL 被改后旧批准自动失效。
        approved, reason = self._check_edl_approval(config_path, drama_name)
        if not approved:
            self.logger.log(drama_name, 'WARN', f'V{v_idx+1} 渲染被审批门禁拦截：{reason}')
            return None, 'approval', {'status': 'unapproved', 'reason': reason}
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
                # 错误信息兜底：render_generic 有时把异常打到 stdout（stderr 为空），
                # 旧日志只取 stderr[:200] → 空消息，失败原因完全丢失。
                detail = (result.stderr or '').strip() or (result.stdout or '').strip()
                if not detail:
                    detail = f'无输出（returncode={result.returncode}）'
                self.logger.log(
                    drama_name, 'ERROR',
                    f'V{v_idx+1} EDL渲染失败（{fail_cat}，rc={result.returncode}）：'
                    f'{detail[-300:]}')
                return None, fail_cat, {'status': 'render_failed', 'reason': fail_cat or '未知'}

        except CutCancelled:
            self.logger.log(drama_name, 'INFO', f'V{v_idx+1} 渲染已停止')
            raise
        except Exception as e:
            fail_cat = self._classify_render_failure('', e)
            self.logger.log(drama_name, 'ERROR',
                            f'V{v_idx+1} EDL异常（{fail_cat}）：{str(e)}')
            return None, fail_cat, {'status': 'render_failed', 'reason': fail_cat or '未知'}

    @staticmethod
    def _edl_hash_of(edl_config):
        """EDL 内容指纹（只看 segments，文案改动不影响批准）。"""
        return _edl_mod.edl_hash_of(edl_config)

    def _check_edl_approval(self, config_path, drama_name=''):
        """渲染前校验 EDL 批准状态（P2-9）。返回 (ok, reason)。

        批准 hash 与当前 segments 一致 → 放行；不一致说明 EDL 被改、旧批准
        失效；auto_approve 打开时自动重批后放行，关闭时拒绝渲染。
        """
        auto = bool((CONFIG.get('edl_approval') or {}).get('auto_approve', True))
        # EDL 文件不存在（续跑/重建路径或单测直调渲染）时不做审批拦截：
        # 没有 EDL 就没有可批准的片段清单，拦住只会掩盖真正的渲染错误。
        if not os.path.exists(config_path):
            return True, ''
        ok, reason, reapproved = _edl_mod.check_approval(config_path,
                                                         auto_approve=auto)
        if reapproved:
            self.logger.log('system', 'INFO',
                            f'{drama_name} EDL 内容变更，已自动重新批准（auto_approve）')
        return ok, reason

    def approve_edl(self, out_dir, version, approved=True, by='manual'):
        """审批某一版 EDL（供 /api/cut/edl/approve 接口调用）。返回 (ok, payload)。"""
        try:
            version = int(version)
        except (TypeError, ValueError):
            return False, {'error': '版本号非法'}
        config_path = os.path.join(out_dir, f'edl_config_v{version}.json')
        if not os.path.exists(config_path):
            return False, {'error': f'EDL 不存在：{config_path}'}
        try:
            approval = _edl_mod.set_approval(config_path, approved=approved, by=by)
        except Exception as e:
            return False, {'error': f'EDL 审批写入失败：{e}'}
        return True, {'version': version, 'approval': approval}

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
        verdicts = {}
        # P1-6：续跑也走“先筛选、再渲染”，渲染阶段按 render_workers 并行
        pending = []
        for config_path in config_paths:
            self._raise_if_cancelled(cancel_event)
            m = re.search(r'edl_config_v(\d+)\.json$', config_path)
            v_idx = (int(m.group(1)) - 1) if m else len(pending)
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
            pending.append((v_idx, config_path, _planned))

        workers = max(1, int((CONFIG.get('cut') or {}).get('render_workers', 1)
                             or 1))
        if workers > 1 and len(pending) > 1:
            self._resume_render_parallel(
                drama_name, out_dir, pending, verdicts, videos,
                len(config_paths), cancel_event, workers)
        else:
            self._resume_render_serial(
                drama_name, out_dir, pending, verdicts, videos,
                len(config_paths), cancel_event)
        self._write_render_report(out_dir, drama_name, verdicts,
                                  expected_count=len(config_paths))
        return videos

    def _apply_resume_result(self, drama_name, out_dir, v_idx, config_path,
                             video, verdict, verdicts, videos):
        """续跑单版结果落账：标记清单、删 config（仅成功）、记录 verdict。"""
        verdicts[v_idx] = verdict or (
            {'status': 'accepted', 'reason': ''} if video else
            {'status': 'render_failed', 'reason': '未知'})
        if video:
            videos.append(video)
            self._mark_resume_version(
                out_dir, v_idx, status='post_done',
                artifact=video.get('filename'))
            # P2-4：渲染成功即删除该版本 config——失败路径保留 config 以便续跑
            try:
                os.remove(config_path)
            except OSError as e:
                self.logger.log(drama_name, 'WARN', f'清理已渲染配置失败（可忽略）: {e}')

    def _resume_render_serial(self, drama_name, out_dir, pending, verdicts,
                              videos, expected_count, cancel_event):
        """续跑串行渲染：保留连续同类失败即停的 fail-fast。"""
        consecutive_fail = 0
        last_fail_cat = None
        for v_idx, config_path, planned in pending:
            self._raise_if_cancelled(cancel_event)
            self._emit_cut_progress(
                'render', min(88, 45 + int(40 * v_idx / max(1, expected_count))),
                f'续跑渲染 V{v_idx + 1}', version=v_idx + 1)
            video, fail_cat, verdict = self._run_render_one(
                drama_name, out_dir, v_idx, config_path,
                cancel_event=cancel_event, timeout=max(600, int(planned * 2.5)))
            self._emit_cut_progress(
                'render', min(89, 45 + int(40 * (v_idx + 1) / max(1, expected_count))),
                f'续跑 V{v_idx + 1} {"渲染完成" if video else "渲染未通过"}',
                version=v_idx + 1, accepted=bool(video))
            self._apply_resume_result(drama_name, out_dir, v_idx, config_path,
                                      video, verdict, verdicts, videos)
            if not video:
                if fail_cat and fail_cat == last_fail_cat:
                    consecutive_fail += 1
                else:
                    consecutive_fail = 1
                    last_fail_cat = fail_cat
                if (fail_cat and fail_cat not in ('other', 'quality')
                        and consecutive_fail >= 2):
                    self.logger.log(drama_name, 'ERROR',
                        f'续跑连续{consecutive_fail}个版本同类失败（{fail_cat}），判定系统性错误，'
                        f'停止剩余版本（已成功{len(videos)}个）')
                    break

    def _resume_render_parallel(self, drama_name, out_dir, pending, verdicts,
                                videos, expected_count, cancel_event, workers):
        """续跑并行渲染（render_workers>1），失败按版独立判定。"""
        self.logger.log(drama_name, 'INFO',
                        f'续跑并行渲染：{len(pending)} 版 / {workers} 路')
        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for v_idx, config_path, planned in pending:
                if cancel_event is not None and cancel_event.is_set():
                    verdicts[v_idx] = {'status': 'not_rendered', 'reason': '用户已停止'}
                    continue
                fut = pool.submit(self._run_render_one, drama_name, out_dir,
                                  v_idx, config_path,
                                  cancel_event=cancel_event,
                                  timeout=max(600, int(planned * 2.5)))
                futures[fut] = (v_idx, config_path)
            done = 0
            for fut in as_completed(futures):
                v_idx, config_path = futures[fut]
                try:
                    video, fail_cat, verdict = fut.result()
                except Exception as e:
                    video, verdict = None, {'status': 'render_failed',
                                            'reason': f'渲染异常：{e}'}
                done += 1
                self._emit_cut_progress(
                    'render', min(89, 45 + int(40 * done / max(1, len(futures)))),
                    f'续跑 V{v_idx + 1} {"渲染完成" if video else "渲染未通过"}',
                    version=v_idx + 1, accepted=bool(video))
                self._apply_resume_result(drama_name, out_dir, v_idx, config_path,
                                          video, verdict, verdicts, videos)
