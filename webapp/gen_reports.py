"""为指定剧生成完整的交付报告和渲染报告"""
import json
import os
import subprocess
import sys
from datetime import datetime

# 参数（可修改）
drama_name = '馒头姑娘旺翻沈家'
out_dir = r'D:\短剧工作区\output\小桃追剧中\馒头姑娘旺翻沈家'
account = '小桃追剧中'
src_dir = r'D:\短剧素材\馒头姑娘旺翻沈家'

# 获取视频数量
videos = [f for f in os.listdir(out_dir) if f.endswith('.mp4') and not f.endswith('.downloading')]
videos.sort()
video_count = len(videos)

print(f"剧名：{drama_name}")
print(f"输出目录：{out_dir}")
print(f"视频数量：{video_count}")
print("-" * 50)

# 获取视频信息
videos_info = []
for i in range(1, video_count + 1):
    config_path = os.path.join(out_dir, f'edl_config_v{i}.json')
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        video_info = config['videos'][0] if config.get('videos') else {}
        
        # 获取视频文件信息
        video_filename = f"{drama_name}_{i}_{video_info.get('hook_keyword', '')}_去重版.mp4"
        video_path = os.path.join(out_dir, video_filename)
        
        if os.path.exists(video_path):
            file_size = os.path.getsize(video_path)
            
            # 获取视频时长
            ffprobe_path = r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffprobe.exe'
            try:
                result = subprocess.run([
                    ffprobe_path,
                    '-v', 'quiet',
                    '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1',
                    video_path
                ], capture_output=True, text=True, timeout=10)
                duration = float(result.stdout.strip()) if result.returncode == 0 else 0
            except:
                duration = 0
            
            videos_info.append({
                'id': i,
                'hook_keyword': video_info.get('hook_keyword', ''),
                'title': video_info.get('title', ''),
                'description': video_info.get('description', ''),
                'hashtags': video_info.get('hashtags', []),
                'filename': video_filename,
                'size': file_size,
                'duration': duration
            })

# 生成时间范围（根据视频文件修改时间）
if videos_info:
    import os
    from datetime import datetime
    
    # 获取最早和最晚的视频修改时间
    video_times = []
    for v in videos_info:
        video_path = os.path.join(out_dir, v['filename'])
        if os.path.exists(video_path):
            mtime = os.path.getmtime(video_path)
            video_times.append(mtime)
    
    if video_times:
        earliest = min(video_times)
        latest = max(video_times)
        render_start = datetime.fromtimestamp(earliest).strftime('%Y-%m-%d %H:%M')
        render_end = datetime.fromtimestamp(latest).strftime('%Y-%m-%d %H:%M')
        # 计算耗时（分钟）
        duration_minutes = (latest - earliest) / 60
    else:
        render_start = ''
        render_end = ''
        duration_minutes = 0
else:
    render_start = ''
    render_end = ''
    duration_minutes = 0

# 获取集数覆盖
episodes_covered = set()
for i in range(1, video_count + 1):
    config_path = os.path.join(out_dir, f'edl_config_v{i}.json')
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        for video in config.get('videos', []):
            for seg in video.get('segments', []):
                episodes_covered.add(seg.get('episode'))

episodes_count = len(episodes_covered) if episodes_covered else 0

# 获取素材实际集数
import os
if os.path.exists(src_dir):
    src_files = [f for f in os.listdir(src_dir) if f.endswith('.mp4') and not f.endswith('.downloading')]
    src_episode_count = len(src_files)
else:
    src_episode_count = episodes_count  # 使用EDL覆盖集数作为替代

# 生成交付报告
print("生成交付报告...")
delivery_report = f"""# {drama_name} — 交付报告

## 基本信息
- **剧名**: {drama_name}
- **账号**: {account}
- **素材**: {src_dir}（{src_episode_count}集）
- **输出**: {out_dir}
- **渲染时间**: {render_start} - {render_end}（共{duration_minutes:.0f}分钟）

## 渲染结果

| 编号 | 钩子主题 | 时长 | 大小 | 状态 |
|------|----------|------|------|------|
"""

for v in videos_info:
    size_mb = v['size'] / (1024 * 1024)
    duration_s = v['duration']
    status = '✅' if duration_s > 0 else '❌'
    delivery_report += f"| V{v['id']} | {v['hook_keyword']} | {duration_s:.1f}s | {size_mb:.0f}MB | {status} |\n"

success_count = len([v for v in videos_info if v['duration'] > 0])
delivery_report += f"\n**总计**: {success_count}/{len(videos_info)}成功，{len(videos_info) - success_count}失败\n"

# 发布文案
delivery_report += "\n## 发布文案\n"
for v in videos_info:
    hashtags_str = ' '.join(v['hashtags'])
    delivery_report += f"""
### V{v['id']}: {v['hook_keyword']}
**标题：** {v['title']}
**描述：** {v['description']}
**话题：** {hashtags_str}
"""

# 文件清单
# 计算文件大小范围
if videos_info:
    sizes = [v['size'] / (1024 * 1024) for v in videos_info]
    min_size = min(sizes)
    max_size = max(sizes)
    durations = [v['duration'] for v in videos_info if v['duration'] > 0]
    avg_duration = sum(durations) / len(durations) if durations else 0
    
    delivery_report += f"""
## 文件清单

```
{out_dir}
├── {len(videos_info)}个去重版MP4（{min_size:.0f}-{max_size:.0f}MB，约{avg_duration:.0f}s）
├── covers/（{len(videos_info)}张封面）
├── edl_config_v1.json 至 edl_config_v{len(videos_info)}.json
└── {drama_name}_交付报告.md
```

## 技术规格
- 分辨率: 1080×1920 (9:16竖屏)
- 编码: H.264, CRF 23, Preset veryfast
- 音频: AAC, 128kbps
- 去重: 速度1.05x, 裁剪30px, 噪点8, 旋转0.008°, EQ+EQ+EQ
- 水印: 四角轮换（V1右上角, V2左上角, V3右下角, V4左下角）
- 帧率: 29.97fps
"""

# 写入交付报告
delivery_path = os.path.join(out_dir, f'{drama_name}_交付报告.md')
with open(delivery_path, 'w', encoding='utf-8') as f:
    f.write(delivery_report)
print(f"✅ 交付报告已生成：{delivery_path}")

# 生成渲染报告
print("\n生成渲染报告...")
render_report = f"""# {drama_name} 渲染报告

- 剧名: {drama_name}
- 视频数: {len(videos_info)}
- 成功: {success_count}
- 失败: {len(videos_info) - success_count}
- 耗时: {duration_minutes:.1f} 分钟
- 覆盖集数: {episodes_count}

## 发布文案

"""

for v in videos_info:
    hashtags_str = ' '.join(v['hashtags'])
    size_str = f"{v['size']:,}B"
    duration_str = f"{v['duration']:.1f}s" if v['duration'] > 0 else 'N/A'
    resolution = "1080x1920" if v['duration'] > 0 else "N/A"
    status = "✅ OK" if v['duration'] > 0 else "❌ FAIL"
    
    render_report += f"""### V{v['id']}: {v['title']}
{status} | {duration_str}, {size_str}, {resolution}

**标题：** {v['title']}
**描述：** {v['description']}
**话题：** {hashtags_str}
**文件：** {v['filename']}
**封面：** covers/{drama_name}_{v['id']}_封面.jpg

"""

# 明细表
render_report += """## 明细

| 编号 | 钩子 | 状态 | 详情 |
|---|---|---|---|
"""
for v in videos_info:
    status = "✅ OK" if v['duration'] > 0 else "❌ FAIL"
    detail = f"{v['duration']:.1f}s, {v['size']:,}B, 1080x1920" if v['duration'] > 0 else "N/A"
    render_report += f"| V{v['id']} | {v['hook_keyword']} | {status} | {detail} |\n"

# 写入渲染报告
render_path = os.path.join(out_dir, f'{drama_name}_渲染报告.md')
with open(render_path, 'w', encoding='utf-8') as f:
    f.write(render_report)
print(f"✅ 渲染报告已生成：{render_path}")

print("\n" + "=" * 50)
print("报告生成完成！")
