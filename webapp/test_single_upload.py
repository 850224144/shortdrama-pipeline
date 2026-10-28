"""
单个视频上传测试
"""
import os
import sys
import time
from pathlib import Path

# 添加路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wechat_uploader import WeChatChannelsUploader

def test_single_upload():
    """测试单个视频上传"""
    
    # 测试视频
    video_dir = r"D:\短剧工作区\output\小桃追剧中\恶霸占我橘山"
    video_file = "恶霸占我橘山_1_逆袭_去重版.mp4"
    video_path = os.path.join(video_dir, video_file)
    
    # 测试封面
    cover_path = os.path.join(video_dir, "covers", "恶霸占我橘山_1_封面.jpg")
    
    # 测试文案
    title = "[1.1-2.0]你敢动我试试"
    description = "[1.1-2.0] 你敢动我试试"
    tags = ['短剧', '短剧推荐', '追剧', '热门短剧', '好剧推荐']
    
    print("=" * 60)
    print("单个视频上传测试")
    print("=" * 60)
    print(f"视频: {video_file}")
    print(f"封面: {os.path.basename(cover_path) if os.path.exists(cover_path) else '无'}")
    print(f"标题: {title}")
    print(f"话题: {tags}")
    print("=" * 60)
    
    # 检查文件是否存在
    if not os.path.exists(video_path):
        print(f"❌ 找不到视频文件: {video_path}")
        return False
    
    if os.path.exists(cover_path):
        cover_size = os.path.getsize(cover_path)
        print(f"封面大小: {cover_size / 1024:.1f} KB")
    
    # 启动上传
    account = '福桃说剧'
    uploader = WeChatChannelsUploader(account)
    
    print(f"\n开始上传到 {account} 的草稿箱...")
    print("-" * 60)
    
    start_time = time.time()
    result = uploader.upload_to_draft(
        video_path=video_path,
        title=title,
        tags=tags,
        description=description,
        cover_path=cover_path,
    )
    elapsed = time.time() - start_time
    
    print("-" * 60)
    
    if result.get('status') == 'success':
        print(f"✅ 上传成功! 耗时: {elapsed:.1f}秒")
        ok = True
    else:
        print(f"❌ 上传失败! 耗时: {elapsed:.1f}秒 - {result.get('error') or result}")
        ok = False
    
    return ok

if __name__ == '__main__':
    test_single_upload()
