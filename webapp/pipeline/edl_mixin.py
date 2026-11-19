# -*- coding: utf-8 -*-
"""PipelineProcessor 的 edl 域实现（P1-5 拆分，方法体原样搬迁）。"""
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


class EdlMixin:
    PLANNED_SEGS_MIN = 230.0

    PLANNED_SEGS_MAX = 285.0

    MIN_SEG_DUR = 15.0

    HIGHLIGHT_WINDOW_MIN = 30.0

    CONFLICT_WORDS = ('打脸', '傻', '死', '滚', '敢', '凭什么', '不可能', '疯了',
                      '骗', '恨', '杀', '血', '泪', '离婚', '出轨', '复合', '孩子',
                      '怀孕', '亿', '婚约', '下不来台', '后悔')

    SUSPENSE_WORDS = ('发现', '秘密', '真相', '原来', '竟然', '居然', '突然',
                      '没想到', '终于', '七年', '前妻', '复婚')

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

    CAUSAL_CACHE_PREFIX = 'causal_check_v'

    REVIEW_SCORE_MIN = 0.65

    REVIEW_MAX_RETRY = 2

    _SCENE_CACHE = {}

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
        # P1-6：先规划并落盘全部 EDL，再统一渲染（串行或并行，见 render_workers）
        pending_render = []

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

            # 素材池见底时不要硬渲染注水版：片段数/规划时长低于本剧成片下限
            # 的版本必然被质量门禁拒收，提前跳过，省掉一次完整渲染（旧行为
            # 是渲染完 133s 成片才发现 <180s 拒收）。
            _rc = self._resolve_cut_config(config)
            _planned = sum(float(s.get('duration') or 0) for s in segments)
            if _planned + 1e-6 < float(_rc['min_dur']):
                self.logger.log(
                    drama_name, 'ERROR',
                    f'V{v_idx+1} 素材不足：仅{len(segments)}段/{_planned:.0f}s，'
                    f'低于成片下限{float(_rc["min_dur"]):.0f}s，'
                    f'跳过该版本（不渲染）')
                verdicts[v_idx] = {
                    'status': 'not_rendered',
                    'reason': (f'素材不足（{len(segments)}段/{_planned:.0f}s '
                               f'< {float(_rc["min_dur"]):.0f}s）')}
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
            # P2-9 审批闭环：EDL 落盘即写 edl_hash 与批准状态。auto_approve
            # 打开时视为生成即批准（保持现有自动化）；关闭则保持 pending，
            # 需调审批接口后才渲染。EDL 内容一变 hash 就变，旧批准自动失效。
            auto_approve = bool((CONFIG.get('edl_approval') or {}).get(
                'auto_approve', True))
            edl_config['approval'] = {
                'state': 'approved' if auto_approve else 'pending',
                'edl_hash': self._edl_hash_of(edl_config),
                'approved_at': (datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                if auto_approve else None),
                'approved_by': 'auto' if auto_approve else None,
            }
            try:
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(edl_config, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.logger.log('system', 'WARN', f'保存EDL配置失败：{str(e)}')
            
            self.logger.log(drama_name, 'INFO', f'生成EDL配置V{v_idx+1}：{len(segments)}个片段，'
                                                 f'规划合计{sum(s["duration"] for s in segments):.0f}s')
            
            # 只做规划与落盘；实际渲染放到循环后统一执行（P1-6）：
            # 串行模式保持原有 fail-fast，并行模式按版独立判定。
            pending_render.append((
                v_idx, config_path,
                sum(float(s.get('duration') or 0) for s in segments),
                len(segments)))

        # ── 渲染阶段 ────────────────────────────────────────────────
        workers = max(1, int((CONFIG.get('cut') or {}).get('render_workers', 1)
                             or 1))
        if workers > 1 and len(pending_render) > 1:
            self._render_versions_parallel(
                drama_name, out_dir, pending_render, verdicts, videos,
                expected_count, cancel_event, workers)
        else:
            self._render_versions_serial(
                drama_name, out_dir, pending_render, verdicts, videos,
                expected_count, cancel_event)

        # P8：渲染报告反映交付结论——render_generic 每版覆写报告（只剩末版），
        # 这里统一按每版 verdict 重写，拒收版标 REJECTED 而非 OK。
        self._write_render_report(out_dir, drama_name, verdicts,
                                  expected_count=expected_count)
        
        return videos

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
        try:
            jitter = random.uniform(float(jl), float(jh))
        except (TypeError, ValueError):
            jitter = 0.0
        # 数学部分统一走 pipeline.duration（与容量估算共用同一口径）
        budget, final_target = _duration_mod.version_budget(
            min_final, max_final, units, spread, ab, cfg, self.MIN_SEG_DUR,
            jitter_override=jitter)
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

    def _planned_segs_per_version(self, ep_count, config=None):
        """按真实时长规划口径估算每版片段数（与 _compute_version_budget 对齐）。

        修复：容量估算曾用 CONFIG['edl']['target_dur']/segment_dur（200/20=10段），
        但实际每版预算是 _compute_version_budget 算出的成片目标×1.05（30集剧
        会被 clamp 到 max_dur 330 → 346.5s → 17 段）。用 10 段算容量会把
        可支撑版本数高估近一倍，第 6 版起素材池见底，片段骤减到 7/2 段、
        时长 133s 拒收或渲染失败。
        """
        rc = self._resolve_cut_config(config)
        return _duration_mod.planned_segs(
            ep_count, float(rc['min_dur']), float(rc['max_dur']),
            CONFIG['edl'], CONFIG.get('duration', {}) or {}, self.MIN_SEG_DUR)

    def _capacity_version_count(self, edl_eps, drama_name, config=None):
        """素材容量决定版本数。

        默认版本数 = min(集数目标, 独立素材可支撑版本数)，最少保留 1 版。
        config.version_count 显式设定时：reuse_material=True 按设定（≤8，允许跨版本
        复用）；reuse_material=False 则 clamp 到零复用容量，绝不超容被迫复用。
        """
        rc = self._resolve_cut_config(config)
        cfg = CONFIG['edl']
        seg_dur = float(cfg.get('segment_dur', 20.0))
        segs_per_version = self._planned_segs_per_version(len(edl_eps), config=config)
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
        """按源文件 scene 边界 + 词首吸附切点（A1 + P0-3）。eps_hl 提供
        path/dur/words。检测失败或禁用 → 原值。返回吸附后的 start。
        """
        e = next((x for x in eps_hl if x['episode'] == ep_num), None)
        if e is None or not e.get('path'):
            return start
        total = e.get('dur', 0)
        snapped = start
        boundaries = self._scene_boundaries(e['path'])
        if boundaries:
            snapped = self._snap_scene_start(start, dur, total, boundaries)
        # 镜头边界只能保证“画面不半帧”，切点仍可能落在词中间；词级时间戳
        # 可用时再向前吸附到最近的词首（不吃掉半句台词）。
        return self._snap_word_start(snapped, dur, total, e.get('words') or [])

    def _snap_word_start(self, start, dur, total, words, tol=None):
        """把切点吸附到最近的词首（P0-3）。只向前吸附，tol 内无候选保持原值。"""
        if not words or start is None:
            return start
        cfg = CONFIG.get('word_snap') or {}
        if not cfg.get('enabled', True):
            return start
        if tol is None:
            tol = float(cfg.get('tol', 0.6))
        best = None
        for w in words:
            try:
                ws = float(w.get('start'))
            except (TypeError, ValueError):
                continue
            if ws > start + 1e-6:
                continue                      # 只考虑不晚于切点的词首
            if total and ws + dur > total - 0.05:
                continue                      # 吸附后超源时长
            if start - ws <= tol and (best is None or ws > best):
                best = ws
        return round(best, 2) if best is not None else start

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

    def _get_episode_num(self, filepath):
        """从文件名获取集数 - 兼容两种格式"""
        fname = os.path.basename(filepath)
        ep = self.parse_episode_number(fname)
        return ep if ep is not None else 1

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
                # 词级时间戳（word_timestamps 新转写才有）：供切点吸附词边界。
                # 旧缓存无 words 时为空列表，吸附自动退化为镜头边界/原值。
                words = []
                for seg in raw:
                    if not isinstance(seg, dict):
                        continue
                    for w in (seg.get('words') or []):
                        if isinstance(w, dict) and w.get('start') is not None:
                            words.append(w)
                words.sort(key=lambda w: float(w.get('start') or 0))
                out.append({"episode": ep_num, "path": ep_file,
                            "dur": ep_dur, "cands": cands, "words": words})
        return out
