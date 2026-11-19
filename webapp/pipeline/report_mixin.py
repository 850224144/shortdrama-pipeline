# -*- coding: utf-8 -*-
"""PipelineProcessor 的 report 域实现（P1-5 拆分，方法体原样搬迁）。"""
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


class ReportMixin:
    _VERSION_RE = re.compile(r'_(\d+)_[^_]+_去重版\.mp4(?:\.post\.tmp\.mp4)?$')

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
                # 盘符冒号必须转义成 C\:，否则 drawtext 字体加载失败、水印渲染成方框
                f"drawtext=fontfile='C\\:/Windows/Fonts/msyh.ttc':"
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
            full_stderr = result.stderr.decode("utf-8", errors="ignore")
            font_failed = "Fontconfig error" in full_stderr
            if result.returncode != 0 or font_failed:
                # 字体加载失败时 ffmpeg 仍返回 0，但水印是方框，同样按失败处理
                stderr_text = full_stderr[:300]
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
        # 词中切断门禁（P0-3）：审计项升级为“禁止自动上传”的软门禁——
        # 成片照常保留（人工可审），但超过阈值的版本不参与自动上传，
        # 避免半句切的片子直接发出去。阈值可配，<=0 表示关闭门禁。
        qc_cfg = CONFIG.get('quality') or {}
        gate_max = int(qc_cfg.get('max_mid_word_cuts', 2) or 0)
        gate = {'max_mid_word_cuts': gate_max, 'versions': {}}
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
                # 门禁判定：只有“已检测到词中切断且超过阈值”才拦自动上传；
                # 未检测（unknown）不拦，但报告明确标注未验证。
                blocked = False
                if gate_max > 0 and mid_word_status != 'unknown' and len(mc) > gate_max:
                    blocked = True
                    lines.append(
                        f"- 🚫 QC 门禁：词中切断 {len(mc)} 处 > 阈值 {gate_max}，"
                        f"该版本禁止自动上传（重切后可解除）\n")
                gate['versions'][str(vid)] = {
                    'file': v.get('filename', ''),
                    'mid_word_cuts': len(mc),
                    'mid_word_status': mid_word_status,
                    'block_upload': blocked,
                }
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
        # 门禁结果落盘：上传侧按版本号读它决定是否自动上传（解耦，不 import processor）。
        try:
            gate_path = os.path.join(out_dir, f'{drama_name}_QC门禁.json')
            tmp = gate_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(gate, f, ensure_ascii=False, indent=1)
            os.replace(tmp, gate_path)
            blocked = [k for k, x in gate['versions'].items() if x.get('block_upload')]
            if blocked:
                self.logger.log('system', 'WARN',
                                f'{drama_name}: QC 门禁拦截自动上传 V{"、V".join(blocked)}')
        except Exception as e:
            self.logger.log('system', 'WARN', f'QC 门禁结果写入失败：{e}')
        return gate

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
                "-vf", f"drawtext=fontfile='C\\:/Windows/Fonts/msyhbd.ttc':"
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

    def _episode_real_duration(self, ep_file, raw):
        """单集真实时长：probe 权威（与渲染端 render_generic 一致），
        防止 EDL 规划超源文件时长；probe 失败回退转写 last end（保底不丢集）。"""
        dur = self._get_video_duration(ep_file)
        if dur > 0:
            return dur
        return max((float(s.get("end", 0)) for s in raw), default=0.0)

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

    def _dedup_versions_by_content(self, best_videos, drama_name, out_dir,
                                   src_dir=None, account=None):
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
        account = account or self.assign_account(drama_name)
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

    def _parse_version_from_filename(self, fname):
        """从交付文件名解析版本号：{drama}_{n}_{主题}_去重版.mp4 或 V{n}。"""
        if not fname:
            return None
        m = re.search(r'_(\d+)_[^_]*_去重版\.mp4$', fname)
        if m:
            return int(m.group(1))
        m = re.search(r'V(\d+)', fname)
        return int(m.group(1)) if m else None

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
