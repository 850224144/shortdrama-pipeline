# -*- coding: utf-8 -*-
"""三态合规闸：PASS / FAIL / PENDING，软告警叠加 WARN。

阻断级别与合规结论解耦：硬阻断退出不出片；软告警不中断、照常产出但记 WARN 不算通过；
缺证据记 PENDING，不判通过（定稿 §8）。
"""
import json
import os

from . import config as cfg, paths

PASS, FAIL, PENDING, WARN = "PASS", "FAIL", "PENDING", "WARN"


def _rule(rule_id: str, name: str, level: str, verdict: str, detail: str, block: bool) -> dict:
    return {"id": rule_id, "name": name, "level": level,
            "verdict": verdict, "detail": detail, "block": block}


def evaluate(material: dict, manifest: dict, collect_res: dict,
             assemble_res: dict, review: dict | None = None) -> dict:
    review = review or {}
    comp = cfg.compliance()
    settings = cfg.settings()
    min_sim = next((r["min_similarity"] for r in comp["hard_rules"]
                    if r["id"] == "1.2"), 0.5)
    min_dur = next((r["min_duration_sec"] for r in comp["hard_rules"]
                    if r["id"] == "1.4"), 30)

    rules: list[dict] = []
    warns: list[str] = []
    pending: list[str] = []
    hook_fail_reasons: list[str] = []

    # 1.1 人物一致（软告警）
    if not material.get("faces"):
        rules.append(_rule("1.1", "人物一致(全AI)", "soft_warn", PENDING,
                            "缺定妆/剧照参照，锁脸待补", False))
        pending.append("缺定妆参照")
    elif review.get("face_consistent") is False:
        rules.append(_rule("1.1", "人物一致(全AI)", "soft_warn", WARN,
                           "人工复核脸不一致（软告警，不阻断）", False))
        warns.append("脸不一致，发布前人工复核")
    elif review.get("face_consistent") is None:
        rules.append(_rule("1.1", "人物一致(全AI)", "soft_warn", PENDING,
                           "脸一致性待人工复核", False))
        pending.append("脸一致性待人工复核")
    else:
        rules.append(_rule("1.1", "人物一致(全AI)", "soft_warn", PASS, "锁脸参照齐备，人工复核一致", False))

    # 1.2 内容一致（硬阻断）
    sim = review.get("similarity")
    if sim is None:
        rules.append(_rule("1.2", "内容一致(全AI)", "hard_fail", PENDING,
                           "相同度待人工对照首集复核", False))
        pending.append("内容相同度待人工复核")
    elif sim < min_sim:
        rules.append(_rule("1.2", "内容一致(全AI)", "hard_fail", FAIL,
                            f"相同度 {sim:.2f} < {min_sim}，疑似整条新编", True))
    else:
        rules.append(_rule("1.2", "内容一致(全AI)", "hard_fail", PASS,
                           f"相同度 {sim:.2f} ≥ {min_sim}", False))

    # 1.3 动物钩子接正剧（硬阻断）
    if review.get("animal_hook"):
        if review.get("animal_has_main"):
            rules.append(_rule("1.3", "动物钩子接正剧", "hard_fail", PASS,
                               "动物钩子已接正剧内容", False))
        else:
            rules.append(_rule("1.3", "动物钩子接正剧", "hard_fail", FAIL,
                               "纯动物钩子未接正剧内容", True))
    else:
        rules.append(_rule("1.3", "动物钩子接正剧", "hard_fail", PASS,
                           "未使用动物钩子", False))

    # 1.4 时长达标（硬阻断）
    duration = assemble_res.get("duration") if assemble_res else None
    if duration is None:
        rules.append(_rule("1.4", "时长达标", "hard_fail", FAIL, "成片未生成或无法探测时长", True))
    elif duration < min_dur:
        rules.append(_rule("1.4", "时长达标", "hard_fail", FAIL,
                           f"成片 {duration:.1f}s < {min_dur}s", True))
    else:
        rules.append(_rule("1.4", "时长达标", "hard_fail", PASS,
                            f"成片 {duration:.1f}s ≥ {min_dur}s", False))

    # 附加：题材垂直（硬阻断）
    if review.get("genre_match") is False:
        rules.append(_rule("extra", "题材垂直", "hard_fail", FAIL, "成片与原剧跨题材", True))
    elif review.get("genre_match") is None:
        rules.append(_rule("extra", "题材垂直", "hard_fail", PENDING, "题材归属待人工确认", False))
        pending.append("题材归属待人工确认")
    else:
        rules.append(_rule("extra", "题材垂直", "hard_fail", PASS, "题材与原剧一致", False))

    # 标注角标（软告警）
    if assemble_res and assemble_res.get("badge_ok") is False:
        rules.append(_rule("badge", "AI 标注", "soft_warn", WARN,
                           "AI 标注角标未压上（软告警，不阻断）", False))
        warns.append("角标缺失，发布前人工补标注")
    elif assemble_res:
        rules.append(_rule("badge", "AI 标注", "soft_warn", PASS, "角标已压上", False))

    # Hook quality is a separate production gate. A duration-compliant concat
    # can still be blocked when its opening or rear cliffhanger is missing.
    hook_qc = manifest.get("hook_qc", {})
    hook_reasons = (list(manifest.get("hard_block", []))
                    + list(hook_qc.get("hard_block", []))
                    + list(hook_qc.get("collect_hard_block", []))
                    + list(collect_res.get("hard_block", [])))
    if manifest and "segments" in manifest:
        pre = next((s for s in manifest.get("segments", [])
                    if s.get("segment") == "前核15s"), {})
        rear = next((s for s in manifest.get("segments", [])
                     if s.get("segment") == "后钩子15s"), {})
        expected_pre = int(cfg.keywords().get("hook", {}).get("min_pre_shots", 3))
        expected_rear = int(cfg.keywords().get("hook", {}).get("min_hook_shots", 2))
        if len(pre.get("shots", [])) < expected_pre:
            hook_reasons.append(f"前核只有 {len(pre.get('shots', []))} 镜，至少需要 {expected_pre} 镜")
        if len(rear.get("shots", [])) < expected_rear:
            hook_reasons.append(f"后钩子只有 {len(rear.get('shots', []))} 镜，至少需要 {expected_rear} 镜")
        if hook_qc and not hook_qc.get("opening_within_limit", False):
            hook_reasons.append("首镜未能证明位于首 3 秒内，缺少开场钩子证据")
        if hook_qc and not hook_qc.get("subtitle_lines_present", False):
            hook_reasons.append("有分镜缺少字幕行")
        if assemble_res and assemble_res.get("output"):
            clips = collect_res.get("clips", [])
            rear_clips = [c for c in clips if c.get("segment") == "后钩子15s"]
            if not rear_clips:
                hook_reasons.append("成片未接入实际后钩子片段")
        hook_reasons = list(dict.fromkeys(hook_reasons))
        if hook_reasons:
            rules.append(_rule("hook", "短视频钩子质量", "hard_fail", FAIL,
                               "；".join(hook_reasons), True))
            hook_fail_reasons.extend(hook_reasons)
        elif hook_qc:
            rules.append(_rule("hook", "短视频钩子质量", "hard_fail", PASS,
                               "开场时间码、前核/后钩子槽位和字幕均齐备", False))
        else:
            rules.append(_rule("hook", "短视频钩子质量", "hard_fail", PENDING,
                               "缺钩子质量证据，需重新生成脚本分镜", True))
            hook_fail_reasons.append("缺钩子质量证据，需重新生成脚本分镜")

    # 音轨/字幕 PENDING 汇总
    for p in collect_res.get("pending", []):
        pending.append(p)

    # 汇总
    hard_fail = [r for r in rules if r["level"] == "hard_fail" and r["verdict"] == FAIL]
    hook_block = bool(hook_fail_reasons)
    has_pending = any(r["verdict"] == PENDING for r in rules) or bool(pending)
    has_warn = any(r["verdict"] == WARN for r in rules) or bool(warns)

    if hard_fail or hook_block:
        overall, block = FAIL, True
    elif has_pending:
        overall, block = PENDING, False
    elif has_warn:
        overall, block = WARN, False
    else:
        overall, block = PASS, False

    return {
        "overall": overall,
        "block": block,
        "rules": rules,
        "warns": warns,
        "pending": pending,
        "fail_reasons": [r["detail"] for r in hard_fail],
        "hook_block": hook_block,
        "hook_fail_reasons": hook_fail_reasons,
    }


def save_report(report: dict, path: str | None = None) -> str:
    path = path or os.path.join(paths.DIR_GATE, "gate_report.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path
