"""方案预演：有权使用的原片粗剪优先，电镜不可用时退化为分镜预演。

预演一律标记“内部预演 / 未正式交付”，不得与最终 AI 成片混同。
"""
from __future__ import annotations

from pathlib import Path

from .. import probe
from ..source_identity import atomic_json, json_hash
from . import media, settings
from .errors import FlowError


def _items(plan: dict, video_path: str, has_audio: bool) -> list[dict]:
    return [{"path": video_path,
             "source_in_ms": shot["source_start_ms"],
             "source_out_ms": shot["source_end_ms"],
             "has_audio": has_audio}
            for shot in plan["shots"]]


def render_preview(store, plan_revision_id: str, evidence_snapshot: dict,
                   *, policy: dict | None = None) -> dict:
    pol = policy or settings.policy()
    plan = store.plan(plan_revision_id)
    video = evidence_snapshot["assets"][0]["path"]
    info = probe.probe_media(video)
    if info.get("error") or not info.get("has_video"):
        raise FlowError("SOURCE_PROBE_FAILED", str(info.get("error")), stage="preview")
    run_id = plan["run_id"]
    output_dir = store.context.run_dir(run_id) / "previews"
    output_dir.mkdir(parents=True, exist_ok=True)
    record_path = output_dir / f"{plan_revision_id}_preview.json"
    items = _items(plan["payload"], video, bool(info.get("has_audio")))
    total_ms = sum(i["source_out_ms"] - i["source_in_ms"] for i in items)

    record = {
        "schema_version": 1, "plan_revision_id": plan_revision_id,
        "plan_semantic_hash": plan["semantic_hash"],
        "kind": "internal_preview", "delivery": False,
        "label": "内部预演 · 未正式交付",
        "source_asset_id": evidence_snapshot["assets"][0]["asset_id"],
        "items": items, "total_ms": total_ms,
        "missing_dynamic_frames": False,
        "missing_audio": not bool(info.get("has_audio")),
        "requires_human_visual_review": True,
        "audio_source": "original_preview_only",
    }

    if not media.ffmpeg_available():
        record.update(status="storyboard_only", output=None,
                      note="FFmpeg 不可用：只输出分镜预演，未生成动态画面",
                      storyboard=[{"shot_id": s["shot_id"], "role": s["role"],
                                   "start_ms": s["source_start_ms"],
                                   "end_ms": s["source_end_ms"],
                                   "dialogue": s["dialogue"]} for s in plan["payload"]["shots"]])
        atomic_json(record_path, record)
        return {**record, "path": str(record_path)}

    output = output_dir / f"{plan_revision_id}_preview.mp4"
    result = media.render(items, output,
                          resolution={"width": int(pol["qc"]["resolution"]["width"]),
                                      "height": int(pol["qc"]["resolution"]["height"])},
                          fps=30, label_text=record["label"],
                          audio_mode="original" if info.get("has_audio") else "none",
                          subtitle_style=pol["subtitle"])
    if not result["ok"]:
        raise FlowError("PREVIEW_RENDER_FAILED", str(result["error"]), stage="preview",
                        retryable=True)
    record.update(status="rendered", output=str(output), duration=result["duration"],
                  resolution={"width": result["width"], "height": result["height"]},
                  audio_embedded=result["audio_embedded"],
                  missing_audio=not result["audio_embedded"],
                  output_sha256=json_hash(str(output))[:16],
                  note="内部预演，仅用于检验故事与节奏")
    atomic_json(record_path, record)
    return {**record, "path": str(record_path)}
