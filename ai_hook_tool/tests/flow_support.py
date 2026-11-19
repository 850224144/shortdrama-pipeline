"""Flow 测试共用构造：不依赖真实影视素材，只构造可定位证据。"""
from __future__ import annotations

import copy
import uuid

RUN_ID = str(uuid.uuid4())

from hooktool.flow import settings
from hooktool.flow.planner import build_proposals
from hooktool.flow.recall import recall
from hooktool.flow.store import FlowStore
from hooktool.flow.story import build_story
from hooktool.material_strict import discover, select_sources

LINES = [
    (1000, 3600, "你敢把这份合同撕了试试？"),
    (5000, 7600, "我为什么不敢，这婚我早就想离了。"),
    (9200, 11800, "你疯了，孩子怎么办？"),
    (13200, 15800, "孩子归我，公司股份一分都不会给你。"),
    (17600, 20200, "其实公司早就被我抵押了。"),
    (21800, 24400, "什么？你居然背着我做了这种事！"),
    (26200, 28800, "所以你今天必须签字。"),
    (30200, 32800, "我要是不签呢？"),
    (34400, 37000, "那你就等着法院的传票吧。"),
    (38200, 40800, "等等，我好像误会你了。"),
    (41800, 44400, "你听我解释，监控拍到了真相。"),
    (45200, 47800, "原来一直在背后帮我的人是你。"),
]


def make_source(tmp_path):
    source = tmp_path / "剧A"
    source.mkdir(parents=True, exist_ok=True)
    (source / "第1集.mp4").write_bytes(b"fake-media")
    (source / "第1集.srt").write_text("1\n00:00:01,000 --> 00:00:03,600\n测试\n",
                                     encoding="utf-8")
    return source


def make_project(tmp_path):
    source = make_source(tmp_path)
    discovery = discover(str(source))
    selection = select_sources(discovery, episode="第1集.mp4",
                              confirm_first_episode=True,
                              transcript="第1集.srt", confirm_transcript=True)
    store = FlowStore.create(str(source), discovery, selection,
                             projects_root=tmp_path / "projects")
    store.set_policy(settings.policy())
    return store


def make_snapshot(lines=LINES, duration_ms=49000):
    utterances = []
    for index, (start, end, text) in enumerate(lines, start=1):
        utterances.append({
            "utterance_id": f"u{index:03d}", "start_ms": start, "end_ms": end,
            "text": text, "speaker": f"spk_{'a' if index % 2 else 'b'}",
            "source_asset_id": "asset_video",
            "transcript_asset_id": "asset_sub",
            "provenance": "subtitle", "verification_status": "extracted"})
    return {
        "schema_version": 1, "project_id": "fake", "run_id": RUN_ID,
        "duration_ms": duration_ms, "source_scope": "confirmed_first_episode",
        "assets": [{"asset_id": "asset_video", "kind": "video",
                    "path": "D:/fake/第1集.mp4", "sha256": "0" * 64},
                   {"asset_id": "asset_sub", "kind": "transcript",
                    "path": "D:/fake/第1集.srt", "sha256": "1" * 64}],
        "selection": {}, "cache_key": "cache", "cache_hit": False,
        "evidence": {"schema_version": 1, "utterances": utterances, "issues": []},
    }


def build_story_and_proposals(store, snapshot=None, limit=None):
    snapshot = copy.deepcopy(snapshot or make_snapshot())
    model = build_story(snapshot, config=settings.config())
    result = build_proposals(model, "ev_test", policy=settings.policy(),
                             config=settings.config(), limit=limit)
    if result["proposals"]:
        store.save_proposals(RUN_ID, "ev_test", result["proposals"])
    return snapshot, model, result


def plan_for(store, proposal, snapshot, story):
    from hooktool.flow import approval
    return approval.create_plan(store, proposal["proposal_id"], story, snapshot,
                                policy=store.policy() or settings.policy())


def recall_for(story):
    return recall(story, policy=settings.policy(), config=settings.config())
