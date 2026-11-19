"""Project-scoped evidence preparation. Never invokes story planning or production."""
from __future__ import annotations

import importlib.metadata
import json
import math
from pathlib import Path

from . import probe, transcribe
from .project_store import ProjectStore
from .source_identity import atomic_json, contained_path, file_hash, json_hash
from .transcript_formats import PARSER_VERSION, parse_transcript, validate_events

ANALYZER_VERSION = "p0-1"


def _asset(root: str, path: str, kind: str) -> dict:
    selected = contained_path(root, path)
    digest = file_hash(selected)
    return {"asset_id": kind + "_" + digest, "kind": kind,
            "path": str(selected), "sha256": digest}


def _signature(subtitle: dict | None) -> dict:
    signature = {"schema_version": 1, "analyzer": ANALYZER_VERSION,
                 "parser": PARSER_VERSION, "mode": "subtitle" if subtitle else "local_asr"}
    if subtitle is None:
        try:
            version = importlib.metadata.version("faster-whisper")
        except importlib.metadata.PackageNotFoundError:
            version = "unavailable"
        model = Path(transcribe.WHISPER_MODEL_PATH)
        # Model contents, not only the directory name, determine ASR cache validity.
        model_files = ({str(p.relative_to(model)): file_hash(p)
                        for p in sorted(model.rglob("*")) if p.is_file()}
                       if model.is_dir() else {})
        signature.update(model=str(model.resolve()), model_files=model_files,
                         implementation=version, language="zh", beam_size=5,
                         vad_filter=True, devices=["cuda:int8", "cpu:int8"])
    return signature


def _validate_payload(payload: dict, assets: list[dict], duration_ms: int) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("INVALID_EVIDENCE_SCHEMA")
    video = assets[0]
    transcript_id = assets[1]["asset_id"] if len(assets) == 2 else "asr:" + video["asset_id"]
    validate_events(payload.get("utterances"), duration_ms, video["asset_id"])
    for event in payload["utterances"]:
        if (event.get("transcript_asset_id") != transcript_id or
                event.get("verification_status") != "extracted" or
                event.get("provenance") != ("subtitle" if len(assets) == 2 else "local_asr")):
            raise ValueError("EVIDENCE_PROVENANCE_MISMATCH")


def prepare_project(store: ProjectStore, *, allow_asr=False) -> dict:
    project = store.project()
    run_id, project = store.begin_run(project["revision"])
    try:
        selection = project["selection"]
        if selection["issues"] or not selection.get("episode"):
            raise ValueError("NEEDS_INPUT: 请先完成首集和字幕归属确认")
        video = _asset(project["source_dir"], selection["episode"], "video")
        subtitle = (_asset(project["source_dir"], selection["transcript"], "transcript")
                    if selection.get("transcript") else None)
        assets = [video] + ([subtitle] if subtitle else [])
        previous = project.get("bound_assets")
        if previous is not None and previous != assets:
            raise ValueError("SOURCE_CHANGED: 素材或字幕已改变，请 project confirm 后重新准备")
        if subtitle is None and not allow_asr:
            raise ValueError("ASR_PERMISSION_REQUIRED: 无可用字幕；本地转写需显式 --allow-asr")
        media = probe.probe_media(video["path"])
        duration = media.get("duration")
        if (media.get("error") or not media.get("has_video") or
                not isinstance(duration, (int, float)) or isinstance(duration, bool) or
                not math.isfinite(duration) or duration <= 0):
            raise ValueError("MEDIA_PROBE_FAILED: 首集媒体信息不可验证")
        duration_ms = round(duration * 1000)
        signature = _signature(subtitle)
        identity = {"assets": [{"kind": a["kind"], "sha256": a["sha256"]} for a in assets],
                    "signature": signature}
        cache_key = json_hash(identity)
        cache_path = store.root / "cache" / (cache_key + ".json")
        payload, cache_hit = None, False
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                candidate = cached["payload"]
                if cached["identity"] != identity or cached["payload_hash"] != json_hash(candidate):
                    raise ValueError("CACHE_MISMATCH")
                _validate_payload(candidate, assets, duration_ms)
                payload, cache_hit = candidate, True
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass  # Damaged cache is not evidence; parse/transcribe the selected source again.
        if payload is None:
            if subtitle:
                text = Path(subtitle["path"]).read_text(encoding="utf-8-sig", errors="strict")
                extension = Path(subtitle["path"]).suffix
                transcript_id = subtitle["asset_id"]
            else:
                lines, reason = transcribe.transcribe_uncached(video["path"])
                if reason:
                    raise ValueError("ASR_UNAVAILABLE: " + reason)
                text, extension = "\n".join(lines), ".txt"
                transcript_id = "asr:" + video["asset_id"]
            payload = parse_transcript(text, extension, video["asset_id"], transcript_id,
                                       duration_ms, "subtitle" if subtitle else "local_asr")
        _validate_payload(payload, assets, duration_ms)
        for asset in assets:
            if file_hash(contained_path(project["source_dir"], asset["path"])) != asset["sha256"]:
                raise ValueError("SOURCE_CHANGED: 准备期间素材发生变化")
        snapshot = {"schema_version": 1, "project_id": project["project_id"],
                    "run_id": run_id, "project_revision": project["revision"],
                    "source_scope": "confirmed_first_episode", "assets": assets,
                    "selection": selection, "duration_ms": duration_ms,
                    "identity": identity, "cache_key": cache_key,
                    "cache_hit": cache_hit, "evidence": payload,
                    "production_allowed": False, "delivery_allowed": False,
                    "note": "仅首集证据包；未执行剧情策划、视觉分析或生成"}
        atomic_json(cache_path, {"identity": identity, "payload": payload,
                                 "payload_hash": json_hash(payload)})
        store.finish_run(run_id, snapshot=snapshot, assets=assets)
        return {"status": "succeeded", "project": str(store.root), "run_id": run_id,
                "evidence_path": str(store.context.run_dir(run_id) / "evidence" / "evidence.json"),
                "utterance_count": len(payload["utterances"]), "cache_hit": cache_hit,
                "issues": payload["issues"], "message": snapshot["note"]}
    except Exception as exc:
        code = str(exc).split(":", 1)[0] if isinstance(exc, ValueError) else type(exc).__name__
        issue = {"code": code, "message": str(exc), "stage": "prepare", "retryable": False}
        store.finish_run(run_id, error=issue)
        return {"status": "failed", "project": str(store.root), "run_id": run_id,
                "error": issue}


def verify_run(store: ProjectStore, run_id: str) -> dict:
    """Read only: verify DB-bound snapshot, current source identity and scope."""
    try:
        run = store.run(run_id)
        if run["status"] != "succeeded":
            raise ValueError("RUN_NOT_SUCCEEDED")
        expected_path = store.context.run_dir(run_id) / "evidence" / "evidence.json"
        if Path(run["snapshot_path"]) != expected_path.relative_to(store.root):
            raise ValueError("SNAPSHOT_PATH_MISMATCH")
        resolved = contained_path(store.root, expected_path)
        snapshot = json.loads(resolved.read_text(encoding="utf-8"))
        if json_hash(snapshot) != run["snapshot_hash"]:
            raise ValueError("SNAPSHOT_HASH_MISMATCH")
        inputs = json.loads(run["input_json"])
        project = store.project()
        if (snapshot["schema_version"] != 1 or snapshot["run_id"] != run_id or
                snapshot["project_id"] != store.context.project_id or
                snapshot["project_revision"] != inputs["revision"] or
                snapshot["selection"] != inputs["selection"]):
            raise ValueError("SNAPSHOT_SCOPE_MISMATCH")
        if project["revision"] != inputs["revision"]:
            raise ValueError("PROJECT_REVISION_CHANGED: 此run仅作为历史记录")
        assets = snapshot["assets"]
        if (not assets or assets[0]["kind"] != "video" or
                assets[0]["path"] != inputs["selection"]["episode"]):
            raise ValueError("SOURCE_SCOPE_MISMATCH")
        wanted_subtitle = inputs["selection"].get("transcript")
        if len(assets) != (2 if wanted_subtitle else 1):
            raise ValueError("TRANSCRIPT_SCOPE_MISMATCH")
        if wanted_subtitle and (assets[1]["kind"] != "transcript" or assets[1]["path"] != wanted_subtitle):
            raise ValueError("TRANSCRIPT_SCOPE_MISMATCH")
        for asset in assets:
            digest = file_hash(contained_path(inputs["source_dir"], asset["path"]))
            if digest != asset["sha256"] or asset["asset_id"] != asset["kind"] + "_" + digest:
                raise ValueError("SOURCE_CHANGED")
        if json_hash(snapshot["identity"]) != snapshot["cache_key"]:
            raise ValueError("CACHE_IDENTITY_MISMATCH")
        _validate_payload(snapshot["evidence"], assets, snapshot["duration_ms"])
        return {"status": "pass", "run_id": run_id, "production_allowed": False,
                "message": "来源与证据完整性核验通过；不等于剧情事实人工核验或成片批准"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"status": "fail", "run_id": run_id, "error": str(exc)}
