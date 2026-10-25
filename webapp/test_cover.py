"""测试封面生成"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from processor import PipelineProcessor, Logger, StateManager

# 初始化
logger = Logger(r'D:\短剧工坊\logs')
state_manager = StateManager(r'D:\短剧工坊\data\pipeline_state.json')
processor = PipelineProcessor(logger, state_manager)

# 测试参数
src_dir = r'D:\短剧素材\馒头姑娘旺翻沈家\馒头姑娘旺翻沈家'
drama_name = '馒头姑娘旺翻沈家'
out_dir = r'D:\短剧工作区\output\小桃追剧中\馒头姑娘旺翻沈家'

# 创建一个测试视频（从素材中复制一集）
import shutil
test_video = os.path.join(out_dir, 'test_input.mp4')
src_video = os.path.join(src_dir, '2.mp4')
shutil.copy(src_video, test_video)

# 生成封面
cover_path = os.path.join(out_dir, 'test_input_封面.jpg')
print(f"素材目录：{src_dir}")
print(f"输出目录：{out_dir}")
print(f"测试视频：{test_video}")
print(f"封面路径：{cover_path}")
print("-" * 50)

# 调用封面生成
processor._generate_cover(
    input_path=test_video,
    video_path=test_video,
    drama_name=drama_name,
    src_dir=src_dir,
    version_idx=1
)

# 检查是否生成
if os.path.exists(cover_path):
    size = os.path.getsize(cover_path)
    print(f"✅ 封面已生成：{cover_path} ({size/1024:.1f}KB)")
else:
    print(f"❌ 封面未生成")

# 清理
os.remove(test_video)
if os.path.exists(cover_path):
    os.remove(cover_path)

print("测试完成")