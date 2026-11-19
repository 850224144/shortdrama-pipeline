# -*- coding: utf-8 -*-
"""候选计划的人工审核关口（定稿 P2：审核批准后才能进入制作）。

manifest["review"] 结构::

    {
      "revision": 1,                 # script 生成版本；重跑 script 即 +1，旧批准随之失效
      "status": "pending|approved|rejected",
      "candidates": [ {...}, ... ],  # 候选清单：台词、时间码、选择理由、分镜↔槽位映射
      "candidates_hash": "…",        # 清单指纹，用于判断审批是否仍对应当前分镜
      "decided_at": None, "decided_by": None, "note": "",
      "history": [ {at, action, revision, status, candidates} ]
    }

为什么要有指纹：manifest 是本机可手改的 JSON。若只在 approve 时写一个布尔，
之后改分镜/换候选，批准会「追认」另一份清单。这里把候选清单的
(candidate_id, segment, shot, subtitle) 序列哈希一起落盘，collect/assemble 前
重算比对，不一致即视为未审核。
"""
import hashlib
import json
import os
from datetime import datetime

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
_ACTIONS = ("approve", "reject", "reset")


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slot_for(segment: str, shot_no) -> str:
    """分镜号 → 生成片段槽位（collect 按「段目录/编号.mp4」取片）。"""
    try:
        no = int(shot_no)
    except (TypeError, ValueError):
        return f"{segment}/?.mp4"
    return f"{segment}/{no}.mp4"


def _selection_reason(shot: dict, hint: dict) -> str:
    """把已有证据字段拼成可读的选择理由（不新增任何推断）。"""
    parts = []
    score = shot.get("conflict_score")
    percentile = shot.get("conflict_percentile")
    if score is not None:
        pct = f"，分位 {percentile}%" if percentile is not None else ""
        parts.append(f"规则冲突分 {score}{pct}")
    emotion = shot.get("emotion")
    if emotion:
        parts.append(f"情绪 {emotion}")
    if shot.get("opening_hook"):
        parts.append("首镜=开场 3 秒钩子")
    if shot.get("is_cliffhanger"):
        parts.append("标记为后钩子悬念句")
    if hint.get("role_in_hook"):
        parts.append(f"LLM 建议位置 {hint['role_in_hook']}")
    if hint.get("why_it_hooks"):
        parts.append(f"LLM 理由：{hint['why_it_hooks']}")
    return "；".join(parts)


def collect_candidates(manifest: dict) -> list[dict]:
    """从 manifest 分镜抽取候选清单（含选择理由与槽位映射）。"""
    rows = []
    for seg in manifest.get("segments", []) or []:
        segment = seg.get("segment", "")
        for shot in seg.get("shots", []) or []:
            hint = shot.get("planning_hint") or {}
            rows.append({
                "candidate_id": shot.get("candidate_id") or "",
                "segment": segment,
                "shot": shot.get("shot"),
                "slot": slot_for(segment, shot.get("shot")),
                "subtitle": shot.get("subtitle", ""),
                "source_time": shot.get("source_time") or {},
                "conflict_score": shot.get("conflict_score"),
                "emotion": shot.get("emotion"),
                "role_in_hook": hint.get("role_in_hook"),
                "why_it_hooks": hint.get("why_it_hooks"),
                "selection_reason": _selection_reason(shot, hint),
                "opening_hook": bool(shot.get("opening_hook")),
                "is_cliffhanger": bool(shot.get("is_cliffhanger")),
            })
    return rows


def candidates_hash(rows: list[dict]) -> str:
    """候选清单指纹：任一候选的 ID/段/分镜号/字幕变化都会变。"""
    payload = [[str(r.get("candidate_id", "")), str(r.get("segment", "")),
                str(r.get("shot", "")), str(r.get("subtitle", ""))] for r in rows]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def prepare(manifest: dict) -> dict:
    """script 生成分镜后调用：写入候选清单并重置为「待审核」。

    重跑 script 会 +1 版本并把状态打回 pending——旧批准对应的是旧清单，
    不能顺延到新清单。
    """
    rows = collect_candidates(manifest)
    prev = manifest.get("review") or {}
    revision = int(prev.get("revision") or 0) + 1
    review = {
        "revision": revision,
        "status": STATUS_PENDING,
        "candidates": rows,
        "candidates_hash": candidates_hash(rows),
        "decided_at": None,
        "decided_by": None,
        "note": "",
        "history": list(prev.get("history") or []),
    }
    review["history"].append({
        "at": _ts(), "action": "prepare", "revision": revision,
        "status": STATUS_PENDING, "candidates": len(rows),
    })
    manifest["review"] = review
    return review


def decide(manifest: dict, action: str, note: str = "",
           reviewer: str | None = None) -> tuple[dict, str]:
    """批准 / 退回 / 重置。返回 (review, error)；error 非空表示未执行。"""
    if action not in _ACTIONS:
        return manifest.get("review") or {}, f"未知审核动作：{action}（可选 {', '.join(_ACTIONS)}）"
    review = manifest.get("review")
    if not review:
        return {}, "无审核记录：请先跑 script 生成候选清单"
    rows = collect_candidates(manifest)
    new_status = {
        "approve": STATUS_APPROVED,
        "reject": STATUS_REJECTED,
        "reset": STATUS_PENDING,
    }[action]
    if action == "approve" and not rows:
        return review, "候选清单为空：没有可批准的分镜，先重新规划"
    review.update({
        "status": new_status,
        "candidates": rows,
        "candidates_hash": candidates_hash(rows),
        "decided_at": _ts(),
        "decided_by": reviewer or "local",
        "note": note or "",
    })
    review["history"].append({
        "at": review["decided_at"], "action": action,
        "revision": review.get("revision"), "status": new_status,
        "candidates": len(rows), "note": note or "",
    })
    return review, ""


def approval_block_reason(manifest: dict) -> str:
    """未满足审核要求时返回阻断原因，通过返回空串（collect/assemble 前调用）。"""
    review = manifest.get("review") or {}
    if not review:
        return "无审核记录：请先跑 script 生成候选清单，再 review --action approve"
    status = review.get("status")
    if status == STATUS_REJECTED:
        note = review.get("note") or "未填写审核意见"
        return f"候选计划已被退回（{note}），重新规划并批准前不得进入制作"
    if status != STATUS_APPROVED:
        return "候选计划待人工审核：review --action approve 通过后才可收集/拼接"
    if review.get("candidates_hash") != candidates_hash(collect_candidates(manifest)):
        return "审核记录与当前分镜不一致（分镜或候选被改动），需重新审核"
    return ""


def is_approved(manifest: dict) -> bool:
    return not approval_block_reason(manifest)


def render_markdown(manifest: dict) -> str:
    """审核单（人工可读），用于导出留痕。"""
    review = manifest.get("review") or {}
    rows = review.get("candidates") or collect_candidates(manifest)
    lines = [
        f"# 钩子候选审核单 — {manifest.get('drama_name', '未命名')}",
        "",
        f"- 审核状态：{review.get('status', STATUS_PENDING)}",
        f"- 版本：{review.get('revision')}",
        f"- 决定时间：{review.get('decided_at') or '未决定'}",
        f"- 审核人：{review.get('decided_by') or '未指定'}",
        f"- 审核意见：{review.get('note') or '无'}",
        f"- 来源集：{manifest.get('source_episode', '')}",
        "",
        "| # | 段 | 分镜 | 生成槽位 | 候选 ID | 台词 | 源时间 | 选择理由 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, row in enumerate(rows, start=1):
        st = row.get("source_time") or {}
        span = ""
        if st.get("start") is not None and st.get("end") is not None:
            span = f"{st['start']}-{st['end']}s"
        cells = [
            str(i), row.get("segment", ""), str(row.get("shot", "")),
            row.get("slot", ""), row.get("candidate_id", ""),
            str(row.get("subtitle", "")).replace("|", "\\|"),
            span, str(row.get("selection_reason", "")).replace("|", "\\|"),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if not rows:
        lines.append("| — | 无候选 | — | — | — | — | — | — |")
    return "\n".join(lines) + "\n"


def export(manifest: dict, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_markdown(manifest))
    return path
