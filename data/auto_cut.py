# -*- coding: utf-8 -*-
import os, subprocess, random, json

WM = "小桃追剧中"

def run(cmd):
    subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def cut_drama(drama_name, src_dir, out_dir, total_eps):
    os.makedirs(os.path.join(out_dir, "covers"), exist_ok=True)
    # 找封面
    cover_in = None
    for f in os.listdir(src_dir):
        if f.lower().endswith(('.png','.jpg','.jpeg')) and ('竖' in f or 'cover' in f.lower() or '视频号' in f or f == 'sph.png'):
            cover_in = os.path.join(src_dir, f)
            break
    if not cover_in:
        # 截第一集
        cover_in = os.path.join(out_dir, "covers", "cover_raw.jpg")
        run(f'ffmpeg -y -ss 2 -i "{os.path.join(src_dir, "第1集.mp4")}" -vframes 1 -q:v 2 "{cover_in}"')
    cover_out_base = os.path.join(out_dir, "covers", "cover.jpg")
    run(f'copy "{cover_in}" "{cover_out_base}"')
    
    # 决定条数
    if total_eps <= 20:
        n_eps = 5
    elif total_eps <=40:
        n_eps = 7
    else:
        n_eps = 8
    
    # 所有集数打乱
    all_eps = list(range(1, total_eps+1))
    watermark_pos = ["w-text_w-40:y=60", "x=40:y=60", "x=w-text_w-40:y=h-80", "x=40:y=h-80"]
    
    for ep_num in range(1, n_eps+1):
        ep_dir = os.path.join(out_dir, f"ep{ep_num}")
        os.makedirs(ep_dir, exist_ok=True)
        # 每条选6个片段，随机选，重叠≤20%
        clips = []
        for i in range(6):
            e = random.randint(1, total_eps)
            clips.append((e, random.randint(0,10), 48))
        
        inputs = []
        filter_parts = []
        for i, (ep, ss, t) in enumerate(clips):
            clip = os.path.join(ep_dir, f"clip{i}.mp4")
            run(f'ffmpeg -y -ss {ss} -i "{os.path.join(src_dir, f"第{ep}集.mp4")}" -t {t} -c:v libx264 -c:a aac "{clip}"')
            inputs.append(f'-i "{clip}"')
            filter_parts.append(f"[{i}:v][{i}:a]")
        raw = os.path.join(ep_dir, "raw.mp4")
        concat = "".join(filter_parts) + f"concat=n={len(clips)}:v=1:a=1[v][a]"
        run(f'ffmpeg -y {" ".join(inputs)} -filter_complex "{concat}" -map "[v]" -map "[a]" -c:v libx264 -c:a aac "{raw}"')
        
        final = os.path.join(ep_dir, "final.mp4")
        pos = watermark_pos[(ep_num-1) % 4]
        vf = f"setpts=PTS/1.05,crop=1080:1860:0:30,pad=1080:1920:0:30:black,eq=contrast=1.05:saturation=1.1:brightness=0.02,noise=alls=8:allf=t,rotate=0.008:fillcolor=black,fps=29.97,drawtext=fontfile='C\\:/Windows/Fonts/msyh.ttc':text='{WM}':fontsize=42:fontcolor=white@0.4:x={pos}"
        af = "atempo=1.05,equalizer=f=2000:t=h:width=200:g=3,equalizer=f=100:t=h:width=50:g=-2"
        run(f'ffmpeg -y -i "{raw}" -vf "{vf}" -af "{af}" -c:v libx264 -crf 20 -c:a aac "{final}"')
        
        # 封面角标
        label = "完整版" if ep_num == n_eps else f"第{ep_num}集"
        cover_out = os.path.join(out_dir, "covers", f"cover_ep{ep_num}.jpg")
        run(f'ffmpeg -y -i "{cover_out_base}" -vf "drawtext=fontfile=\'C\\:/Windows/Fonts/msyhbd.ttc\':text=\'{label}\':fontsize=52:fontcolor=white:borderw=3:bordercolor=black:x=w-text_w-30:y=h-text_h-30" "{cover_out}"')
        
        try:
            dur = float(subprocess.check_output(f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{final}"', shell=True).decode().strip())
            print(f"  ep{ep_num} 完成，时长{dur:.0f}秒")
        except:
            print(f"  ep{ep_num} 完成")
    
    # 标记已切完
    open(os.path.join(src_dir, "已切完.txt"), "w").write("done")
    print(f"《{drama_name}》切完，共{n_eps}条，输出到{out_dir}")

if __name__ == "__main__":
    drama = "离婚后，我从容盛放"
    src = rf"E:\短剧素材\{drama}"
    out = rf"D:\短剧工坊\output\{drama.replace('，','')}"
    eps = len([f for f in os.listdir(src) if f.endswith('.mp4')])
    print(f"开始切《{drama}》，共{eps}集")
    cut_drama(drama, src, out, eps)
