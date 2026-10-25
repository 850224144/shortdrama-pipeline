# -*- coding: utf-8 -*-
"""
自动切片脚本：调用mediakit-cli高光智剪，自动后处理
"""
import os
import json
import subprocess
import time
import urllib.request
from datetime import datetime

MEDIAKIT = "mediakit-cli"
FFMPEG = r"D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"


def run_cmd(cmd):
    print(f"运行：{cmd[:120]}...")
    result = subprocess.run(["powershell", "-Command", cmd], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print(f"  stderr: {result.stderr[:200]}")
    return result.stdout


def start_highlights_task(episode_paths, min_dur=210, max_dur=230, count=6):
    """提交高光智剪任务，直接列参数调用"""
    # 分批，总时长不超过45分钟，每集约1.5分钟，每次最多20集
    batch = episode_paths[:20]
    params = json.dumps({
        "max_duration": max_dur,
        "min_duration": min_dur,
        "max_number": count,
        "cut_mode": "Mixed"
    })
    cmd = [
        "mediakit-cli", "video", "generate-highlights-microdrama",
        "--video-urls", ",".join(batch),
        "--highlight-cuts-param", params,
        "--enable-return-poster=true"
    ]
    print(f"提交任务，{len(batch)}集...")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    out = result.stdout
    try:
        data = json.loads(out)
        return data.get("task_id")
    except Exception as e:
        print(f"提交失败：{out[:300]} {result.stderr[:200]} {e}")
        return None


def query_task(task_id):
    """查询任务结果"""
    result = subprocess.run(["mediakit-cli", "shared", "query-task", "--task-id", task_id],
                            capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(result.stdout)
    except:
        return None


def download_file(url, save_path):
    """下载成片"""
    urllib.request.urlretrieve(url, save_path)


def post_process(input_path, output_path, watermark_text="小桃追剧中"):
    """后处理：动态水印+调色+抗查重"""
    vf = (
        "setpts=PTS/1.05,"
        "crop=1080:1860:0:30,pad=1080:1920:0:30:black,"
        "eq=contrast=1.05:saturation=1.1:brightness=0.02,"
        "noise=alls=8:allf=t,rotate=0.008:fillcolor=black,fps=29.97,"
        f"drawtext=fontfile='C\\:/Windows/Fonts/msyh.ttc':text='{watermark_text}':fontsize=42:fontcolor=white@0.4:x=w-text_w-40:y=60"
    )
    af = "atempo=1.05,equalizer=f=2000:t=h:width=200:g=3,equalizer=f=100:t=h:width=50:g=-2"
    cmd = [FFMPEG, "-y", "-i", input_path, "-vf", vf, "-af", af,
           "-c:v", "libx264", "-crf", "20", "-c:a", "aac", output_path]
    subprocess.run(cmd, capture_output=True)


def find_cover(src_dir):
    """自动找封面图"""
    for f in os.listdir(src_dir):
        if f.lower() in ["cover.jpg", "cover.png", "封面.jpg", "封面图.jpg", "sph.png", "source4.png"]:
            return os.path.join(src_dir, f)
    # 找最大的图片
    imgs = [os.path.join(src_dir, f) for f in os.listdir(src_dir) if f.endswith((".jpg", ".png"))]
    if imgs:
        return max(imgs, key=os.path.getsize)
    return None


def make_cover(src_cover, out_cover, label="第1集"):
    """给封面加角标"""
    cmd = [FFMPEG, "-y", "-i", src_cover,
           "-vf", f"drawtext=fontfile='C\\:/Windows/Fonts/msyhbd.ttc':text='{label}':fontsize=52:fontcolor=white:borderw=3:bordercolor=black:x=w-text_w-30:y=h-text_h-30",
           out_cover]
    subprocess.run(cmd, capture_output=True)


def gen_copy(drama_name):
    """自动生成发布文案"""
    titles = [
        f"看完我连夜刷完了全集",
        f"这反转我看三遍才懂",
        f"太爽了根本停不下来",
        f"最后反转我鸡皮疙瘩都起来了",
        f"谁懂啊这剧太上头了",
        f"熬夜也要看完的爽剧"
    ]
    topics = "#短剧 #短剧推荐 #追剧 #热门短剧 #好剧推荐"
    return titles, topics


def write_report(out_dir, drama_name, account, count):
    """写交付报告"""
    titles, topics = gen_copy(drama_name)
    report = f"""# {drama_name} 交付报告
- 账号：{account}
- 切完时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}
- 共{count}条成片

## 发布文案
"""
    for i in range(count):
        report += f"\n### V{i+1}\n标题：{titles[i%len(titles)]}\n话题：{topics}\n"
    with open(os.path.join(out_dir, f"{drama_name}_交付报告.md"), "w", encoding="utf-8") as f:
        f.write(report)


def cut_drama(drama_name, src_dir, account):
    """切一整部剧"""
    out_dir = f"D:\\短剧工坊\\output\\{account}\\{drama_name}"
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "covers"), exist_ok=True)

    # 收集所有mp4，跳着选20集，总时长控制在45分钟内
    all_eps = sorted([os.path.join(src_dir, f) for f in os.listdir(src_dir) if f.endswith(".mp4")])
    if len(all_eps) > 20:
        step = len(all_eps) // 20
        eps = all_eps[::step][:20]
    else:
        eps = all_eps
    print(f"共 {len(all_eps)} 集，选 {len(eps)} 集分析")

    # 提交任务
    task_id = start_highlights_task(eps)
    if not task_id:
        return False

    # 轮询等结果
    for i in range(60):
        time.sleep(20)
        res = query_task(task_id)
        if res and res.get("status") == "completed":
            break
        print(f"等待中... {i*20}秒")

    if not res or res.get("status") != "completed":
        return False

    # 找封面
    cover = find_cover(src_dir)
    cover_count = len(res.get("mixvideo_info", []))

    # 下载并后处理
    for idx, video in enumerate(res.get("mixvideo_info", [])):
        url = video["video_url"]
        raw_path = os.path.join(out_dir, f"raw_{idx+1}.mp4")
        final_path = os.path.join(out_dir, f"{drama_name}_V{idx+1}_去重版.mp4")
        download_file(url, raw_path)
        post_process(raw_path, final_path, account)
        os.remove(raw_path)
        print(f"V{idx+1} 完成")
        # 做封面
        if cover:
            label = "完整版" if idx == cover_count-1 else f"第{idx+1}集"
            make_cover(cover, os.path.join(out_dir, "covers", f"{drama_name}_V{idx+1}_封面.jpg"), label)

    # 写交付报告
    write_report(out_dir, drama_name, account, cover_count)

    # 标记素材已切完
    with open(os.path.join(src_dir, "已切完.txt"), "w") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    return True
