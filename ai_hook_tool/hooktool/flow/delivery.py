"""正式交付：只导出通过分层门禁与最终人工验收的成片。

本功能只交付，不上传、不发布；文件写入项目输出目录并登记哈希。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from ..source_identity import atomic_json, file_hash, json_hash
from . import quality, settings
from .errors import FlowError


def _safe_name(value: str) -> str:
    cleaned = "".join(c for c in value if c not in '\\/:*?"<>|').strip()
    return cleaned[:40] or "hook"


def export(store, timeline_revision_id: str, *, policy: dict | None = None) -> dict:
    pol = policy or settings.policy()
    timeline = store.timeline(timeline_revision_id)
    latest_qc = store.latest_qc(timeline_revision_id)
    if latest_qc is None:
        raise FlowError("QC_MISSING", "尚未质检，不能导出", stage="delivery")
    qc = latest_qc["report"]
    qc["qc_id"] = latest_qc["qc_id"]
    if qc.get("timeline_payload_hash") != timeline["payload_hash"]:
        raise FlowError("QC_STALE", "质检结果对应旧时间线版本", stage="delivery")
    perms = quality.permissions(store, qc)
    if not perms["delivery_allowed"]:
        blocking = list(qc.get("blocking_issues") or [])
        if perms["final_review"] is None:
            blocking.append("缺少最终人工验收")
        elif perms["final_review"]["decision"] != "approved":
            blocking.append("最终人工验收未通过")
        raise FlowError("DELIVERY_BLOCKED", "分层门禁未通过，不能正式导出",
                        stage="delivery", blocking_issues=blocking)
    render = qc.get("render") or {}
    source = Path(render.get("output") or "")
    if not source.is_file():
        raise FlowError("DELIVERY_FILE_MISSING", str(source), stage="delivery")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    drama = Path(store.project()["source_dir"]).name
    output_dir = store.context.run_dir(timeline["run_id"]) / "output" / "deliveries"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{_safe_name(drama)}_E01_AI钩子_{timestamp}.mp4"
    if target.exists():
        raise FlowError("DELIVERY_EXISTS", str(target), stage="delivery")
    shutil.copy2(source, target)
    digest = file_hash(target)

    plan = store.plan(timeline["plan_revision_id"])
    payload = {
        "schema_version": 1,
        "delivery_id": "deliv_" + json_hash(f"{timeline['payload_hash']}|{digest}")[:16],
        "timeline_revision_id": timeline_revision_id,
        "timeline_payload_hash": timeline["payload_hash"],
        "plan_revision_id": plan["plan_revision_id"],
        "plan_semantic_hash": plan["semantic_hash"],
        "qc_id": qc["qc_id"],
        "final_review": perms["final_review"],
        "file": str(target),
        "sha256": digest,
        "duration": render.get("duration"),
        "resolution": [render.get("width"), render.get("height")],
        "ai_label_burned": bool(render.get("label_burned")),
        "audio_embedded": bool(render.get("audio_embedded")),
        "publication": "disabled",
        "policy": {"delivery_duration_range_sec": pol["delivery_duration_range_sec"],
                   "require_ai_label": pol["qc"]["require_ai_label"]},
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    record = store.add_delivery(timeline_revision_id, payload)
    store.patch_project({"stage": "DELIVERABLE", "stage_reason": None})
    manifest_path = output_dir / f"{payload['delivery_id']}_manifest.json"
    atomic_json(manifest_path, payload)
    return {**payload, "delivery_id": record["delivery_id"],
            "manifest_path": str(manifest_path)}
