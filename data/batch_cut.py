# -*- coding: utf-8 -*-
import os, subprocess, json, sys

SRC = r"E:\短剧素材\被赶十年，前夫后悔了"
OUT = r"D:\短剧工坊\output\被赶十年前夫后悔了"
DRAMA_NAME = "被赶十年，前夫后悔了"
WM = "小桃追剧中"

def get_ep(n):
    return os.path.join(SRC, f"{DRAMA_NAME} - 第{n}集.mp4")

def run(cmd):
    subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ep2-ep7
plans = [
    (5, [(25,10,48),(12,0,48),(32,5,48),(48,0,48),(59,15,48),(7,0,48)], "w-text_w-40:60"),
    (6, [(40,0,48),(18,5,48),(28,10,48),(45,15,48),(57,0,48),(20,5,48)], "40:h-80"),
    (7, [(20,5,48),(2,0,48),(35,0,48),(50,10,48),(60,0,48),(10,10,48)], "w-text_w-40:h-80"),
]

for ep_num, clips, pos in plans:
    ep_dir = os.path.join(OUT, f"ep{ep_num}")
    os.makedirs(ep_dir, exist_ok=True)
    inputs = []
    filter_parts = []
    for i, (ep, ss, t) in enumerate(clips):
        clip = os.path.join(ep_dir, f"clip{i}.mp4")
        run(f'ffmpeg -y -ss {ss} -i "{get_ep(ep)}" -t {t} -c:v libx264 -c:a aac "{clip}"')
        inputs.append(f'-i "{clip}"')
        filter_parts.append(f"[{i}:v][{i}:a]")
    raw = os.path.join(ep_dir, "raw.mp4")
    concat = "".join(filter_parts) + f"concat=n={len(clips)}:v=1:a=1[v][a]"
    run(f'ffmpeg -y {" ".join(inputs)} -filter_complex "{concat}" -map "[v]" -map "[a]" -c:v libx264 -c:a aac "{raw}"')
    final = os.path.join(ep_dir, "final.mp4")
    vf = f"setpts=PTS/1.05,crop=1080:1860:0:30,pad=1080:1920:0:30:black,eq=contrast=1.05:saturation=1.1:brightness=0.02,noise=alls=8:allf=t,rotate=0.008:fillcolor=black,fps=29.97,drawtext=fontfile='C\\:/Windows/Fonts/msyh.ttc':text='{WM}':fontsize=42:fontcolor=white@0.4:x={pos}"
    af = "atempo=1.05,equalizer=f=2000:t=h:width=200:g=3,equalizer=f=100:t=h:width=50:g=-2"
    run(f'ffmpeg -y -i "{raw}" -vf "{vf}" -af "{af}" -c:v libx264 -crf 20 -c:a aac "{final}"')
    # 封面
    cover_in = os.path.join(OUT, "covers", "cover.jpg")
    cover_out = os.path.join(OUT, "covers", f"cover_ep{ep_num}.jpg")
    run(f'ffmpeg -y -i "{cover_in}" -vf "drawtext=fontfile=\'C\\:/Windows/Fonts/msyhbd.ttc\':text=\'第{ep_num}集\':fontsize=52:fontcolor=white:borderw=3:bordercolor=black:x=w-text_w-30:y=h-text_h-30" "{cover_out}"')
    # 时长
    dur = subprocess.check_output(f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{final}"', shell=True).decode().strip()
    print(f"ep{ep_num} 完成，时长{float(dur):.1f}秒")

# ep8 终极混剪
ep_dir = os.path.join(OUT, "ep8")
os.makedirs(ep_dir, exist_ok=True)
clips = [(50,0,45),(45,0,45),(30,0,45),(60,0,45),(20,0,45)]
inputs = []
filter_parts = []
for i, (ep, ss, t) in enumerate(clips):
    clip = os.path.join(ep_dir, f"clip{i}.mp4")
    run(f'ffmpeg -y -ss {ss} -i "{get_ep(ep)}" -t {t} -c:v libx264 -c:a aac "{clip}"')
    inputs.append(f'-i "{clip}"')
    filter_parts.append(f"[{i}:v][{i}:a]")
raw = os.path.join(ep_dir, "raw.mp4")
concat = "".join(filter_parts) + "concat=n=5:v=1:a=1[v][a]"
run(f'ffmpeg -y {" ".join(inputs)} -filter_complex "{concat}" -map "[v]" -map "[a]" -c:v libx264 -c:a aac "{raw}"')
final = os.path.join(ep_dir, "final.mp4")
vf = f"setpts=PTS/1.05,crop=1080:1860:0:30,pad=1080:1920:0:30:black,eq=contrast=1.05:saturation=1.1:brightness=0.02,noise=alls=8:allf=t,rotate=0.008:fillcolor=black,fps=29.97,drawtext=fontfile='C\\:/Windows/Fonts/msyh.ttc':text='{WM}':fontsize=42:fontcolor=white@0.4:x=w-text_w-40:y=60"
af = "atempo=1.05,equalizer=f=2000:t=h:width=200:g=3,equalizer=f=100:t=h:width=50:g=-2"
run(f'ffmpeg -y -i "{raw}" -vf "{vf}" -af "{af}" -c:v libx264 -crf 20 -c:a aac "{final}"')
cover_in = os.path.join(OUT, "covers", "cover.jpg")
cover_out = os.path.join(OUT, "covers", "cover_ep8.jpg")
run(f'ffmpeg -y -i "{cover_in}" -vf "drawtext=fontfile=\'C\\:/Windows/Fonts/msyhbd.ttc\':text=\'完整版\':fontsize=52:fontcolor=white:borderw=3:bordercolor=black:x=w-text_w-30:y=h-text_h-30" "{cover_out}"')
# 标记已切完
open(os.path.join(SRC, "已切完.txt"), "w").write("done")
print("ep8完成，整部剧8条全部切完！")
