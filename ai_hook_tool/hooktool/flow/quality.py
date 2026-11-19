"""分层质量门禁：来源 / 叙事 / 生成 / 交付。

技术通过不等于创意通过；关键 PENDING 一律阻断交付。每个许可独立计算。
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import probe
from ..source_identity import file_hash
from . import media, settings
from .errors import FlowError
from .validator import validate_plan

DETECTOR_VERSION = "qc-1"


def _check(check_id: str, status: str, reason: str, *, layer: str,
           severity: str = "blocking", refs: list | None = None,
           review_required: bool = False) -> dict:
    return {"check_id": check_id, "status": status, "layer": layer,
            "severity": severity, "reason": reason,
            "evidence_refs": refs or [], "detector_version": DETECTOR_VERSION,
            "review_required": review_required}


def _load_render_info(store, timeline: dict, render_info: dict | None) -> dict | None:
    if render_info:
        return render_info
    path = (store.context.run_dir(timeline["run_id"]) / "output"
            / f"{timeline['timeline_revision_id']}_render.json")
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("timeline_payload_hash") != timeline["payload_hash"]:
        return None
    return data


def run(store, timeline_revision_id: str, *, policy: dict | None = None,
        render_info: dict | None = None, prober=probe.probe_media) -> dict:
    pol = policy or settings.policy()
    timeline = store.timeline(timeline_revision_id)
    payload = timeline["payload"]
    plan = store.plan(payload["plan_revision_id"])
    checks: list[dict] = []

    # ── 来源层 ────────────────────────────────────────
    try:
        store.assert_current_source()
        checks.append(_check("SRC-SCOPE", "PASS", "首集与已确认素材一致", layer="source"))
    except FlowError as exc:
        checks.append(_check("SRC-SCOPE", "FAIL", exc.message, layer="source"))
    try:
        evidence = store.evidence_by_id(plan["payload"].get("evidence_revision_id"))
        snapshot = evidence["snapshot"]
        checks.append(_check("SRC-EVIDENCE", "PASS", "证据修订哈希有效", layer="source"))
    except FlowError as exc:
        evidence = store.latest_evidence()
        snapshot = evidence["snapshot"]
        checks.append(_check("SRC-EVIDENCE", "FAIL", exc.message, layer="source"))
    if evidence["evidence_revision_id"] != plan["payload"].get("evidence_revision_id"):
        checks.append(_check("SRC-EVIDENCE-CURRENT", "FAIL",
                             "方案引用的证据不是当前证据修订", layer="source"))
    review = store.valid_plan_review(plan)
    if review is None:
        checks.append(_check("SRC-PLAN-APPROVED", "FAIL",
                             "方案未批准或审批已失效", layer="source"))
    else:
        checks.append(_check("SRC-PLAN-APPROVED", "PASS",
                             f"审批 {review['review_id']} 绑定当前语义哈希",
                             layer="source"))
    latest_plan = store.latest_plan(plan["proposal_id"])
    if latest_plan["plan_revision_id"] != plan["plan_revision_id"]:
        checks.append(_check("SRC-PLAN-CURRENT", "FAIL",
                             "时间线使用了旧方案版本", layer="source"))

    assets = {}
    for item in payload["video_items"]:
        asset = store.generated_asset(item["generated_asset_id"])
        assets[asset["asset_id"]] = asset
        if file_hash(asset["path"]) != asset["sha256"]:
            checks.append(_check("SRC-ASSET-HASH", "FAIL",
                                 f"{item['shot_id']} 片段文件已变化", layer="source"))
        task = store.task(item["task_id"])
        if task["plan_revision_id"] != plan["plan_revision_id"]:
            checks.append(_check("SRC-TASK-BINDING", "FAIL",
                                 f"{item['shot_id']} 来自其他方案版本", layer="source"))

    # ── 叙事层 ────────────────────────────────────────
    model = (store.story_for_evidence(plan["payload"].get("evidence_revision_id"))
             or store.story())
    validation = validate_plan(plan["payload"], model, snapshot, policy=pol)
    if validation["ok"]:
        checks.append(_check("NAR-VALIDATION", "PASS", "引用/前提/时长校验通过",
                             layer="narrative"))
    else:
        for reason in validation["blocking_issues"]:
            checks.append(_check("NAR-VALIDATION", "FAIL", reason, layer="narrative"))
    if review is None or not review.get("facts_reviewed"):
        checks.append(_check("NAR-FACTS-REVIEWED", "PENDING",
                             "关键剧情事实尚未人工核对", layer="narrative",
                             review_required=True))
    else:
        checks.append(_check("NAR-FACTS-REVIEWED", "PASS",
                             "审批记录确认关键事实已核对", layer="narrative"))

    # ── 生成层 ────────────────────────────────────────
    for item in payload["video_items"]:
        asset = assets[item["generated_asset_id"]]
        asset_review = store.asset_review(asset["asset_id"])
        if asset_review is None or asset_review["decision"] != "approved":
            checks.append(_check("GEN-ASSET-REVIEW", "FAIL",
                                 f"{item['shot_id']} 片段未验收通过", layer="generation",
                                 refs=[asset["asset_id"]]))
            continue
        failing = [k for k in ("identity", "action", "dialogue")
                   if asset_review["checks"].get(k) == "fail"]
        pending = [k for k in ("identity", "action", "dialogue")
                   if asset_review["checks"].get(k) == "pending"]
        if failing:
            checks.append(_check("GEN-ASSET-CHECKS", "FAIL",
                                 f"{item['shot_id']} 关键检查失败：{failing}",
                                 layer="generation", refs=[asset["asset_id"]]))
        elif pending:
            checks.append(_check("GEN-ASSET-CHECKS", "PENDING",
                                 f"{item['shot_id']} 仍有待核对项：{pending}",
                                 layer="generation", review_required=True,
                                 refs=[asset["asset_id"]]))
        else:
            checks.append(_check("GEN-ASSET-CHECKS", "PASS",
                                 f"{item['shot_id']} 身份/动作/对白检查通过",
                                 layer="generation"))

    # ── 交付层 ────────────────────────────────────────
    render = _load_render_info(store, timeline, render_info)
    if render is None:
        checks.append(_check("DEL-RENDER", "FAIL", "没有与当前时间线匹配的渲染记录",
                             layer="delivery"))
        return _finish(store, timeline_revision_id, checks, plan, payload,
                       deliverable=False, render=None)
    output = Path(render["output"])
    if not output.is_file():
        checks.append(_check("DEL-RENDER", "FAIL", "渲染文件不存在", layer="delivery"))
        return _finish(store, timeline_revision_id, checks, plan, payload,
                       deliverable=False, render=render)
    info = prober(str(output))
    if info.get("error") or not info.get("has_video"):
        checks.append(_check("DEL-STREAM", "FAIL", f"成片流异常：{info.get('error')}",
                             layer="delivery"))
    else:
        checks.append(_check("DEL-STREAM", "PASS", "视频流可解析", layer="delivery"))
        expected = pol["qc"]["resolution"]
        actual = (info["width"], info["height"])
        if actual != (expected["width"], expected["height"]):
            checks.append(_check("DEL-RESOLUTION", "FAIL",
                                 f"分辨率 {actual} 与 {expected} 不符", layer="delivery"))
        else:
            checks.append(_check("DEL-RESOLUTION", "PASS", "分辨率符合竖屏规格",
                                 layer="delivery"))
        minimum, maximum = (int(x * 1000) for x in pol["delivery_duration_range_sec"])
        actual_ms = int(round(float(info["duration"]) * 1000))
        if actual_ms < minimum:
            checks.append(_check("DEL-DURATION", "FAIL",
                                 f"成片 {actual_ms / 1000:.1f}s 低于项目下限",
                                 layer="delivery"))
        elif actual_ms > maximum:
            checks.append(_check("DEL-DURATION", "WARN",
                                 f"成片 {actual_ms / 1000:.1f}s 超过项目上限",
                                 layer="delivery", severity="warning",
                                 review_required=True))
        else:
            checks.append(_check("DEL-DURATION", "PASS", "时长在交付区间内",
                                 layer="delivery"))
    if not render.get("audio_embedded"):
        checks.append(_check("DEL-AUDIO", "FAIL",
                             "成片没有音轨；缺对白必须显式失败，不得用静音掩盖",
                             layer="delivery"))
    elif payload.get("audio_permission") in (None, "unconfirmed"):
        checks.append(_check("DEL-AUDIO-PERMISSION", "PENDING",
                             "原生音频使用权限未确认", layer="delivery",
                             review_required=True))
    else:
        checks.append(_check("DEL-AUDIO", "PASS", "音轨已嵌入", layer="delivery"))
    black = media.detect_black(str(output))
    if not black["available"]:
        checks.append(_check("DEL-BLACK", "PENDING",
                             f"无法检测黑帧：{black['error']}", layer="delivery",
                             review_required=True))
    else:
        total_black = sum(seg["end"] - seg["start"] for seg in black["segments"])
        tolerance = float(pol["qc"]["black_frame_tolerance_sec"])
        if total_black > tolerance:
            checks.append(_check("DEL-BLACK", "FAIL",
                                 f"黑帧 {total_black:.2f}s 超过容差 {tolerance}s",
                                 layer="delivery"))
        else:
            checks.append(_check("DEL-BLACK", "PASS", "无明显黑帧", layer="delivery"))
    loud = media.loudness(str(output))
    if not loud["available"]:
        checks.append(_check("DEL-LOUDNESS", "PENDING",
                             f"响度检测不可用：{loud['error']}", layer="delivery",
                             review_required=True))
    elif not (pol["qc"]["min_lufs"] <= loud["integrated_lufs"] <= pol["qc"]["max_lufs"]):
        checks.append(_check("DEL-LOUDNESS", "WARN",
                             f"响度 {loud['integrated_lufs']} LUFS 超出建议区间",
                             layer="delivery", severity="warning", review_required=True))
    else:
        checks.append(_check("DEL-LOUDNESS", "PASS", "响度在建议区间", layer="delivery"))
    expected_subtitles = len(payload.get("subtitle_events") or [])
    rendered = int(render.get("subtitles_rendered") or 0)
    if expected_subtitles and rendered < expected_subtitles:
        checks.append(_check("DEL-SUBTITLE", "FAIL",
                             f"字幕 {rendered}/{expected_subtitles} 未烧录；"
                             "不得以缺字幕或截断长句交付", layer="delivery"))
    elif expected_subtitles:
        checks.append(_check("DEL-SUBTITLE", "PASS", "字幕与对白事件数量一致",
                             layer="delivery"))
    if pol["qc"]["require_ai_label"] and not render.get("label_burned"):
        checks.append(_check("DEL-AI-LABEL", "FAIL", "AI 标识缺失", layer="delivery"))
    elif pol["qc"]["require_ai_label"]:
        checks.append(_check("DEL-AI-LABEL", "PASS", "AI 标识已烧录", layer="delivery"))
    return _finish(store, timeline_revision_id, checks, plan, payload,
                   deliverable=True, render=render)


def _finish(store, timeline_revision_id: str, checks: list[dict], plan: dict,
            payload: dict, *, deliverable: bool, render: dict | None) -> dict:
    failures = [c for c in checks if c["status"] == "FAIL"]
    pendings = [c for c in checks if c["status"] == "PENDING"]
    warnings = [c for c in checks if c["status"] == "WARN"]
    layer_state = {}
    for layer in ("source", "narrative", "generation", "delivery"):
        rows = [c for c in checks if c["layer"] == layer]
        layer_state[layer] = "FAIL" if any(c["status"] == "FAIL" for c in rows) else (
            "PENDING" if any(c["status"] == "PENDING" for c in rows) else (
                "WARN" if any(c["status"] == "WARN" for c in rows) else "PASS"))

    preview_allowed = layer_state["source"] in ("PASS", "WARN")
    production_allowed = (layer_state["source"] in ("PASS", "WARN")
                          and store.valid_plan_review(plan) is not None)
    delivery_ready = deliverable and not failures and not pendings
    report = {
        "schema_version": 1, "timeline_revision_id": timeline_revision_id,
        "timeline_payload_hash": payload and store.timeline(timeline_revision_id)["payload_hash"],
        "plan_revision_id": plan["plan_revision_id"],
        "plan_semantic_hash": plan["semantic_hash"],
        "checks": checks,
        "layers": layer_state,
        "failure_count": len(failures), "pending_count": len(pendings),
        "warning_count": len(warnings),
        "blocking_issues": [c["reason"] for c in failures + pendings],
        "preview_allowed": preview_allowed,
        "production_allowed": production_allowed,
        "delivery_ready": delivery_ready,
        "delivery_allowed": False,
        "render": {k: render.get(k) for k in (
            "output", "duration", "width", "height", "audio_embedded",
            "label_burned", "subtitles_rendered")} if render else None,
        "note": "技术指标不代表创意质量；最终完整观看不能省略",
    }
    qc = store.add_qc(timeline_revision_id, report)
    report["qc_id"] = qc["qc_id"]
    return {**report, "qc_id": qc["qc_id"], "path": qc["path"]}


def permissions(store, qc: dict) -> dict:
    """最终许可动态计算：QC 结果 + 最终人工验收。"""
    final = store.final_review(qc["timeline_revision_id"], qc["qc_id"])
    delivery = (qc.get("delivery_ready", False) and final is not None
                and final["decision"] == "approved")
    return {"preview_allowed": qc.get("preview_allowed", False),
            "production_allowed": qc.get("production_allowed", False),
            "delivery_allowed": bool(delivery),
            "final_review": final}
