"""时间线编排、校验与渲染。

支持只用生成片段的有效区间；字幕按实际音轨时长分句，不截断长句充数。
不足时长回策划，不用循环/慢放/空镜拖长。
"""
from __future__ import annotations

from pathlib import Path

from ..source_identity import atomic_json, json_hash
from . import approval, media, settings
from .errors import FlowError

CLOSING = "。！？；!?;"
SPLIT = "。！？；，、,;!?"


def _best_asset(store, task_id: str) -> dict | None:
    assets = [a for a in store.assets_for_task(task_id)
              if a["status"] == "approved" and store.asset_review(a["asset_id"])]
    assets.sort(key=lambda a: a["attempt"], reverse=True)
    return assets[0] if assets else None


def _select_range(store, asset: dict, shot: dict,
                  selection: dict | None) -> dict:
    review = store.asset_review(asset["asset_id"])
    ranges = review["usable_ranges"]
    planned = int(shot["planned_ms"])
    if selection:
        start, end = selection.get("start_ms"), selection.get("end_ms")
        if (type(start) is not int or type(end) is not int or end <= start):
            raise FlowError("TIMELINE_SELECTION_INVALID", shot["shot_id"], stage="timeline")
        if not any(r["start_ms"] <= start and end <= r["end_ms"] for r in ranges):
            raise FlowError("TIMELINE_RANGE_OUTSIDE_APPROVED",
                            f"{shot['shot_id']} 选择的区间不在已验收可用区间内",
                            stage="timeline")
        return {"source_in_ms": start, "source_out_ms": end}
    best = max(ranges, key=lambda r: (r["end_ms"] - r["start_ms"], -r["start_ms"]))
    length = min(max(planned, 1000), best["end_ms"] - best["start_ms"])
    return {"source_in_ms": best["start_ms"],
            "source_out_ms": best["start_ms"] + length}


def split_subtitles(text: str, start_ms: int, end_ms: int, *,
                    max_chars: int = 18, max_duration_ms: int = 5000) -> list[dict]:
    """按标点分句并换行；总时长按字数比例分配，绝不截断后静默。"""
    text = " ".join((text or "").split())
    if not text:
        return []
    raw_parts = []
    buffer = ""
    for char in text:
        buffer += char
        if char in SPLIT:
            raw_parts.append(buffer)
            buffer = ""
    if buffer:
        raw_parts.append(buffer)
    clauses = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        while len(part) > max_chars:
            clauses.append(part[:max_chars])
            part = part[max_chars:]
        if part:
            clauses.append(part)
    total_chars = sum(len(c) for c in clauses)
    if total_chars == 0:
        return []
    events, cursor = [], start_ms
    for index, clause in enumerate(clauses):
        if index == len(clauses) - 1:
            end = end_ms
        else:
            share = max(400, int((end_ms - start_ms) * len(clause) / total_chars))
            end = min(end_ms, cursor + min(share, max_duration_ms))
        if end <= cursor:
            end = min(end_ms, cursor + 400)
        if end <= cursor:
            break
        events.append({"start_ms": cursor, "end_ms": end, "text": clause})
        cursor = end
    return events


def build_timeline(store, plan_revision_id: str, story: dict,
                   evidence_snapshot: dict, *, policy: dict | None = None,
                   selections: dict | None = None) -> dict:
    pol = policy or settings.policy()
    approval.assert_production_allowed(store, plan_revision_id, story,
                                       evidence_snapshot, policy=pol)
    plan = store.plan(plan_revision_id)
    payload_plan = plan["payload"]
    items, blocking = [], []
    cursor = 0
    subtitle_events = []
    audio_tracks = []
    for shot in payload_plan["shots"]:
        try:
            task = store.task_for_shot(plan_revision_id, shot["shot_id"])
        except FlowError:
            blocking.append(f"{shot['shot_id']} 缺少制作任务")
            continue
        asset = _best_asset(store, task["task_id"])
        if asset is None:
            blocking.append(f"{shot['shot_id']} 没有已验收通过的生成片段")
            continue
        selected = _select_range(store, asset, shot,
                                (selections or {}).get(shot["shot_id"]))
        duration = selected["source_out_ms"] - selected["source_in_ms"]
        item = {
            "shot_id": shot["shot_id"],
            "task_id": task["task_id"],
            "generated_asset_id": asset["asset_id"],
            "asset_sha256": asset["sha256"],
            "source_in_ms": selected["source_in_ms"],
            "source_out_ms": selected["source_out_ms"],
            "timeline_start_ms": cursor,
            "timeline_end_ms": cursor + duration,
            "speed": 1.0,
            "dialogue": shot.get("dialogue") or "",
            "has_audio": bool(asset["payload"]["metadata"]["media"].get("has_audio")),
        }
        items.append(item)
        audio_tracks.append({"asset_id": asset["asset_id"],
                             "mode": pol["audio_mode"],
                             "has_audio": item["has_audio"]})
        subtitle_events.extend(split_subtitles(
            item["dialogue"], item["timeline_start_ms"], item["timeline_end_ms"],
            max_chars=int(pol["subtitle"]["max_chars_per_line"]),
            max_duration_ms=int(pol["subtitle"]["max_duration_ms"])))
        cursor = item["timeline_end_ms"]
    if blocking:
        raise FlowError("TIMELINE_ASSETS_MISSING", "时间线缺少可用片段", stage="timeline",
                        blocking_issues=blocking)

    payload = {
        "schema_version": 1,
        "plan_revision_id": plan_revision_id,
        "plan_semantic_hash": plan["semantic_hash"],
        "timebase": "milliseconds",
        "video_items": items,
        "audio_tracks": audio_tracks,
        "audio_mode": pol["audio_mode"],
        "audio_permission": (store.project().get("policy") or {}).get(
            "audio_permission", pol.get("audio_permission")),
        "subtitle_events": subtitle_events,
        "overlays": [{"kind": "ai_label",
                      "text": "含 AI 生成内容 · 仅供娱乐",
                      "required": bool(pol["qc"]["require_ai_label"])}],
        "render_spec": {"aspect_ratio": pol["aspect_ratio"],
                        "resolution": pol["qc"]["resolution"], "fps": 30},
        "total_ms": cursor,
    }
    validation = validate_timeline(store, payload, policy=pol)
    if not validation["ok"]:
        raise FlowError("TIMELINE_INVALID", "时间线未通过校验", stage="timeline",
                        blocking_issues=validation["blocking_issues"])
    timeline = store.create_timeline(plan_revision_id, payload)
    store.set_stage("ASSEMBLED")
    return {**timeline, "validation": validation}


def validate_timeline(store, payload: dict, *, policy: dict | None = None) -> dict:
    pol = policy or settings.policy()
    checks, blocking = [], []
    items = payload.get("video_items") or []
    if not items:
        return {"ok": False, "checks": [], "blocking_issues": ["时间线没有视频片段"]}
    duration_ms = int(payload.get("total_ms") or 0)

    for index, item in enumerate(items):
        try:
            asset = store.generated_asset(item["generated_asset_id"])
            task = store.task(item["task_id"])
        except FlowError as exc:
            blocking.append(f"{item.get('shot_id')} 片段绑定失效：{exc.message}")
            continue
        checks.append({"check_id": "TL-BINDING", "status": "PASS",
                       "reason": f"{item['shot_id']} 绑定任务与片段"})
        if task["plan_revision_id"] != payload["plan_revision_id"]:
            blocking.append(f"{item['shot_id']} 任务属于其他方案版本")
        media_duration = int(asset["payload"]["metadata"]["media"]["duration_ms"])
        start, end = item.get("source_in_ms"), item.get("source_out_ms")
        if (type(start) is not int or type(end) is not int or start < 0
                or end <= start or end > media_duration):
            blocking.append(f"{item['shot_id']} 入出点非法或超出片段范围")
            continue
        review = store.asset_review(item["generated_asset_id"])
        if review is None or review["decision"] != "approved":
            blocking.append(f"{item['shot_id']} 片段未通过逐镜验收")
        elif not any(r["start_ms"] <= start and end <= r["end_ms"]
                     for r in review["usable_ranges"]):
            blocking.append(f"{item['shot_id']} 使用区间超出验收可用区间")
        speed = item.get("speed", 1.0)
        if not isinstance(speed, (int, float)) or speed <= 0 or speed > 2:
            blocking.append(f"{item['shot_id']} 速度参数不支持")
        expected = int((end - start) / speed) if isinstance(speed, (int, float)) and speed else 0
        if item["timeline_end_ms"] - item["timeline_start_ms"] != expected:
            blocking.append(f"{item['shot_id']} 时间换算不一致")
        if index == 0 and item["timeline_start_ms"] != 0:
            blocking.append("时间线首段未从 0 开始")
        if index > 0:
            previous = items[index - 1]
            if item["timeline_start_ms"] < previous["timeline_end_ms"]:
                blocking.append(f"{item['shot_id']} 与上一段重叠")
            elif item["timeline_start_ms"] > previous["timeline_end_ms"]:
                checks.append({"check_id": "TL-GAP", "status": "WARN",
                               "reason": f"{item['shot_id']} 前存在空白间隙"})
    for event in payload.get("subtitle_events") or []:
        if not (0 <= event["start_ms"] < event["end_ms"] <= duration_ms):
            blocking.append("字幕事件超出时间线时长")
            break
    minimum, maximum = (int(x * 1000) for x in pol["delivery_duration_range_sec"])
    if duration_ms < minimum:
        checks.append({"check_id": "TL-DURATION", "status": "FAIL",
                       "reason": f"成片时长 {duration_ms / 1000:.1f}s 低于项目下限"})
    if duration_ms > maximum:
        checks.append({"check_id": "TL-DURATION", "status": "WARN",
                       "reason": f"成片时长 {duration_ms / 1000:.1f}s 超过项目上限"})
    if payload.get("audio_mode") == "none":
        checks.append({"check_id": "TL-AUDIO", "status": "FAIL",
                       "reason": "显式关闭音轨；缺对白不得用静音掩盖"})
    return {"ok": not blocking, "checks": checks, "blocking_issues": blocking,
            "duration_ms": duration_ms, "item_count": len(items)}


def render(store, timeline_revision_id: str, *, policy: dict | None = None) -> dict:
    pol = policy or settings.policy()
    timeline = store.timeline(timeline_revision_id)
    payload = timeline["payload"]
    items = []
    for item in payload["video_items"]:
        asset = store.generated_asset(item["generated_asset_id"])
        items.append({"path": asset["path"],
                      "source_in_ms": item["source_in_ms"],
                      "source_out_ms": item["source_out_ms"],
                      "has_audio": item["has_audio"]})
    if not media.ffmpeg_available():
        raise FlowError("FFMPEG_UNAVAILABLE", "未配置可用的 ffmpeg，无法渲染",
                        stage="render", retryable=True)
    output = (store.context.run_dir(timeline["run_id"]) / "output"
              / f"{timeline_revision_id}.mp4")
    label = next((o["text"] for o in payload.get("overlays") or []
                  if o.get("kind") == "ai_label"), None)
    result = media.render(
        items, output,
        resolution=payload["render_spec"]["resolution"],
        fps=int(payload["render_spec"]["fps"]),
        label_text=label if pol["qc"]["require_ai_label"] else None,
        subtitles=payload.get("subtitle_events"),
        audio_mode="none" if payload.get("audio_mode") == "none" else "original",
        subtitle_style={**pol["subtitle"], "label_fontsize": 40})
    if not result["ok"]:
        raise FlowError("RENDER_FAILED", str(result["error"]), stage="render",
                        retryable=True)
    render_info = {"timeline_revision_id": timeline_revision_id,
                   "timeline_payload_hash": timeline["payload_hash"],
                   "plan_revision_id": payload["plan_revision_id"],
                   "plan_semantic_hash": payload["plan_semantic_hash"],
                   "output": str(output), "duration": result["duration"],
                   "width": result["width"], "height": result["height"],
                   "audio_embedded": result["audio_embedded"],
                   "label_burned": result["label_burned"],
                   "subtitles_rendered": result["subtitles_rendered"],
                   "expected_subtitles": len(payload.get("subtitle_events") or []),
                   "file_sha256": json_hash(f"{output}|{result['duration']}")}
    record = store.context.run_dir(timeline["run_id"]) / "output"
    atomic_json(record / f"{timeline_revision_id}_render.json", render_info)
    render_info["record_path"] = str(record / f"{timeline_revision_id}_render.json")
    return render_info
