# -*- coding: utf-8 -*-
"""两段分镜生成：前核15s + 后钩子15s。

骨架来自首集台词/人物，不新编；烈度在原片内放大，悬念前置（定稿 §4.2）。
台词来源优先级：成片 ASR ＞ 剧本 txt 候选（txt 仅候选，不进相同度判定，定稿 §4.3）。
脚本只产分镜骨架与提示词草稿；锁脸出图/图生视频在豆包网页版人工完成。
"""
from . import config as cfg, manifest as mf


def _pick_roles(faces: dict[str, str]) -> list[str]:
    return list(faces.keys())


def _emotion_for(text: str, conflict_words: list[str], suspense_words: list[str]) -> str:
    if any(w in text for w in conflict_words):
        return "愤怒/对峙"
    if any(w in text for w in suspense_words):
        return "震惊/悬念"
    return "情绪递进"


def _strip_punct(line: str, punct: list[str], max_len: int = 16) -> str:
    s = line or ""
    for p in punct:
        s = s.replace(p, "")
    return s.strip()[:max_len]


def _merge_quotes(asr_long: list[str], asr_short: list[str],
                  txt_entries: list[dict]) -> tuple[list[dict], list[str]]:
    """合并台词池：ASR 优先，txt 候选去重补充。

    返回 (dialogues[{line, role, source, vo}], subtitles[short])。
    """
    kw = cfg.keywords()
    dialogues: list[dict] = []
    subtitles: list[str] = []
    seen: set[str] = set()

    def push(line: str, role: str, source: str, vo: bool):
        line = (line or "").strip()
        if not line or line in seen:
            return
        seen.add(line)
        dialogues.append({"line": line, "role": role, "source": source, "vo": vo})
        subtitles.append(_strip_punct(line, kw["punctuations"]))

    for i, line in enumerate(asr_long):
        push(line, "", "asr", False)
    for entry in txt_entries:
        push(entry["line"], entry["role"], "txt", entry.get("vo", False))

    # short 兜底对齐：ASR short 优先，缺的用台词池截短补
    merged_short = list(asr_short)
    for d in dialogues:
        short = _strip_punct(d["line"], kw["punctuations"])
        if short and short not in merged_short:
            merged_short.append(short)
    return dialogues, merged_short


def _shot(shot_no: int, role: str, ref_image: str, dialogue: dict | None,
          subtitle: str, emotion: str, scene_hint: str) -> dict:
    line = dialogue["line"] if dialogue else ""
    prompt = (
        f"9:16 竖屏，{scene_hint}；{role or '原剧人物'}（以参照图锁脸，保持发型/服装一致）；"
        f"情绪：{emotion}；台词氛围：{line or '无台词'}；近景/中景，光线自然，写实短剧质感。"
    )
    return {
        "shot": shot_no,
        "role": role,
        "ref_image": ref_image,
        "prompt": prompt,
        "dialogue": line,
        "dialogue_source": dialogue["source"] if dialogue else "",
        "vo": dialogue["vo"] if dialogue else False,
        "subtitle": subtitle,
        "emotion": emotion,
    }


def build_script(material: dict, short_titles: list[str], long_titles: list[str],
                 genre: str, source_episode: str,
                 txt_entries: list[dict] | None = None) -> dict:
    kw = cfg.keywords()
    faces = material.get("faces", {})
    roles = _pick_roles(faces)
    warns: list[str] = []
    txt_entries = txt_entries or []

    if not roles:
        warns.append("无定妆/剧照参照，角色锁脸待补（PENDING）")

    dialogues, subtitles = _merge_quotes(long_titles, short_titles, txt_entries)
    if any(d["source"] == "txt" for d in dialogues):
        warns.append("部分台词来自剧本 txt 候选，需人工对照成片复核（不进相同度判定）")

    manifest = mf.empty_manifest(material["drama_name"], genre,
                                 cfg.get_setting("episode_label", "E01"), source_episode)
    seg_pre = mf.segment(manifest, mf.SEG_PRE)
    seg_hook = mf.segment(manifest, mf.SEG_HOOK)

    def role_at(i: int) -> tuple[str, str]:
        if roles:
            r = roles[i % len(roles)]
            return r, faces[r]
        return "", ""

    def role_for(dialogue: dict | None, i: int) -> tuple[str, str]:
        # txt 候选带角色且能匹配定妆脸时对齐；ASR 无说话人信息，按轮值
        if dialogue and dialogue["role"] and dialogue["role"] in faces:
            return dialogue["role"], faces[dialogue["role"]]
        return role_at(i)

    scene_hint = "原剧对应场景"

    def fill(seg: dict, start: int, count: int, label: str):
        for i in range(count):
            idx = start + i
            dialogue = dialogues[idx] if idx < len(dialogues) else None
            role, ref = role_for(dialogue, i)
            subtitle = subtitles[idx] if idx < len(subtitles) else ""
            emotion = _emotion_for(dialogue["line"] if dialogue else "",
                                   kw["conflict_words"], kw["suspense_words"])
            seg["shots"].append(_shot(i + 1, role, ref, dialogue, subtitle,
                                      emotion, scene_hint))
        if start + count > len(dialogues):
            warns.append(f"{label}台词候选不足，部分镜头用占位，需人工在豆包对话补全")

    # 前核 15s：首镜即冲突/悬念；后钩子 15s：承未解冲突，悬念收尾
    fill(seg_pre, 0, 3, "前核段")
    fill(seg_hook, 3, 2, "后钩子段")

    manifest["refs"] = faces
    manifest["status"] = "scripted"
    manifest["warns"] = warns
    return manifest
