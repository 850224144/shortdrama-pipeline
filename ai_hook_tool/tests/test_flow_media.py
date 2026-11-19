# -*- coding: utf-8 -*-
"""真实 FFmpeg 合成素材端到端：渲染、四层质检与导出。

没有 ffmpeg 时跳过，不伪造成通过。
"""
import subprocess
from pathlib import Path

import pytest

from hooktool import config as cfg
from hooktool.flow import media, service


def _make_media(root: Path) -> Path:
    video = root / "第1集.mp4"
    subprocess.run(
        [cfg.ffmpeg_bin(), "-y", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=420", "-t", "48",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(video)], check=True, capture_output=True)
    cues = [(1000, 3600, "这份合同你今天必须签。"),
            (5000, 7600, "我要是不签，你又能拿我怎样？"),
            (9200, 11800, "公司已经被我抵押了。"),
            (13200, 15800, "什么？你竟然背着我做这种事！"),
            (17600, 20200, "所以你现在只能签字。"),
            (21800, 24400, "我要是不签呢？"),
            (26200, 28800, "那你就等着法院的传票吧。"),
            (30200, 32800, "等等，我好像误会你了。"),
            (34400, 37000, "监控拍到了真相，是她在背后帮我。"),
            (38200, 40800, "原来一直帮我的人是你。"),
            (41800, 44400, "你听我解释。"),
            (45200, 47800, "原来是你一直在帮我。")]
    lines = []
    for index, (start, end, text) in enumerate(cues, start=1):
        def ts(value):
            return (f"{value // 3600000:02d}:{(value // 60000) % 60:02d}:"
                    f"{(value // 1000) % 60:02d},{value % 1000:03d}")
        lines.append(f"{index}\n{ts(start)} --> {ts(end)}\n{text}\n")
    (root / "第1集.srt").write_text("\n".join(lines), encoding="utf-8")
    return video


@pytest.mark.skipif(not media.ffmpeg_available(), reason="FFmpeg 不可用")
def test_full_flow_renders_and_exports(tmp_path):
    source = tmp_path / "剧A"
    source.mkdir()
    video = _make_media(source)

    created = service.create_project(
        str(source), episode="第1集.mp4", confirm_first_episode=True,
        transcript="第1集.srt", confirm_transcript=True,
        projects_root=str(tmp_path / "projects"))
    project = created["project_dir"]
    service.update_policy(project, {"audio_permission": "confirmed"})
    service.analyze_episode(project, allow_asr=False)
    proposals = service.propose_hooks(project)
    assert proposals["plans"], proposals
    plan = next(p for p in proposals["plans"] if not p["blocked"])
    service.review_plan(project, plan["plan_revision_id"], "approved",
                        reviewer="qa", facts_reviewed=True)
    tasks = service.create_generation_tasks(project, plan["plan_revision_id"])
    service.add_import_root(project, str(source))
    for task in tasks["tasks"]:
        shot = next(s for s in plan["payload"]["shots"]
                    if s["shot_id"] == task["shot_id"])
        asset = service.import_generated_asset(project, task["task_id"], str(video),
                                              {"provider": "manual"})
        service.review_generated_asset(
            project, asset["asset"]["asset_id"], "approved",
            [{"start_ms": 0, "end_ms": shot["planned_ms"]}],
            {"identity": "pass", "action": "pass", "dialogue": "pass",
             "usable_footage": "pass"}, reviewer="qa")
    timeline = service.assemble_timeline(project, plan["plan_revision_id"])
    render = service.render_timeline(project, timeline["timeline_revision_id"])
    assert Path(render["render"]["output"]).is_file()
    assert render["render"]["audio_embedded"] is True
    qc = service.run_quality_checks(project, timeline["timeline_revision_id"])
    assert qc["qc"]["layers"]["source"] == "PASS"
    assert qc["qc"]["delivery_ready"] is True, qc["qc"]["blocking_issues"]
    service.review_final(project, timeline["timeline_revision_id"], "approved",
                         reviewer="qa")
    export = service.export_delivery(project, timeline["timeline_revision_id"])
    assert Path(export["delivery"]["file"]).is_file()
    assert export["delivery"]["publication"] == "disabled"
