# -*- coding: utf-8 -*-
"""PipelineProcessor 的 cut 域实现（P1-5 拆分，方法体原样搬迁）。"""
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


class CutMixin:
    def cut_batch(self, downloads, resume=False, force=False, cancel_event=None,
                  watermark=None):
        """批量切片"""
        results = []
        failed = []
        # 剧级档案：切片状态追踪（延迟 import 避免循环，archive 独立不依赖 processor）
        from archive import init_database as _arc_init
        try:
            _arc_init()
        except Exception:
            pass
        
        # 第一轮：处理所有
        for d in downloads:
            # 水印选择（提交消息/手动切片透传）；None=沿用各下载自身/自动交替。
            if watermark is not None:
                d['watermark_override'] = watermark
            drama_name = self._resolve_download_drama_name(d)
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
                self._write_archive_slice_state(drama_name, 'cutting',
                                                source_url=d.get('link'))
            _djob = self._begin_drama_job(d, drama_name, resume=resume)
            try:
                result = self._cut_once(d, resume=resume, force=force,
                                        cancel_event=cancel_event)
            except Exception as _cut_err:
                # 失败隔离：单部剧异常不再中断整批（旧实现上抛后后续剧集直接
                # 丢失，只能靠人工对账重提）。用户停止仍按取消语义上抛。
                if _cut_err.__class__.__name__ in ('CutCancelled', 'JobCancelled'):
                    self._end_drama_job(_djob, {'cancelled': True}, drama_name)
                    raise
                self.logger.log('system', 'ERROR',
                                f'切片异常（已隔离，继续下一部）：{drama_name} - {_cut_err}')
                result = {'name': drama_name, 'success': False,
                          'output_dir': None, 'error': f'切片异常：{_cut_err}'}
            result['retried'] = False
            # 队列回标需要链接标识：防重跳过的下载结果没有 drama_name，
            # 只按剧名标会全部漏标（pending_batch 永远 pending）。
            result.setdefault('link_id', d.get('link_id'))
            result.setdefault('link', d.get('link'))
            # 以 _cut_once 解析出的实际剧名回写（比下载结果更权威）
            drama_name = result.get('name') or drama_name
            if drama_name:
                if result.get('cancelled'):
                    # 停止不是失败；清掉“切片中”并在下次启动时重新处理。
                    self._write_archive_slice_state(
                        drama_name, 'none', error='用户已停止切片',
                        source_url=d.get('link'))
                elif result['success']:
                    self._write_archive_slice_state(drama_name, 'done',
                                                    result=result,
                                                    source_url=d.get('link'))
                elif '跳过' not in (result.get('error') or ''):
                    self._write_archive_slice_state(
                        drama_name, 'failed', error=result.get('error'),
                        source_url=d.get('link'))
            
            self._end_drama_job(_djob, result, drama_name)
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
            drama_name = self._resolve_download_drama_name(d)
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
            _djob = self._begin_drama_job(d, drama_name, resume=resume)
            try:
                result = self._cut_once(d, resume=resume, force=force,
                                        cancel_event=cancel_event)
            except Exception as _cut_err:
                if _cut_err.__class__.__name__ in ('CutCancelled', 'JobCancelled'):
                    self._end_drama_job(_djob, {'cancelled': True}, drama_name)
                    raise
                self.logger.log('system', 'ERROR',
                                f'重试切片异常（已隔离）：{drama_name} - {_cut_err}')
                result = {'name': drama_name, 'success': False,
                          'output_dir': None, 'error': f'切片异常：{_cut_err}'}
            result['retried'] = True
            result.setdefault('link_id', d.get('link_id'))
            result.setdefault('link', d.get('link'))
            drama_name = result.get('name') or drama_name
            if drama_name:
                if result.get('cancelled'):
                    self._write_archive_slice_state(
                        drama_name, 'none', error='用户已停止切片',
                        source_url=d.get('link'))
                elif result['success']:
                    self._write_archive_slice_state(drama_name, 'done',
                                                    result=result,
                                                    source_url=d.get('link'))
                elif '跳过' not in (result.get('error') or ''):
                    self._write_archive_slice_state(
                        drama_name, 'failed', error=result.get('error'),
                        source_url=d.get('link'))
            self._end_drama_job(_djob, result, drama_name)
            if result.get('cancelled'):
                results.append(result)
                break
            if result['success'] or result.get('skipped'):
                results.append(result)
            else:
                error_msg = result.get("error", "未知错误") or "未知错误"
                self.logger.log('system', 'ERROR', f'重试切片失败：{d.get("link_id", "未知")} - {error_msg}')
                results.append({
                    'name': drama_name or d.get('link_id'),
                    'success': False,
                    'output_dir': None,
                    'error': f'Retry failed: {error_msg}',
                    'retried': True
                })
        
        return results

    def _cut_once(self, download_result, resume=False, force=False,
                  cancel_event=None):
        """单次切片"""
        # 从下载目录提取实际剧名（drama_name → 素材目录名 → link_id）
        drama_name = self._resolve_download_drama_name(download_result)
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
        
        # 水印/账号选择解析（提交消息/手动切片透传的 watermark_override）：
        #   固定账号名 → 账号覆盖；其它文本 → 仅水印文字覆盖（账号仍自动交替）；
        #   无选择 → 原自动交替逻辑，一个字节不改。
        account = self.apply_watermark_override(download_result, cut_cfg, drama_name)
        # 输出目录按水印分类：output/福桃说剧/ 或 output/小桃追剧中/
        output_subdir = self.get_output_subdir(
            drama_name, account=account)
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
            # 成品齐全属于“这部剧已完成”，批次队列要同步收尾；否则每次重启
            # 的批次续跑都会把这部剧重新提交一遍（表现为永远 pending）。
            if '已有成品' in (reason or ''):
                try:
                    _pq_mark_by_drama(drama_name, status='done')
                    # 队列条目若没有 drama_name（下载续传/手动补队列写入的老条目），
                    # 按剧名回标会落空 → 每次重启都重复提交这部剧。补按 link_id 回标。
                    link_id = (download_result or {}).get('link_id')
                    if link_id:
                        _pq_mark(link_id=link_id, status='done', drama_name=drama_name)
                except Exception:
                    pass
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
                best_videos, drama_name, out_dir, src_dir,
                account=download_result.get('account') if
                download_result.get('auto_selected') else None)

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
                account = download_result.get('account') or os.path.basename(acc_dir)
                if not account or account == 'output':
                    account = self.assign_account(drama_name)
            except Exception:
                account = download_result.get('account') or self.assign_account(drama_name)
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

    def manual_cut(self, drama_name, resume=False, force=False, cancel_event=None,
                   config=None, account=None, watermark=None):
        """手动切片 - 从素材目录直接切片

        resume=True：失败续跑，只重渲染保留的 edl_config，不重新生成 EDL；
        无保留 config 时自动回退全量重切。

        config：本剧切制参数覆盖（version_count/min_dur/max_dur/reuse_material/
        watermark），None 走代码默认；调用方（UI）先合并 job_store.cut_config。

        watermark：可选水印选择，语义同 process_message（None=自动交替；
        固定账号名=账号覆盖；其它文本=仅水印文字覆盖）。
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
        if account:
            download_result['account'] = account
            download_result['auto_selected'] = True
        if watermark:
            download_result['watermark_override'] = watermark
        
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

    def manual_cut_for_account(self, drama_name, account, resume=False,
                               force=False, cancel_event=None, config=None):
        """新增的自动选剧切片入口；指定账号，不改变手动切片的默认分配。"""
        if not drama_name:
            return {'status': 'failed', 'error': '缺少剧名'}
        source_dir = os.path.join(CONFIG['download_base_dir'], drama_name)
        if not os.path.isdir(source_dir):
            return {'status': 'failed', 'error': f'素材目录不存在：{drama_name}'}
        files = self._scan_files_recursive(source_dir)
        mp4s = self._dedupe_episodes_by_number(
            [p for p in files if p.lower().endswith('.mp4') and
             '.downloading' not in p.lower()])
        if len(mp4s) < 2:
            return {'status': 'failed', 'error': f'素材文件不足：{len(mp4s)}集'}
        return self.manual_cut(drama_name, resume=resume, force=force,
                               cancel_event=cancel_event, config=config,
                               account=account)

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
                # 超时按集数缩放：65 集实测 8m41s，固定 600s 会让长剧卡在超时
                # （超时后转写为空 → 后续只能回落随机选段）。基准 180s + 每集 12s。
                asr_cfg = CONFIG.get('asr') or {}
                try:
                    ep_count = len(self.list_episodes(src_dir))
                except Exception:
                    ep_count = 0
                asr_timeout = max(
                    int(asr_cfg.get('min_timeout', 600)),
                    int(asr_cfg.get('base_timeout', 180))
                    + int(asr_cfg.get('per_episode_timeout', 12)) * int(ep_count or 0))
                result = self._run_command_cancellable(
                    cmd, timeout=asr_timeout, cancel_event=cancel_event, cwd=src_dir)
                self.logger.log(drama_name, 'INFO',
                                f'ASR超时预算 {asr_timeout}s（{ep_count}集）')
            
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

    @staticmethod
    def _child_process_env():
        """强制 Python 子进程使用 UTF-8，避免 Windows GBK 日志编码崩溃。"""
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        return env

    @staticmethod
    def _raise_if_cancelled(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise CutCancelled('用户已停止切片')

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

    def set_cut_progress_callback(self, callback):
        """注册切片阶段回调（BatchCutManager 用于实时 Web 进度）。"""
        self._cut_progress_callback = callback if callable(callback) else None

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

    def _begin_drama_job(self, download, drama_name, resume=False):
        """为单部剧写一条持久任务行（幂等键=link_id/manual:剧名）。

        落库失败只告警：剧任务用于可观测与续跑，不能反过来阻塞切片。
        """
        try:
            from drama_jobs import start as _start
            return _start(self._download_job_store(), drama_name,
                          link_id=(download or {}).get('link_id'),
                          link=(download or {}).get('link'),
                          account=(download or {}).get('account'),
                          resume=resume)
        except Exception as e:
            self.logger.log('system', 'WARN', f'剧任务落库失败（不影响切片）：{e}')
            return None

    def _end_drama_job(self, job_id, result, drama_name=''):
        """回写单剧任务终态；cancelled 不入库（保持 running，重启后重跑）。"""
        if not job_id:
            return
        try:
            from drama_jobs import (finish as _finish,
                                    STATUS_COMPLETED as _OK,
                                    STATUS_FAILED as _BAD)
            if result.get('cancelled'):
                return
            if result.get('success') or result.get('skipped'):
                _finish(self._download_job_store(), job_id, _OK, {
                    'drama_name': result.get('name') or drama_name,
                    'output_dir': result.get('output_dir'),
                    'versions': result.get('version_count'),
                    'skipped': bool(result.get('skipped')),
                    'summary': f'交付 {result.get("version_count") or "?"} 版',
                })
            else:
                _finish(self._download_job_store(), job_id, _BAD,
                        error=(result.get('error') or '切片失败')[:500])
        except Exception:
            pass

    def _write_archive_slice_state(self, drama_name, state, result=None, error=None,
                                    source_url=None):
        """回写剧档案切片状态（重试一次，失败不再静默吞掉）。

        此前 `except Exception: pass` 会吞掉 database is locked 等写入失败，
        交付完成后档案仍显示 none，列表页/防重/报表全部误判。
        P0-4：带来源链接时先按 source_key 找规范行，杜绝用分享码派生名写出
        第二条档案行（真实剧名行的 slice_state 永远停在 none 的那种事故）。
        """
        from archive import resolve_archive_name, upsert_archive
        name = resolve_archive_name(source_url, drama_name) or drama_name
        if not name or self._looks_like_link_id(name):
            return
        fields = {'slice_state': state}
        if source_url:
            fields['source_url'] = source_url
        if state == 'done':
            fields['slice_version_count'] = self._count_delivered_versions(result or {})
            fields['sliced_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            fields['slice_error'] = None
        else:
            fields['slice_error'] = (error or '')[:500]
        last_err = None
        for _ in range(2):
            try:
                upsert_archive(name, **fields)
                return
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        self.logger.log('system', 'WARN',
                        f'剧档案切片状态回写失败（{name}→{state}）：{last_err}')
