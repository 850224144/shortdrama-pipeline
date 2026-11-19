"""Opt-in P0 commands; no implicit global workspace or production side effects."""
from __future__ import annotations

import json
import sqlite3

from .material_strict import discover, select_sources
from .project_store import ProjectStore
from .prepare import prepare_project, verify_run


def _selection(args, discovery):
    return select_sources(
        discovery, episode=args.episode,
        confirm_first_episode=args.confirm_first_episode,
        transcript=args.transcript, confirm_transcript=args.confirm_transcript,
        no_transcript=args.no_transcript)


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def execute(args) -> int:
    try:
        if args.p0_action == "create":
            discovery = discover(args.src)
            selection = _selection(args, discovery)
            store = ProjectStore.create(args.src, discovery, selection)
            _print({"project": str(store.root), **store.project()})
            return 1 if selection["issues"] else 0
        store = ProjectStore(args.project)
        if args.p0_action == "show":
            _print({"project": store.project(), "runs": store.runs()})
            return 0
        if args.p0_action == "confirm":
            project = store.project()
            discovery = discover(project["source_dir"])
            selection = _selection(args, discovery)
            result = store.confirm(discovery, selection, args.expected_revision)
            _print(result)
            return 1 if selection["issues"] else 0
        if args.p0_action == "interrupt":
            if not args.confirm_worker_stopped:
                raise ValueError("须确认旧worker已经停止，才能解除遗留run占用")
            store.interrupt_run(args.run)
            _print({"status": "interrupted", "run_id": args.run})
            return 0
        if args.p0_action == "prepare":
            result = prepare_project(store, allow_asr=args.allow_asr)
            _print(result)
            return 0 if result["status"] == "succeeded" else 1
        result = verify_run(store, args.run)
        _print(result)
        return 0 if result["status"] == "pass" else 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        _print({"status": "failed", "error": str(exc)})
        return 1


def _add_selection(parser):
    parser.add_argument("--episode", help="相对素材目录或绝对首集路径")
    parser.add_argument("--confirm-first-episode", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--transcript", help="首集字幕/转写路径")
    group.add_argument("--no-transcript", action="store_true", help="显式不采用目录字幕")
    parser.add_argument("--confirm-transcript", action="store_true")


def register(subparsers):
    project = subparsers.add_parser("project", help="P0 独立项目管理，不改变旧制作工作区")
    actions = project.add_subparsers(dest="project_action", required=True)
    create = actions.add_parser("create", help="创建隔离项目并检查首集归属")
    create.add_argument("--src", required=True)
    _add_selection(create)
    create.set_defaults(func=execute, p0_action="create")
    show = actions.add_parser("show", help="只读查看项目和运行记录")
    show.add_argument("--project", required=True)
    show.set_defaults(func=execute, p0_action="show")
    confirm = actions.add_parser("confirm", help="显式确认新输入版本；旧run保留历史")
    confirm.add_argument("--project", required=True)
    confirm.add_argument("--expected-revision", type=int, required=True)
    _add_selection(confirm)
    confirm.set_defaults(func=execute, p0_action="confirm")
    interrupt = actions.add_parser("interrupt", help="确认worker已停止后标记遗留任务中断")
    interrupt.add_argument("--project", required=True)
    interrupt.add_argument("--run", required=True)
    interrupt.add_argument("--confirm-worker-stopped", action="store_true")
    interrupt.set_defaults(func=execute, p0_action="interrupt")
    prepare = subparsers.add_parser("prepare", help="P0 仅准备首集证据，不生成分镜或成片")
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--allow-asr", action="store_true", help="明确允许已安装的本地ASR")
    prepare.set_defaults(func=execute, p0_action="prepare")
    verify = subparsers.add_parser("verify", help="只读核验指定run的证据及当前源文件")
    verify.add_argument("--project", required=True)
    verify.add_argument("--run", required=True)
    verify.set_defaults(func=execute, p0_action="verify")
