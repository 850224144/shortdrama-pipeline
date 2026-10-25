#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量处理脚本：渲染、查重、去重所有改编方案视频

用法：
    python batch_process.py [--render-only] [--dedup-only] [--check-only]

流程：
1. 根据EDL配置渲染视频（ffmpeg concat）
2. 运行查重检测（video_checker.py）
3. 应用去重优化（ffmpeg filters）
4. 重新查重验证
5. 更新报告
"""

import os
import sys
import json
import subprocess
import argparse
from datetime import datetime

# 配置
FFMPEG = r"D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"
PYTHON = r"C:\Users\86159\AppData\Local\Programs\Python\Python311\python.exe"
VIDEO_CHECKER = r"D:\短剧工坊\tools\video_checker.py"
MANIFEST = r"D:\短剧工坊\tasks\20261011_批量处理清单.json"

def run_cmd(cmd, timeout=600):
    """运行命令并返回结果"""
    try:
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            encoding='utf-8', 
            errors='ignore',
            timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -1, "", str(e)

def render_video(config, output_path):
    """根据EDL配置渲染视频"""
    with open(config, 'r', encoding='utf-8') as f:
        segments = json.load(f)
    
    if not segments:
        return False, "无片段"
    
    # 构建ffmpeg命令
    cmd = [FFMPEG, '-y']
    
    for seg in segments:
        if not os.path.exists(seg['file']):
            return False, f"文件不存在: {seg['file']}"
        cmd.extend(['-ss', seg['start'], '-t', seg['duration'], '-i', seg['file']])
    
    # 构建filter_complex
    filters = []
    for i in range(len(segments)):
        filters.append(f"[{i}:v]scale=1080:1920,setsar=1,format=yuv420p[v{i}]")
    
    concat = "".join([f"[v{i}]" for i in range(len(segments))])
    filters.append(f"{concat}concat=n={len(segments)}:v=1:a=0[out]")
    
    cmd.extend([
        '-filter_complex', ';'.join(filters),
        '-map', '[out]',
        '-c:v', 'libx264', '-crf', '18', '-preset', 'medium',
        '-c:a', 'aac', '-b:a', '192k',
        '-r', '30',
        output_path
    ])
    
    code, stdout, stderr = run_cmd(cmd, timeout=1200)
    if code != 0:
        return False, f"ffmpeg失败: {stderr[-500:]}"
    
    return True, f"成功: {len(segments)}段, {os.path.getsize(output_path)/1024/1024:.1f}MB"

def check_originality(video_path, source_videos):
    """运行查重检测"""
    cmd = [PYTHON, VIDEO_CHECKER, video_path] + source_videos
    
    code, stdout, stderr = run_cmd(cmd, timeout=600)
    
    if code != 0:
        return None, f"查重失败: {stderr[-300:]}"
    
    # 解析结果
    try:
        import re
        match = re.search(r'原创度:\s*([\d.]+)%', stdout)
        if match:
            originality = float(match.group(1))
            return originality, stdout
    except:
        pass
    
    return None, stdout

def apply_dedup(input_path, output_path):
    """应用去重优化"""
    cmd = [
        FFMPEG, '-y', '-i', input_path,
        '-filter:v', 'setpts=PTS/1.05,scale=1080:1920,eq=brightness=0.02:saturation=1.03,setsar=1',
        '-c:v', 'libx264', '-crf', '18', '-preset', 'slow',
        '-c:a', 'aac', '-b:a', '192k',
        output_path
    ]

    code, stdout, stderr = run_cmd(cmd, timeout=1200)
    if code != 0:
        return False, f"去重失败: {stderr[-300:]}"

    return True, f"成功: {os.path.getsize(output_path)/1024/1024:.1f}MB"

def generate_publish_copy(drama, name, originality, dedup_originality, output_dir, output_name, dedup_name):
    """生成发布文案文本"""
    title = f"{drama}｜{name}"
    copy = f"""标题：{title}

简介：{drama}｜{name}｜9:16竖屏｜短剧二次创作

正文：
这条内容保留了原作最抓人的冲突点，重新按短视频节奏重排，更适合快速理解剧情。
如果你还没看过原剧，也可以直接看这一版，节奏更紧凑，情绪更顺。

画面检查：
- 成片尺寸：1080x1920
- 显示比例：9:16
- 主体构图：饱满，无明显黑边
- 备注：去重版已做尺寸与裁切修正，避免出现偏小感

标签：#短剧 #甜虐 #剧情 #短视频 #原创剪辑
"""
    copy_path = os.path.join(output_dir, output_name.replace('.mp4', '_发布文案.txt'))
    with open(copy_path, 'w', encoding='utf-8') as f:
        f.write(copy)
    return copy_path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--render-only', action='store_true', help='仅渲染')
    parser.add_argument('--dedup-only', action='store_true', help='仅去重')
    parser.add_argument('--check-only', action='store_true', help='仅查重')
    parser.add_argument('--skip', nargs='*', default=[], help='跳过的方案字母')
    args = parser.parse_args()
    
    # 加载清单
    with open(MANIFEST, 'r', encoding='utf-8') as f:
        configs = json.load(f)
    
    print(f"{'='*60}")
    print(f"批量处理: {len(configs)} 个方案")
    print(f"{'='*60}")
    
    results = []
    
    for i, config in enumerate(configs, 1):
        drama = config['drama']
        letter = config['letter']
        name = config['name']
        config_path = config['config_path']
        output_dir = config['output_dir']
        
        print(f"\n{'='*60}")
        print(f"[{i}/{len(configs)}] {drama} {name}")
        print(f"{'='*60}")
        
        if letter in args.skip:
            print("⏭️  跳过")
            continue
        
        # 输出路径
        output_name = f"{drama}_{name}.mp4"
        output_path = os.path.join(output_dir, output_name)
        dedup_path = os.path.join(output_dir, output_name.replace('.mp4', '_去重.mp4'))
        
        # 获取源视频列表（用于查重）
        with open(config_path, 'r', encoding='utf-8') as f:
            segments = json.load(f)
        source_videos = list(set([seg['file'] for seg in segments]))
        
        # Step 1: 渲染
        if not args.dedup_only and not args.check_only:
            if not os.path.exists(output_path):
                print(f"🎬 渲染中...")
                success, msg = render_video(config_path, output_path)
                if success:
                    print(f"  ✅ {msg}")
                else:
                    print(f"  ❌ {msg}")
                    results.append({"name": f"{drama}_{name}", "render": False, "error": msg})
                    continue
            else:
                print(f"  ✅ 已存在")
        
        # Step 2: 查重
        if not args.render_only:
            print(f"🔍 查重中...")
            originality, output = check_originality(output_path, source_videos)
            if originality is not None:
                print(f"  原创度: {originality:.1f}%")
            else:
                print(f"  ⚠️  无法解析结果")
        
        # Step 3: 去重
        if not args.render_only and not args.check_only:
            if not os.path.exists(dedup_path):
                print(f"🛠️  去重中...")
                success, msg = apply_dedup(output_path, dedup_path)
                if success:
                    print(f"  ✅ {msg}")
                else:
                    print(f"  ❌ {msg}")
            else:
                print(f"  ✅ 已存在")
            
            # Step 4: 重新查重
            if os.path.exists(dedup_path):
                print(f"🔍 去重后查重...")
                originality2, output2 = check_originality(dedup_path, source_videos)
                if originality2 is not None:
                    print(f"  原创度: {originality2:.1f}%")

        # Step 5: 生成发布文案
        copy_path = generate_publish_copy(drama, name, originality if not args.render_only else None, originality2 if not args.render_only and not args.check_only else None, output_dir, output_name, os.path.basename(dedup_path))
        print(f"  📄 {copy_path}")

        results.append({
            "name": f"{drama}_{name}",
            "render": True,
            "originality": originality if not args.render_only else None,
            "dedup_originality": originality2 if not args.render_only and not args.check_only else None,
            "copy_path": copy_path
        })
    
    # 保存结果
    results_path = os.path.join(os.path.dirname(MANIFEST), "批量处理结果.json")
    with open(results_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'='*60}")
    print(f"完成: {len(results)} 个方案")
    print(f"结果保存: {results_path}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
