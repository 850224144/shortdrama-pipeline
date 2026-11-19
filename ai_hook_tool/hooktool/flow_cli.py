"""P0–P4 完整流程 CLI（显式新入口，不改旧 `hooktool.cli` 契约）。

示例：
  python -m hooktool.flow_cli project --src "E:\\短剧素材\\某剧"
  python -m hooktool.flow_cli analyze --project "…\\projects\\<id>" --allow-asr
  python -m hooktool.flow_cli propose --project …
  python -m hooktool.flow_cli preview --project … --plan plan_xxx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .flow import errors, service


def _json(parser, name: str, help_text: str):
    parser.add_argument(name, help=help_text)


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _commands() -> dict:
    return {
        "project": lambda a: service.create_project(
            a.src, episode=a.episode,
            confirm_first_episode=a.confirm_first_episode,
            transcript=a.transcript, confirm_transcript=a.confirm_transcript,
            no_transcript=a.no_transcript,
            policy=_load(a.policy) if a.policy else None),
        "show": lambda a: service.project_state(a.project),
        "policy": lambda a: service.update_policy(a.project, _load(a.file)),
        "confirm": lambda a: service.confirm_episode(
            a.project, expected_revision=a.expected_revision, episode=a.episode,
            transcript=a.transcript, confirm_transcript=a.confirm_transcript,
            no_transcript=a.no_transcript,
            policy=_load(a.policy) if a.policy else None),
        "analyze": lambda a: service.analyze_episode(a.project, allow_asr=a.allow_asr),
        "evidence": lambda a: service.evidence(a.project, page=a.page, size=a.size),
        "story": lambda a: service.story(a.project),
        "propose": lambda a: service.propose_hooks(a.project,
                                                   evidence_revision_id=a.evidence,
                                                   limit=a.limit),
        "proposals": lambda a: service.list_proposals(a.project),
        "import-root": lambda a: service.add_import_root(a.project, a.dir),
        "plan-revise": lambda a: service.revise_plan(
            a.project, a.plan, _load(a.edits_file),
            expected_revision=a.expected_revision),
        "plan-review": lambda a: service.review_plan(
            a.project, a.plan, a.decision, reviewer=a.reviewer, notes=a.notes or "",
            approved_scope=a.scope, facts_reviewed=a.facts_reviewed,
            reason_code=a.reason_code),
        "plan-show": lambda a: service.plan_detail(a.project, a.plan),
        "preview": lambda a: service.render_preview(a.project, a.plan),
        "generation": lambda a: service.create_generation_tasks(a.project, a.plan),
        "import": lambda a: service.import_generated_asset(
            a.project, a.task, a.file,
            {"provider": a.provider, "attempt_reason": a.reason} if (a.provider or a.reason) else None),
        "asset-review": lambda a: service.review_generated_asset(
            a.project, a.asset, a.decision, _load(a.ranges), _load(a.checks),
            reviewer=a.reviewer),
        "assemble": lambda a: service.assemble_timeline(
            a.project, a.plan, _load(a.selections) if a.selections else None),
        "render": lambda a: service.render_timeline(a.project, a.timeline),
        "qc": lambda a: service.run_quality_checks(a.project, a.timeline),
        "final-review": lambda a: service.review_final(
            a.project, a.timeline, a.decision, reviewer=a.reviewer,
            notes=a.notes or "", qc_id=a.qc),
        "permissions": lambda a: service.permissions(a.project, a.timeline),
        "export": lambda a: service.export_delivery(a.project, a.timeline),
        "job": lambda a: service.job_status(a.project, a.job_id),
        "jobs": lambda a: service.list_jobs(a.project),
        "cancel": lambda a: service.cancel_job(a.project, a.job_id),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hooktool.flow", description="AI 钩子视频完整流程（P0–P4）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("project", help="创建隔离项目并检查首集归属")
    p.add_argument("--src", required=True)
    p.add_argument("--episode")
    p.add_argument("--confirm-first-episode", action="store_true")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--transcript")
    group.add_argument("--no-transcript", action="store_true")
    p.add_argument("--confirm-transcript", action="store_true")
    p.add_argument("--policy", help="JSON 文件：覆盖交付策略")

    p = sub.add_parser("show", help="项目当前阶段、方案、审批与失效记录")
    p.add_argument("--project", required=True)

    p = sub.add_parser("policy", help="更新交付策略（音频权限、时长范围等）")
    p.add_argument("--project", required=True)
    p.add_argument("--file", required=True, help="JSON 文件：要覆盖的策略字段")

    p = sub.add_parser("confirm", help="确认新的首集输入版本")
    p.add_argument("--project", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--episode")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--transcript")
    group.add_argument("--no-transcript", action="store_true")
    p.add_argument("--confirm-transcript", action="store_true")
    p.add_argument("--policy", help="JSON 文件：覆盖交付策略")

    p = sub.add_parser("analyze", help="分析首集：证据 + 人物 + 事件 + 双时间线")
    p.add_argument("--project", required=True)
    p.add_argument("--allow-asr", action="store_true")

    p = sub.add_parser("evidence", help="分页读取证据")
    p.add_argument("--project", required=True)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--size", type=int, default=50)

    p = sub.add_parser("story", help="读取人物、事件与待核对项")
    p.add_argument("--project", required=True)

    p = sub.add_parser("propose", help="召回候选并生成最多 N 个差异化方案")
    p.add_argument("--project", required=True)
    p.add_argument("--evidence")
    p.add_argument("--limit", type=int)

    p = sub.add_parser("proposals", help="读取方案与最新版本")
    p.add_argument("--project", required=True)

    p = sub.add_parser("import-root", help="批准一个生成片段的导入目录")
    p.add_argument("--project", required=True)
    p.add_argument("--dir", required=True)

    p = sub.add_parser("plan-revise", help="编辑方案生成新版本（旧审批失效）")
    p.add_argument("--project", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--edits-file", required=True)

    p = sub.add_parser("plan-review", help="批准/退回指定版本")
    p.add_argument("--project", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--decision", choices=("approved", "rejected"), required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--notes")
    p.add_argument("--scope", default="full")
    p.add_argument("--facts-reviewed", action="store_true")
    p.add_argument("--reason-code")

    p = sub.add_parser("plan-show", help="读取方案版本与审批历史")
    p.add_argument("--project", required=True)
    p.add_argument("--plan", required=True)

    p = sub.add_parser("preview", help="生成内部预演（非正式交付）")
    p.add_argument("--project", required=True)
    p.add_argument("--plan", required=True)

    p = sub.add_parser("generation", help="按批准方案创建逐镜制作任务")
    p.add_argument("--project", required=True)
    p.add_argument("--plan", required=True)

    p = sub.add_parser("import", help="导入人工生成片段")
    p.add_argument("--project", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--provider")
    p.add_argument("--reason")

    p = sub.add_parser("asset-review", help="逐镜验收：可用区间与身份/动作/对白")
    p.add_argument("--project", required=True)
    p.add_argument("--asset", required=True)
    p.add_argument("--decision", choices=("approved", "rejected"), required=True)
    p.add_argument("--ranges", required=True, help="JSON 文件：[{start_ms,end_ms}]")
    p.add_argument("--checks", required=True,
                   help="JSON 文件：identity/action/dialogue/usable_footage")
    p.add_argument("--reviewer")

    p = sub.add_parser("assemble", help="按批准方案与验收片段创建时间线")
    p.add_argument("--project", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--selections", help="JSON 文件：{shot_id:{start_ms,end_ms}}")

    p = sub.add_parser("render", help="渲染时间线")
    p.add_argument("--project", required=True)
    p.add_argument("--timeline", required=True)

    p = sub.add_parser("qc", help="四层质检")
    p.add_argument("--project", required=True)
    p.add_argument("--timeline", required=True)

    p = sub.add_parser("final-review", help="最终完整观看后的验收")
    p.add_argument("--project", required=True)
    p.add_argument("--timeline", required=True)
    p.add_argument("--decision", choices=("approved", "rejected"), required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--notes")
    p.add_argument("--qc")

    p = sub.add_parser("permissions", help="读取 preview/production/delivery 三个许可")
    p.add_argument("--project", required=True)
    p.add_argument("--timeline", required=True)

    p = sub.add_parser("export", help="正式导出（不发布）")
    p.add_argument("--project", required=True)
    p.add_argument("--timeline", required=True)

    p = sub.add_parser("job", help="查询作业状态")
    p.add_argument("--project", required=True)
    p.add_argument("--job-id", required=True)

    p = sub.add_parser("jobs", help="列出项目作业")
    p.add_argument("--project", required=True)

    p = sub.add_parser("cancel", help="协作式取消作业")
    p.add_argument("--project", required=True)
    p.add_argument("--job-id", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = _commands()
    try:
        result = handlers[args.command](args)
    except errors.FlowError as exc:
        _print(exc.to_dict())
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _print({"status": "failed", "code": "CLI_INPUT_ERROR", "message": str(exc)})
        return 1
    _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
