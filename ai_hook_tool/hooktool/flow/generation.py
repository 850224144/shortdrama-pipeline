"""制作任务包、人工生成导入与逐镜验收。

任务绑定 project / plan_revision / shot / 参考版本；导入前校验目录授权、哈希与
媒体信息；旧版本任务可留作历史，但不能直接进入当前时间线。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import probe
from ..source_identity import atomic_json, contained_path, file_hash, json_hash
from . import approval, settings
from .errors import FlowError

CHECK_KEYS = ("identity", "action", "dialogue", "usable_footage")
CHECK_VALUES = ("pass", "fail", "pending")


def reference_pack(story: dict, plan: dict) -> dict:
    used = sorted({cid for shot in plan["shots"] for cid in shot.get("character_ids") or []})
    characters = [c for c in story["characters"] if c["char_id"] in used]
    references = [{"char_id": c["char_id"],
                   "provisional_name": c.get("provisional_name"),
                   "reference_images": c.get("reference_images") or [],
                   "appearance": c.get("appearance")} for c in characters]
    missing = [r["char_id"] for r in references if not r["reference_images"]]
    version = json_hash({"references": references})[:16]
    return {"version": version, "references": references,
            "missing_references": missing,
            "status": "requires_reference_images" if missing else "ready",
            "note": "人物无法区分时先补参考，不靠提示词硬猜"}


def _task_payload(store, plan: dict, shot: dict, reference: dict,
                  policy: dict, story: dict) -> dict:
    events = {e["event_id"]: e for e in story["events"]}
    event = next((events[eid] for eid in shot.get("event_ids") or [] if eid in events), None)
    dialogue = (shot.get("dialogue") or (event or {}).get("dialogue_text") or "")[:160]
    planned = int(shot["planned_ms"])
    return {
        "project_id": store.context.project_id,
        "plan_revision_id": plan["plan_revision_id"],
        "plan_semantic_hash": plan["semantic_hash"],
        "shot_id": shot["shot_id"],
        "role": shot["role"],
        "reference_version": reference["version"],
        "approved_action": (event or {}).get("action") or "按已批准镜头说明执行",
        "approved_fact": {
            "event_ids": shot.get("event_ids"),
            "evidence_ids": shot.get("evidence_ids"),
            "verification_status": (event or {}).get("verification_status"),
        },
        "dialogue": dialogue,
        "audio_mode": policy["audio_mode"],
        "camera": shot.get("camera") or "机位与构图可设计；不得改变人物身份与事件结果",
        "expression_note": "机位/构图属表达创新，不写入事实；事实只能来自 approved_fact",
        "continuity": [
            f"沿用 {shot['shot_id']} 已批准的人物身份与服装",
            "动作主体不得互换",
            "不得补拍未在源片出现的关键事实",
        ],
        "forbidden": [
            "不得新增人物或替换身份",
            "不得改写台词语义或说话人",
            "不得新增旁白/解说",
            "不得用慢放、循环或空镜补足时长",
        ],
        "request_duration_sec": round((planned + 1000) / 1000, 2),
        "expected_usable_sec": round(planned / 1000, 2),
        "source_span_ms": [shot["source_start_ms"], shot["source_end_ms"]],
        "output_spec": {"aspect_ratio": policy["aspect_ratio"],
                        "resolution": policy["qc"]["resolution"],
                        "fps": 30},
        "references": reference["references"],
        "missing_references": reference["missing_references"],
    }


def _prompt_markdown(task: dict) -> str:
    return "\n".join([
        f"# 制作任务 {task['task_id']}",
        "",
        f"- 项目：{task['payload']['project_id']}",
        f"- 方案版本：{task['payload']['plan_revision_id']}",
        f"- 镜头：{task['payload']['shot_id']}（{task['payload']['role']}）",
        f"- 参考版本：{task['payload']['reference_version']}",
        f"- 请求时长：{task['payload']['request_duration_sec']}s；"
        f"预期有效：{task['payload']['expected_usable_sec']}s",
        "",
        "## 已批准动作",
        task["payload"]["approved_action"],
        "",
        "## 台词",
        task["payload"]["dialogue"] or "（无台词，按动作与反应执行）",
        "",
        "## 镜头表达",
        task["payload"]["camera"],
        "",
        "## 连续性",
        *[f"- {item}" for item in task["payload"]["continuity"]],
        "",
        "## 禁止",
        *[f"- {item}" for item in task["payload"]["forbidden"]],
        "",
        "## 人物参考",
        *[f"- {r['char_id']}：{r['provisional_name'] or '待确认'}；"
          f"参考图 {len(r['reference_images'])} 张" for r in task["payload"]["references"]],
        "",
        "> 任务包只描述已批准内容；生成结果需逐镜验收，未验收不得进入时间线。",
    ]) + "\n"


def create_tasks(store, plan_revision_id: str, story: dict,
                 evidence_snapshot: dict, *, policy: dict | None = None) -> dict:
    pol = policy or settings.policy()
    approval.assert_production_allowed(store, plan_revision_id, story,
                                       evidence_snapshot, policy=pol)
    plan = store.plan(plan_revision_id)
    reference = reference_pack(story, plan["payload"])
    tasks = []
    for shot in plan["payload"]["shots"]:
        payload = _task_payload(store, plan, shot, reference, pol, story)
        task = store.create_task(plan_revision_id, shot["shot_id"], payload)
        prompt_path = (store.context.run_dir(plan["run_id"]) / "generation" / "tasks"
                       / f"{task['task_id']}.md")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(_prompt_markdown(task), encoding="utf-8")
        tasks.append({**task, "prompt_path": str(prompt_path)})
    store.set_stage("PRODUCING")
    return {"plan_revision_id": plan_revision_id, "tasks": tasks,
            "reference_pack": reference,
            "production_mode": pol["production_mode"],
            "note": "人工在生成平台按任务出片；系统只管理任务、导入与验收"}


def _approved_roots(store) -> list[Path]:
    project = store.project()
    roots = [Path(p).resolve() for p in (project.get("import_roots") or [])]
    default = (store.root / "imports").resolve()
    roots.append(default)
    return roots


def import_asset(store, task_id: str, file_path: str,
                 metadata: dict | None = None) -> dict:
    task = store.task(task_id)
    source = Path(file_path).resolve(strict=True)
    if not source.is_file():
        raise FlowError("IMPORT_NOT_FILE", str(source), stage="generation")
    roots = _approved_roots(store)
    if not any(source.is_relative_to(root) for root in roots):
        raise FlowError("IMPORT_PATH_NOT_APPROVED",
                        "导入文件必须位于用户批准的目录", stage="generation",
                        blocking_issues=[str(root) for root in roots])
    info = probe.probe_media(str(source))
    if info.get("error") or not info.get("has_video") or not info.get("duration"):
        raise FlowError("IMPORT_MEDIA_INVALID", str(info.get("error")), stage="generation")
    digest = file_hash(source)
    stored = (store.context.run_dir(task["run_id"]) / "generation" / "assets"
              / f"{json_hash(task_id + digest)[:16]}{source.suffix.lower()}")
    stored.parent.mkdir(parents=True, exist_ok=True)
    if not stored.exists():
        shutil.copy2(source, stored)
    if file_hash(stored) != digest:
        raise FlowError("IMPORT_COPY_MISMATCH", str(stored), stage="generation")
    plan = store.plan(task["plan_revision_id"])
    latest = store.latest_plan(plan["proposal_id"])
    stale = latest["plan_revision_id"] != plan["plan_revision_id"]
    approved = store.valid_plan_review(plan) is not None
    payload_metadata = {
        "provider": (metadata or {}).get("provider") or "manual",
        "provided_by": (metadata or {}).get("provided_by") or "user",
        "attempt_reason": (metadata or {}).get("attempt_reason"),
        "media": {"duration_ms": int(round(info["duration"] * 1000)),
                  "width": info["width"], "height": info["height"],
                  "has_audio": bool(info["has_audio"])},
        "original_path": str(source), "stored_path": str(stored),
        "stale_plan_revision": stale, "plan_approved": approved,
    }
    asset = store.add_generated_asset(task_id, path=str(stored), sha256=digest,
                                      metadata=payload_metadata)
    if stale or not approved:
        with store.connection() as conn:
            conn.execute("UPDATE generated_assets SET status='history' WHERE asset_id=?",
                         (asset["asset_id"],))
        asset["status"] = "history"
    return {**asset, "stale_plan_revision": stale, "plan_approved": approved,
            "usable": not (stale or not approved)}


def review_asset(store, asset_id: str, decision: str, usable_ranges: list[dict],
                 checks: dict, reviewer: str | None = None) -> dict:
    asset = store.generated_asset(asset_id)
    duration_ms = int(asset["payload"]["metadata"]["media"]["duration_ms"])
    ranges = []
    for row in usable_ranges:
        start, end = row.get("start_ms"), row.get("end_ms")
        if (type(start) is not int or type(end) is not int or start < 0
                or end <= start or end > duration_ms):
            raise FlowError("USABLE_RANGE_INVALID", str(row), stage="generation")
        ranges.append({"start_ms": start, "end_ms": end})
    if not ranges:
        raise FlowError("USABLE_RANGE_REQUIRED", "必须标注可用区间", stage="generation")
    for key in CHECK_KEYS:
        if checks.get(key) not in CHECK_VALUES:
            raise FlowError("ASSET_CHECK_INVALID", f"{key}={checks.get(key)}",
                            stage="generation")
    if decision == "approved":
        failing = [k for k in ("identity", "action", "dialogue") if checks[k] == "fail"]
        if failing:
            raise FlowError("ASSET_CHECK_FAILED", f"关键检查失败：{failing}",
                            stage="generation")
        if asset["status"] == "history":
            raise FlowError("ASSET_STALE_PLAN", "旧方案版本的片段不能验收进入当前时间线",
                            stage="generation")
    review = store.add_asset_review(asset_id, decision=decision,
                                    usable_ranges=ranges, checks=checks,
                                    reviewer=reviewer)
    return {**review, "shot_id": asset["payload"]["shot_id"],
            "task_id": asset["payload"]["task_id"]}
