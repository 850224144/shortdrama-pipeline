#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
短剧混剪通用渲染脚本 v1.0

读取 EDL 配置 JSON，自动完成：
  验证 → 切片段 → 拼接 → 抗查重 → ffprobe 核验 → 清理临时文件 → 报告

用法:
  python render_generic.py <config.json>
  python render_generic.py <config.json> --validate   # 只做预检查，不渲染
"""

import json
import os
import sys
import subprocess
import shutil
import time

# ===== 路径配置 =====
FFMPEG = r"D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"
FFPROBE = r"D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffprobe.exe"

# ===== 默认抗查重参数 =====
DEFAULT_ANTI_DUP = {
    "speed": 1.05,
    "crop": 30,
    "contrast": 1.05,
    "saturation": 1.1,
    "brightness": 0.02,
    "noise": 8,
    "rotate": 0.008,
    "fps": None,
    "atempo": 1.05,
    "eq_h": {"f": 3000, "width": 1000, "g": 3},
    "eq_l": {"f": 100, "width": 50, "g": -2},
    "watermark_text": "福桃说剧",
    "watermark_alpha": 0.3,
    "watermark_fontsize": 36,
    "watermark_font": r"C:\Windows\Fonts\msyh.ttc",
}

DEFAULT_SETTINGS = {
    "crf": 22,
    "preset": "veryfast",
    "anti_dup": DEFAULT_ANTI_DUP,
}


# ===== 日志 =====
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _safe_remove(path):
    """尽力删除文件；清理路径自身出错不能炸掉主流程"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def sweep_stale_tmp(out_dir):
    """清扫历史运行残留的 .tmp 半成品（进程被强杀时 finally 没执行到的）。

    命名固定为 <交付名>.mp4.tmp：对所有 endswith('.mp4') 的收集/去重/上传
    glob 不可见，因此残留也无害，清扫只是保持交付目录整洁。
    """
    n = 0
    try:
        for f in os.listdir(out_dir):
            if f.endswith(".mp4.tmp"):
                _safe_remove(os.path.join(out_dir, f))
                n += 1
    except OSError:
        pass
    return n


# ===== ffprobe 工具 =====
def probe_duration(filepath):
    cmd = [FFPROBE, "-v", "error",
           "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1",
           filepath]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:200]}")
    return float(r.stdout.strip())


def probe_info(filepath):
    cmd = [FFPROBE, "-v", "error", "-show_streams", "-of", "json", filepath]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


# ===== 预检查 =====
def validate_source(path, start, duration):
    if not os.path.exists(path):
        raise FileNotFoundError(f"源文件不存在: {path}")
    total = probe_duration(path)
    if start < 0:
        raise ValueError(f"起始时间不能为负: {start}")
    if start + duration > total:
        raise ValueError(
            f"时间码 [{start:.1f}s - {start+duration:.1f}s] "
            f"超出源文件时长 {total:.1f}s ({os.path.basename(path)})"
        )
    return total


def check_coverage(config):
    eps = set()
    for v in config["videos"]:
        for s in v["segments"]:
            eps.add(s["episode"])
    return sorted(eps)


def check_overlap(config):
    """检查视频间片段重叠"""
    overlaps = []
    segs = []
    for v in config["videos"]:
        for s in v["segments"]:
            segs.append({
                "vid": v["id"],
                "ep": s["episode"],
                "s": s["start"],
                "e": s["start"] + s["duration"],
            })
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            a, b = segs[i], segs[j]
            if a["vid"] == b["vid"]:
                continue
            if a["ep"] == b["ep"]:
                os_ = max(a["s"], b["s"])
                oe = min(a["e"], b["e"])
                if os_ < oe:
                    overlaps.append({
                        "a": a, "b": b,
                        "overlap_start": os_,
                        "overlap_end": oe,
                        "overlap_sec": oe - os_,
                    })
    return overlaps


def check_zero_start(segs):
    """检查是否有从 0 秒开始切的片段"""
    warnings = []
    for s in segs:
        if s["start"] == 0:
            warnings.append(f"Ep{s['episode']}: 从 0 秒开始切，建议偏移 5-15 秒")
    return warnings


# ===== 滤镜构建 =====
def build_vf(ad):
    wm_text = ad.get("watermark_text", "福桃说剧")
    wm_alpha = ad.get("watermark_alpha", 0.3)
    wm_size = ad.get("watermark_fontsize", 36)
    wm_font = ad.get("watermark_font", r"C:\Windows\Fonts\msyh.ttc")

    wm_base = f"drawtext=fontfile='{wm_font}':text='{wm_text}':fontsize={wm_size}:fontcolor=white@{wm_alpha}"

    wm_filters = [
        f"{wm_base}:x=30:y=30:enable='lt(mod(t\\,12)\\,3)'",
        f"{wm_base}:x=w-tw-30:y=30:enable='between(mod(t\\,12)\\,3\\,6)'",
        f"{wm_base}:x=w-tw-30:y=h-th-30:enable='between(mod(t\\,12)\\,6\\,9)'",
        f"{wm_base}:x=30:y=h-th-30:enable='gte(mod(t\\,12)\\,9)'",
    ]

    parts = [
        f"setpts=PTS/{ad['speed']}",
        f"crop=1080:1860:0:{ad['crop']}",
        f"pad=1080:1920:0:{ad['crop']}:color=black",
        f"eq=contrast={ad['contrast']}:saturation={ad['saturation']}:brightness={ad['brightness']}",
        f"noise=alls={ad['noise']}:allf=t",
        f"rotate={ad['rotate']}:fillcolor=black",
    ]
    # 帧率：源文件60fps保持60fps，30fps保持30fps，不强制转换
    if ad.get('fps') is not None:
        parts.append(f"fps={ad['fps']}")
    parts.extend(wm_filters)

    return ",".join(parts)


def build_af(ad):
    return (
        f"atempo={ad['atempo']},"
        f"equalizer=f={ad['eq_h']['f']}:width={ad['eq_h']['width']}:t=h:g={ad['eq_h']['g']},"
        f"equalizer=f={ad['eq_l']['f']}:width={ad['eq_l']['width']}:t=h:g={ad['eq_l']['g']}"
    )


def concat_filter(n):
    streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    return f"{streams}concat=n={n}:v=1:a=1[v][a]"


# ===== 渲染步骤 =====
def render_segment(source, start, duration, output, crf, preset):
    cmd = [
        FFMPEG, "-y",
        "-ss", str(start), "-i", source,
        "-t", str(duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-c:a", "aac", "-b:a", "160k",
        output,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"片段渲染失败: {r.stderr.strip()[-300:]}")


def concat_segments(segment_files, output, crf, preset):
    """使用concat demuxer拼接，避免concat filter在60fps源文件上的-22错误"""
    n = len(segment_files)
    # 生成文件清单
    concat_list = output + ".concat.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for sf in segment_files:
            f.write(f"file '{sf.replace(chr(39), chr(39)*2)}'\n")
    # 使用concat demuxer + re-encode确保兼容性
    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-c:a", "aac", "-b:a", "160k",
        output,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    # 清理清单文件
    if os.path.exists(concat_list):
        os.remove(concat_list)
    if r.returncode != 0:
        # fallback: 使用concat filter
        cmd2 = [FFMPEG, "-y"]
        for sf in segment_files:
            cmd2.extend(["-i", sf])
        cmd2.extend([
            "-filter_complex", concat_filter(n),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
            "-c:a", "aac", "-b:a", "160k",
            output,
        ])
        r = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"拼接失败: {r.stderr.strip()[-300:]}")


def apply_anti_dup(raw, output, ad, crf, preset):
    # 注意：output 可能是 .mp4.tmp 临时名，ffmpeg 靠扩展名猜封装格式会失败，
    # 必须显式 -f mp4（正式名场景下显式指定也无害）。
    cmd = [
        FFMPEG, "-y",
        "-i", raw,
        "-vf", build_vf(ad),
        "-af", build_af(ad),
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        "-f", "mp4",
        output,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"抗查重失败: {r.stderr.strip()[-300:]}")


def verify_output(filepath):
    """ffprobe 核验，返回 (ok, detail)"""
    if not os.path.exists(filepath):
        return False, "文件不存在"
    size = os.path.getsize(filepath)
    if size < 1_000_000:
        return False, f"文件过小: {size:,} bytes"
    try:
        info = probe_info(filepath)
        streams = info.get("streams", [])
        has_v = any(s.get("codec_type") == "video" for s in streams)
        has_a = any(s.get("codec_type") == "audio" for s in streams)
        if not has_v:
            return False, "无视频流"
        if not has_a:
            return False, "无音频流"
        duration = probe_duration(filepath)
        if duration < 60:
            return False, f"时长过短: {duration:.1f}s"
        v_stream = next(s for s in streams if s.get("codec_type") == "video")
        w, h = v_stream.get("width", 0), v_stream.get("height", 0)
        if w != 1080 or h != 1920:
            return False, f"分辨率错误: {w}x{h}"
        return True, f"{duration:.1f}s, {size:,}B, {w}x{h}"
    except Exception as e:
        return False, str(e)


# ===== 单条视频处理 =====
def process_video(video, config, tmp_base):
    vid = video["id"]
    kw = video["hook_keyword"]
    segs = video["segments"]

    settings = config.get("settings", DEFAULT_SETTINGS)
    crf = settings.get("crf", 22)
    preset = settings.get("preset", "veryfast")
    ad = settings.get("anti_dup", DEFAULT_ANTI_DUP)

    src_dir = config["source_dir"]
    src_pat = config.get("source_pattern", "{name} - 第{ep}集.mp4")
    out_dir = config["output_dir"]
    drama = config["drama_name"]
    drama_safe = drama.replace('，', '_').replace(',', '_').replace(' ', '_')

    final_path = os.path.join(out_dir, f"{drama_safe}_{vid}_{kw}_去重版.mp4")
    # P0-2：交付名绝不直接当 ffmpeg 输出。先写 .tmp（对 endswith('.mp4') 的
    # 去重闸门/收集/报告/上传 glob 全部不可见），核验通过才 os.replace 落正式名。
    tmp_path = final_path + ".tmp"
    vtmp = os.path.join(tmp_base, f"v{vid}")
    os.makedirs(vtmp, exist_ok=True)

    log(f"\n{'='*50}")
    log(f"🎬 视频 {vid}: {kw}")
    log(f"{'='*50}")

    # 1. 验证
    log(f"  🔍 验证 {len(segs)} 个片段...")
    for i, seg in enumerate(segs):
        ep = seg["episode"]
        src = os.path.join(src_dir, src_pat.format(name=drama, ep=ep))
        try:
            total = validate_source(src, seg["start"], seg["duration"])
            log(f"    [{i+1}] Ep{ep} [{seg['start']:.1f}s-{seg['start']+seg['duration']:.1f}s] / {total:.1f}s ✓")
        except Exception as e:
            return {"id": vid, "kw": kw, "status": "FAILED", "detail": f"验证失败: {e}"}

    seg_files = []
    try:
        # 2. 渲染片段
        log(f"  🎬 渲染片段...")
        for i, seg in enumerate(segs):
            ep = seg["episode"]
            src = os.path.join(src_dir, src_pat.format(name=drama, ep=ep))
            out = os.path.join(vtmp, f"seg_{i:02d}.mp4")
            t0 = time.time()
            render_segment(src, seg["start"], seg["duration"], out, crf, preset)
            seg_files.append(out)
            log(f"    [{i+1}/{len(segs)}] Ep{ep} [{seg['start']:.1f}s-{seg['start']+seg['duration']:.1f}s] ✓ ({time.time()-t0:.1f}s)")

        # 3. 拼接
        raw = os.path.join(vtmp, f"v{vid}_raw.mp4")
        t0 = time.time()
        concat_segments(seg_files, raw, crf, preset)
        log(f"  🔗 拼接完成 ({time.time()-t0:.1f}s)")

        # 4+5. 抗查重 + 核验 —— 三段式原子落盘（P0-2）。
        #    原实现 ffmpeg 直接写 *_去重版.mp4：编码失败或核验不过都会把
        #    0 字节~400MB 的残缺文件留在交付目录，manual_cut 数 _去重版.mp4
        #    误判"已切完"永久跳过重切，收集/上传还会把坏文件当成品。
        t0 = time.time()
        apply_anti_dup(raw, tmp_path, ad, crf, preset)
        log(f"  🛡️  抗查重完成 ({time.time()-t0:.1f}s)")

        log(f"  ✅ 核验...")
        ok, msg = verify_output(tmp_path)
        if ok:
            os.replace(tmp_path, final_path)  # 同卷原子替换
            log(f"  ✅ 通过: {msg}")
            return {"id": vid, "kw": kw, "status": "OK", "detail": msg, "path": final_path}
        else:
            log(f"  ❌ 核验失败: {msg}")
            _safe_remove(tmp_path)
            return {"id": vid, "kw": kw, "status": "FAILED", "detail": msg}

    except Exception as e:
        _safe_remove(tmp_path)
        return {"id": vid, "kw": kw, "status": "FAILED", "detail": str(e)}
    finally:
        # 6. 清理临时文件
        if os.path.isdir(vtmp):
            shutil.rmtree(vtmp)
        _safe_remove(tmp_path)  # 兜底：成功路径 tmp 已被 replace 走，这里通常无事可做
        log(f"  🧹 临时文件已清理")


# ===== 主入口 =====
def main():
    if len(sys.argv) < 2:
        print("用法: python render_generic.py <config.json>")
        print("      python render_generic.py <config.json> --validate")
        sys.exit(1)

    cfg_path = sys.argv[1]
    validate_only = "--validate" in sys.argv

    with open(cfg_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    out_dir = config["output_dir"]
    tmp_base = os.path.join(out_dir, "_tmp_raw")
    os.makedirs(out_dir, exist_ok=True)
    if not validate_only:
        os.makedirs(tmp_base, exist_ok=True)

    log(f"📋 配置: {cfg_path}")
    log(f"🎭 剧名: {config['drama_name']}")
    log(f"🎬 视频数: {len(config['videos'])}")
    log(f"📁 素材: {config['source_dir']}")
    log(f"📁 输出: {out_dir}")
    if validate_only:
        log(f"🔍 模式: 仅预检查")

    # ===== 预检查 =====
    eps = check_coverage(config)
    ep_str = ",Ep".join(str(e) for e in eps)
    log(f"\n📊 覆盖 {len(eps)} 集: Ep{ep_str}")

    overlaps = check_overlap(config)
    if overlaps:
        log(f"⚠️  {len(overlaps)} 处跨视频重叠:")
        for o in overlaps:
            log(f"   V{o['a']['vid']}/Ep{o['a']['ep']} ↔ V{o['b']['vid']}/Ep{o['b']['ep']}: 重叠 {o['overlap_sec']:.1f}s")
    else:
        log(f"✅ 无跨视频重叠")

    all_segs = [s for v in config["videos"] for s in v["segments"]]
    zero_warns = check_zero_start(all_segs)
    if zero_warns:
        log(f"⚠️  起始偏移: {'; '.join(zero_warns)}")

    # 验证源文件
    src_pat = config.get("source_pattern", "{name} - 第{ep}集.mp4")
    src_dir = config["source_dir"]
    drama = config["drama_name"]
    # 与 process_video 中的规则保持一致（报告里的文件名要能对上实际交付文件）
    drama_safe = drama.replace('，', '_').replace(',', '_').replace(' ', '_')
    src_errors = []
    checked = set()
    for s in all_segs:
        ep = s["episode"]
        src = os.path.join(src_dir, src_pat.format(name=drama, ep=ep))
        if src in checked:
            continue
        checked.add(src)
        if not os.path.exists(src):
            src_errors.append(src)
    if src_errors:
        log(f"❌ 源文件缺失 {len(src_errors)} 个:")
        for e in src_errors[:5]:
            log(f"   {e}")
        if len(src_errors) > 5:
            log(f"   ... 还有 {len(src_errors)-5} 个")
    else:
        log(f"✅ 所有源文件存在 ({len(checked)} 个)")

    # 验证时间码
    time_errors = []
    for v in config["videos"]:
        for seg in v["segments"]:
            ep = seg["episode"]
            src = os.path.join(src_dir, src_pat.format(name=drama, ep=ep))
            if not os.path.exists(src):
                continue
            try:
                total = probe_duration(src)
                if seg["start"] + seg["duration"] > total:
                    time_errors.append(
                        f"V{v['id']}/Ep{ep}: [{seg['start']:.1f}s-{seg['start']+seg['duration']:.1f}s] > {total:.1f}s"
                    )
            except:
                pass
    if time_errors:
        log(f"❌ 时间码错误 {len(time_errors)} 处:")
        for e in time_errors[:5]:
            log(f"   {e}")
    else:
        log(f"✅ 所有时间码有效")

    if validate_only:
        log(f"\n🔍 预检查完成")
        has_errors = bool(src_errors or time_errors)
        return 1 if has_errors else 0

    # ===== 渲染 =====
    results = []
    t_start = time.time()

    for v in config["videos"]:
        r = process_video(v, config, tmp_base)
        results.append(r)

    # 清理临时目录
    if os.path.isdir(tmp_base):
        shutil.rmtree(tmp_base)

    # P0-2：清扫历史运行残留的 .tmp 半成品（上次进程被强杀时 finally 没跑到）
    swept = sweep_stale_tmp(out_dir)
    if swept:
        log(f"🧹 已清扫 {swept} 个历史残留的 .tmp 半成品")

    # ===== 报告 =====
    elapsed = time.time() - t_start
    ok = sum(1 for r in results if r["status"] == "OK")
    fail = sum(1 for r in results if r["status"] == "FAILED")

    log(f"\n{'='*50}")
    log(f"📊 处理完成")
    log(f"{'='*50}")
    for r in results:
        icon = "✅" if r["status"] == "OK" else "❌"
        log(f"  {icon} V{r['id']} ({r['kw']}): {r['detail']}")
    log(f"\n📈 总计: {len(results)} 条, ✅ {ok} 成功, ❌ {fail} 失败")
    log(f"⏱  耗时: {elapsed/60:.1f} 分钟")

    # 写报告文件
    report_path = os.path.join(out_dir, f"{drama}_渲染报告.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# {drama} 渲染报告\n\n")
        f.write(f"- 剧名: {drama}\n")
        f.write(f"- 视频数: {len(results)}\n")
        f.write(f"- 成功: {ok}\n")
        f.write(f"- 失败: {fail}\n")
        f.write(f"- 耗时: {elapsed/60:.1f} 分钟\n")
        f.write(f"- 覆盖集数: {len(eps)}\n\n")

        # 发布文案（随视频一起交付）
        f.write("## 发布文案\n\n")
        for r in results:
            vid = r["id"]
            kw = r["kw"]
            icon = "✅" if r["status"] == "OK" else "❌"
            # 从 config 中获取文案
            v_cfg = next((v for v in config["videos"] if v["id"] == vid), {})
            title = v_cfg.get("title", "")
            desc = v_cfg.get("description", "")
            tags = v_cfg.get("hashtags", [])
            tags_str = " ".join(tags)
            fname = f"{drama_safe}_{vid}_{kw}_去重版.mp4"
            cover = f"covers/{drama_safe}_{vid}_封面.jpg"

            f.write(f"### V{vid}: {kw}\n")
            f.write(f"{icon} {r['status']} | {r['detail']}\n\n")
            if title:
                f.write(f"**标题：** {title}\n")
            if desc:
                f.write(f"**描述：** {desc}\n")
            if tags_str:
                f.write(f"**话题：** {tags_str}\n")
            f.write(f"**文件：** {fname}\n")
            f.write(f"**封面：** {cover}\n\n")

        f.write("## 明细\n\n")
        f.write("| 编号 | 钩子 | 状态 | 详情 |\n")
        f.write("|---|---|---|---|\n")
        for r in results:
            icon = "✅" if r["status"] == "OK" else "❌"
            f.write(f"| V{r['id']} | {r['kw']} | {icon} {r['status']} | {r['detail']} |\n")
        if overlaps:
            f.write(f"\n## 重叠警告\n\n")
            f.write(f"共 {len(overlaps)} 处跨视频重叠，建议调整 EDL。\n\n")
            for o in overlaps:
                f.write(f"- V{o['a']['vid']}/Ep{o['a']['ep']} ↔ V{o['b']['vid']}/Ep{o['b']['ep']}: 重叠 {o['overlap_sec']:.1f}s\n")

    log(f"\n📝 报告: {report_path}")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
