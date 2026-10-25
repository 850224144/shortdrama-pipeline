"""为恶霸占我橘山生成封面 - 保存到 covers/ 目录"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from processor import PipelineProcessor, Logger, StateManager

# 初始化
logger = Logger(r'D:\短剧工坊\logs')
state_manager = StateManager(r'D:\短剧工坊\data\pipeline_state.json')
processor = PipelineProcessor(logger, state_manager)

# 参数
drama_name = '恶霸占我橘山'
src_dir = r'D:\短剧素材\恶霸占我橘山'
out_dir = r'D:\短剧工作区\output\小桃追剧中\恶霸占我橘山'

# 创建covers目录
covers_dir = os.path.join(out_dir, 'covers')
os.makedirs(covers_dir, exist_ok=True)

# 获取所有视频文件
videos = [f for f in os.listdir(out_dir) if f.endswith('.mp4') and not f.endswith('.downloading')]
videos.sort()

print(f"素材目录：{src_dir}")
print(f"输出目录：{out_dir}")
print(f"封面目录：{covers_dir}")
print(f"找到 {len(videos)} 个视频文件")
print("-" * 50)

for i, video in enumerate(videos, 1):
    video_path = os.path.join(out_dir, video)
    cover_path = os.path.join(covers_dir, f'{drama_name}_{i}_封面.jpg')
    
    print(f"\n[{i}/{len(videos)}] 生成封面：{video}")
    
    try:
        processor._generate_cover(
            input_path=video_path,
            video_path=video_path,
            drama_name=drama_name,
            src_dir=src_dir,
            version_idx=i
        )
        
        if os.path.exists(cover_path):
            size = os.path.getsize(cover_path)
            print(f"  ✅ 成功：{cover_path} ({size/1024:.1f}KB)")
        else:
            print(f"  ❌ 失败：封面未生成")
    except Exception as e:
        print(f"  ❌ 异常：{str(e)}")

print("\n" + "=" * 50)
print("封面生成完成")
