# -*- coding: utf-8 -*-
"""时长规划纯函数（P1-5 拆分第一批）。

原 processor.py 里“预算怎么算、每版大约几段”的逻辑只依赖配置与数学，
与下载/渲染无耦合；抽到这里后 processor 只保留“接线”方法，
单测可以直接对着本模块验算，不必构造整个 Processor。
"""
import math


def version_budget(min_dur, max_dur, units, spread, angle_bonus,
                   duration_cfg, min_seg_dur, jitter_override=None,
                   jitter_range=None):
    """计算单版 EDL 规划预算（秒），含 atempo 1.05 与停手间隙余量。

    units: 叙事单元数（集数）；spread: 时间线跨度；
    angle_bonus: 角度加成秒；duration_cfg: CONFIG['duration']。
    """
    per_unit = float(duration_cfg.get('per_unit', 32))
    per_spread = float(duration_cfg.get('per_spread', 8))
    if jitter_override is not None:
        jitter = float(jitter_override)
    else:
        try:
            jl, jh = (jitter_range or duration_cfg.get('jitter', (20, 55)))
            jitter = float(jh)
        except (TypeError, ValueError):
            jitter = 0.0
    base = per_unit * max(1, units) + per_spread * max(1, spread) + float(angle_bonus or 0)
    final_target = min(max(base + jitter, float(min_dur)), float(max_dur))
    # 成片目标 ×1.05 抵消渲染压缩；下限再留一个 MIN_SEG_DUR 防末段过短被丢弃
    min_budget = float(min_dur) * 1.05 + float(min_seg_dur)
    budget = min(max(final_target * 1.05, min_budget), float(max_dur) * 1.05)
    return round(budget, 1), final_target


def planned_segs(ep_count, min_dur, max_dur, edl_cfg, duration_cfg,
                 min_seg_dur):
    """按真实预算口径估算每版片段数（与 version_budget 对齐）。

    旧实现在容量估算时用 target_dur/segment_dur（200/20=10 段），
    而实际每版预算是成片目标×1.05（30 集被 clamp 到 330 → 17 段），
    导致可支撑版本数被高估近一倍。
    """
    seg_dur = float(edl_cfg.get('segment_dur', 20.0))
    duration_cfg = duration_cfg or {}
    if not duration_cfg.get('enabled', True):
        fallback = float(edl_cfg.get('target_dur', 200.0))
        return max(1, int(math.ceil(fallback / seg_dur)))
    try:
        angle_vs = [float(v) for v in (duration_cfg.get('angle_bonus') or {}).values()]
    except (TypeError, ValueError):
        angle_vs = []
    ab = max(angle_vs or [0.0])
    units = max(1, int(ep_count))
    budget, _ = version_budget(min_dur, max_dur, units, units, ab,
                               duration_cfg, min_seg_dur, jitter_range=None)
    # 段循环在“剩余预算 ≤ MIN_SEG_DUR”时停手
    usable = max(seg_dur, budget - float(min_seg_dur))
    return max(1, int(math.ceil(usable / seg_dur)))
