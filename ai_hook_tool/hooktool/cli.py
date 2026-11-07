# -*- coding: utf-8 -*-
"""CLI 入口：preflight → script → collect → assemble → gate。

示例：
  python -m hooktool.cli preflight --src "E:\\短剧素材\\娇妻婆婆为我挡风雨"
  python -m hooktool.cli script --src "E:\\短剧素材\\娇妻婆婆为我挡风雨"
  python -m hooktool.cli collect
  python -m hooktool.cli assemble
  python -m hooktool.cli gate --src "E:\\短剧素材\\娇妻婆婆为我挡风雨" --similarity 0.6
"""
import argparse
import datetime
import os
import sys

from . import (assemble, collect, config as cfg, gate, genre, manifest as mf,
               material, paths, quotes, script, script_source, transcribe)


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _first_script_txt(texts: list[str]) -> str | None:
    """定位首集剧本 txt：排除简介，优先集号/剧本关键词，兜底第一个非简介文本。"""
    non_brief = [p for p in texts if "简介" not in os.path.basename(p)]
    if not non_brief:
        return None
    for p in non_brief:
        name = os.path.basename(p)
        if "第一集" in name or "第1集" in name or "剧本" in name:
            return p
    return non_brief[0]


def _cmd_preflight(args) -> int:
    paths.ensure_workdir()
    mat = material.scan_material(args.src)
    text = material.read_texts(mat["texts"])
    genre_res = genre.detect_genre(text)

    env = {
        "ffmpeg": cfg.bin_exists(cfg.ffmpeg_bin()),
        "ffprobe": cfg.bin_exists(cfg.ffprobe_bin()),
    }
    report = {
        "drama_name": mat["drama_name"],
        "episode_count": len(mat["episodes"]),
        "first_episode": mat["first_episode"],
        "faces": list(mat["faces"].keys()),
        "cover": mat["cover"],
        "genre": genre_res,
        "env": env,
        "missing": mat["missing"],
        "warnings": mat["warnings"],
    }
    print(f"[preflight] 剧名：{mat['drama_name']}")
    print(f"[preflight] 剧集：{len(mat['episodes'])} 集；首集：{mat['first_episode'][0] if mat['first_episode'] else '无'}")
    print(f"[preflight] 参照脸：{', '.join(mat['faces'].keys()) or '无'}")
    print(f"[preflight] 题材：{genre_res['genre'] or '未命中(PENDING)'}")
    print(f"[preflight] 环境：ffmpeg={env['ffmpeg']} ffprobe={env['ffprobe']}")
    for w in mat["warnings"]:
        print(f"  WARN: {w}")
    for m in mat["missing"]:
        print(f"  MISSING: {m}")
    return 1 if mat["missing"] else 0


def _cmd_script(args) -> int:
    paths.ensure_workdir()
    mat = material.scan_material(args.src)
    if mat["missing"]:
        for m in mat["missing"]:
            print(f"  MISSING: {m}")
        return 1

    lines, pending_reason = transcribe.load_or_transcribe(mat)
    ranked = quotes.rank_quotes(lines, count=40)
    short = [item["short_title"] for item in ranked]
    long = [item["text"] for item in ranked]
    genre_res = genre.detect_genre(material.read_texts(mat["texts"]))

    kw = cfg.keywords()
    txt_entries: list[dict] = []
    script_txt = _first_script_txt(mat["texts"])
    if script_txt:
        entries = script_source.parse_script_txt(script_txt)
        txt_entries = script_source.pick_txt_candidates(
            entries, kw["conflict_words"], kw["suspense_words"], count=40)

    source_ep = mat["first_episode"][1] if mat["first_episode"] else ""
    man = script.build_script(mat, short, long, genre_res["genre"], source_ep,
                              txt_entries, quote_records=ranked,
                              transcript_lines=lines)
    if pending_reason:
        man["warns"].append(f"转写：{pending_reason}（PENDING）")
    if genre_res["pending"]:
        man["warns"].append("题材关键词未命中，待人工定（PENDING）")
    mf.save(man)

    print(f"[script] 分镜已生成：{mf.MANIFEST_PATH}")
    print(f"[script] 前核 {len(mf.segment(man, mf.SEG_PRE)['shots'])} 镜，"
          f"后钩子 {len(mf.segment(man, mf.SEG_HOOK)['shots'])} 镜")
    for w in man["warns"]:
        print(f"  WARN/PENDING: {w}")
    if man.get("hard_block"):
        print("[script] HARD BLOCK: 前核/后钩子不完整；不应生成或拼接占位片段")
        return 1
    print("[script] 前核按 3 秒开场钩子生成；后钩子独立选段，字幕、原剧动作及时间码已写入")
    return 0


def _cmd_collect(_args) -> int:
    paths.ensure_workdir()
    man = mf.load()
    if not man:
        print("  无 manifest，请先跑 script")
        return 1
    res = collect.collect(man)
    man["collect"] = res
    mf.save(man)
    print(f"[collect] 片段 {len(res['clips'])} 段")
    for m in res["missing"]:
        print(f"  MISSING: {m}")
    for w in res["warns"]:
        print(f"  WARN: {w}")
    for p in res["pending"]:
        print(f"  PENDING: {p}")
    if res.get("hard_block"):
        for reason in res["hard_block"]:
            print(f"  HARD BLOCK: {reason}")
    return 1 if res["missing"] or res.get("hard_block") else 0


def _cmd_assemble(args) -> int:
    paths.ensure_workdir()
    man = mf.load()
    if not man:
        print("  无 manifest，请先跑 script/collect")
        return 1
    if man.get("hard_block") or man.get("hook_qc", {}).get("hard_block"):
        reasons = man.get("hard_block") or man.get("hook_qc", {}).get("hard_block", [])
        for reason in reasons:
            print(f"  HARD BLOCK: {reason}")
        print("[assemble] 未达到钩子要求，拒绝拼接")
        return 1
    clips = man.get("clips", [])
    if not clips:
        print("  无已收集片段，请先跑 collect")
        return 1
    segment_shots = {
        seg.get("segment"): {shot.get("shot"): shot
                             for shot in seg.get("shots", [])}
        for seg in man.get("segments", [])
    }
    for clip in clips:
        shot = segment_shots.get(clip.get("segment"), {}).get(clip.get("shot"), {})
        clip["subtitle"] = shot.get("subtitle", "")
        clip["subtitle_source_time"] = shot.get("source_time", {})
    if not any(c.get("segment") == mf.SEG_HOOK for c in clips):
        print("  HARD BLOCK: 后钩子缺少独立视频，禁止以前核片段代替")
        return 1

    out_name = assemble.compose_output_name(
        man["drama_name"], man["episode_label"], _ts())
    out_path = os.path.join(paths.DIR_OUTPUT, out_name)
    res = assemble.run_assemble(clips, out_path, badge_text=args.badge_text)
    man["assemble"] = res
    mf.save(man)

    if res["error"]:
        print(f"  FAIL: {res['error']}")
        return 1
    print(f"[assemble] 成片：{out_path}")
    print(f"[assemble] 时长 {res['duration']}s，分辨率 {res['width']}x{res['height']}，"
          f"音轨 {res['has_audio']}，角标 {res['badge_ok']}")
    for w in res["warns"]:
        print(f"  WARN: {w}")
    return 0


def _cmd_gate(args) -> int:
    paths.ensure_workdir()
    mat = material.scan_material(args.src) if args.src else {"faces": {}, "episodes": []}
    man = mf.load() or {}
    collect_res = man.get("collect", {"clips": [], "missing": [], "warns": [], "pending": []})
    assemble_res = man.get("assemble")

    review = {
        "face_consistent": args.face_consistent,
        "similarity": args.similarity,
        "genre_match": args.genre_match,
        "animal_hook": args.animal_hook,
        "animal_has_main": args.animal_has_main,
    }
    report = gate.evaluate(mat, man, collect_res, assemble_res, review)
    path = gate.save_report(report)

    print(f"[gate] 结论：{report['overall']}（block={report['block']}）")
    for r in report["rules"]:
        print(f"  {r['id']:<6} {r['verdict']:<8} {r['name']} — {r['detail']}")
    for w in report["warns"]:
        print(f"  WARN: {w}")
    for p in report["pending"]:
        print(f"  PENDING: {p}")
    for f in report["fail_reasons"]:
        print(f"  FAIL: {f}")
    for f in report.get("hook_fail_reasons", []):
        print(f"  HOOK FAIL: {f}")
    print(f"[gate] 记录：{path}")
    return 1 if report["block"] or report.get("hook_block") else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hooktool", description="AI 短剧钩子生成工具")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("preflight", help="扫描素材目录并做前置闸检查")
    sp.add_argument("--src", required=True, help="素材目录路径")
    sp.set_defaults(func=_cmd_preflight)

    ss = sub.add_parser("script", help="生成两段分镜 manifest")
    ss.add_argument("--src", required=True, help="素材目录路径")
    ss.set_defaults(func=_cmd_script)

    sc = sub.add_parser("collect", help="收集 AI 生成片段并校验槽位")
    sc.set_defaults(func=_cmd_collect)

    sa = sub.add_parser("assemble", help="拼接成片并压 AI 标注角标")
    sa.add_argument("--badge-text", default=None, help="覆盖默认角标文案")
    sa.set_defaults(func=_cmd_assemble)

    sg = sub.add_parser("gate", help="三态合规闸")
    sg.add_argument("--src", default=None, help="素材目录路径")
    sg.add_argument("--face-consistent", dest="face_consistent", choices=["true", "false", None], default=None)
    sg.add_argument("--similarity", type=float, default=None)
    sg.add_argument("--genre-match", dest="genre_match", choices=["true", "false", None], default=None)
    sg.add_argument("--animal-hook", dest="animal_hook", action="store_true")
    sg.add_argument("--animal-has-main", dest="animal_has_main", action="store_true")
    sg.set_defaults(func=_cmd_gate)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # 归一化三态布尔参数
    for k in ("face_consistent", "genre_match"):
        v = getattr(args, k, None)
        if v == "true":
            setattr(args, k, True)
        elif v == "false":
            setattr(args, k, False)
        else:
            setattr(args, k, None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
