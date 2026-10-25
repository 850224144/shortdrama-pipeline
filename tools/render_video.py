# -*- coding: utf-8 -*-
"""
用 ffmpeg 直接渲染视频（替代 JianyingPro 导出）
按 EDL 将多个片段拼接成一个视频

用法：
    python render_video.py "输出视频" "片段1" "片段2" ...
"""
import sys
import os
import subprocess
import json

def run_ffmpeg(cmd, desc=""):
    """运行 ffmpeg 命令"""
    print(f"\n🔧 {desc}")
    print(f"  {' '.join(cmd[:10])}{'...' if len(cmd) > 10 else ''}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
    if result.returncode != 0:
        print(f"  ❌ ffmpeg 失败 (code {result.returncode})")
        print(f"  stderr: {result.stderr[-500:]}")
        return False
    return True

def render_from_edl(output_path, segments, subtitle_path=None):
    """
    按 EDL 渲染视频

    segments: [
        {"file": "EP01.mp4", "start": "30s", "duration": "15s"},
        {"file": "EP02.mp4", "start": "0s", "duration": "25s"},
        ...
    ]
    """
    print(f"\n{'='*60}")
    print(f"🎬 渲染视频")
    print(f"{'='*60}")
    print(f"输出: {output_path}")
    print(f"片段数: {len(segments)}")
    if subtitle_path:
        print(f"字幕: {subtitle_path}")
    
    # Check all segments exist
    for i, seg in enumerate(segments):
        if not os.path.exists(seg['file']):
            print(f"  ❌ 片段不存在: {seg['file']}")
            return False

    # Optional burned-in subtitles
    subtitle_filter = ''
    if subtitle_path and os.path.exists(subtitle_path):
        safe_subtitle = subtitle_path.replace('\\', '/').replace(':', r'\:')
        subtitle_filter = f",subtitles={safe_subtitle}:force_style='FontName=Microsoft YaHei,FontSize=16,PrimaryColour=&Hffffff&,OutlineColour=&H000000&,Outline=1,MarginV=60'"

    # Build ffmpeg filter_complex
    inputs = []
    filters = []
    concat_inputs = []

    for i, seg in enumerate(segments):
        inputs.extend(['-ss', seg['start'], '-t', seg['duration'], '-i', seg['file']])
        # Add scale and format filter, then burn subtitles if requested
        filters.append(f"[{i}:v]scale=1080:1920,setsar=1,format=yuv420p{subtitle_filter}[v{i}]")
        filters.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
        concat_inputs.append(f"[v{i}][a{i}]")

    # Concat all video + audio segments
    concat_filter = ''.join(concat_inputs) + f"concat=n={len(segments)}:v=1:a=1[out]"
    filters.append(concat_filter)

    # Build command
    cmd = ['ffmpeg', '-y'] + inputs + [
        '-filter_complex', ';'.join(filters),
        '-map', '[out]',
        '-c:v', 'libx264', '-crf', '18', '-preset', 'medium',
        '-c:a', 'aac', '-b:a', '192k',
        '-r', '30',
        output_path
    ]
    
    # Run ffmpeg
    if not run_ffmpeg(cmd, f"渲染 {len(segments)} 个片段"):
        return False
    
    # Get video info
    probe = subprocess.run(['ffmpeg', '-i', output_path], capture_output=True, text=True)
    print(f"\n✅ 渲染完成: {output_path}")
    
    # Get duration
    for line in probe.stderr.split('\n'):
        if 'Duration' in line:
            print(f"  时长: {line.split('Duration:')[1].split(',')[0].strip()}")
            break
    
    size = os.path.getsize(output_path)
    print(f"  大小: {size / 1024 / 1024:.1f} MB")
    
    return True

def main():
    if len(sys.argv) < 3:
        print("用法: python render_video.py <输出视频> <片段配置JSON>")
        print("\n片段配置 JSON 示例:")
        print('  [{')
        print('    "file": "D:\\短剧素材\\EP01.mp4",')
        print('    "start": "30s",')
        print('    "duration": "15s"')
        print('  }, { ... }]')
        sys.exit(1)
    
    output_path = sys.argv[1]
    config_path = sys.argv[2]
    
    # Load segments config
    with open(config_path, 'r', encoding='utf-8') as f:
        segments = json.load(f)
    
    if not segments:
        print("❌ 无片段配置")
        sys.exit(1)
    
    # Render
    success = render_from_edl(output_path, segments)
    
    if success:
        print(f"\n🎉 视频已生成: {output_path}")
    else:
        print(f"\n❌ 渲染失败")
        sys.exit(1)

if __name__ == "__main__":
    main()
