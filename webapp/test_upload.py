# -*- coding: utf-8 -*-
"""视频号上传测试 - 上传到草稿箱"""

import sys
import os

sys.path.insert(0, r'D:\短剧工坊\webapp')

from wechat_uploader import WeChatChannelsUploader

# 配置
ACCOUNT = '福桃说剧'
VIDEO_PATH = r'D:\短剧工作区\output\小桃追剧中\恶霸占我橘山\恶霸占我橘山_1_逆袭_去重版.mp4'
COVER_PATH = r'D:\短剧工作区\output\小桃追剧中\恶霸占我橘山\covers\恶霸占我橘山_1_封面.jpg'
TITLE = '恶霸欺我我必逆袭'
TAGS = ['豪门逆袭', '身份曝光', '短剧推荐', '恶霸欺人', '千金归来']
DESCRIPTION = '恶霸强占橘子山，她隐忍多年无人知晓。直到真实身份即将曝光，所有人都不敢相信她竟是隐藏多年的豪门千金。一场惊天逆袭即将展开，谁能挡住她的脚步？'

print("=" * 60)
print("视频号上传测试")
print("=" * 60)
print(f"账号: {ACCOUNT}")
print(f"视频: {os.path.basename(VIDEO_PATH)}")
print(f"封面: {os.path.basename(COVER_PATH) if os.path.exists(COVER_PATH) else '无'}")
print(f"标题: {TITLE}")
print(f"话题: {', '.join(TAGS)}")
print(f"描述: {DESCRIPTION[:50]}...")
print()

# 检查文件
if not os.path.exists(VIDEO_PATH):
    print(f"❌ 找不到视频文件: {VIDEO_PATH}")
    print("请先运行切片流程生成视频")
    sys.exit(1)

# 检查文件大小
file_size = os.path.getsize(VIDEO_PATH)
print(f"视频大小: {file_size / 1024 / 1024:.1f} MB")
print()

# 创建上传器
uploader = WeChatChannelsUploader(account_name=ACCOUNT)

# 检查登录
if not uploader.check_login():
    print("❌ 登录失效，请先登录")
    sys.exit(1)

print("✅ 登录有效，开始上传...")
print()

# 上传到草稿箱
success = uploader.upload_to_draft(
    video_path=VIDEO_PATH,
    title=TITLE,
    tags=TAGS,
    description=DESCRIPTION,
    cover_path=COVER_PATH
)

print()
print("=" * 60)
if success:
    print("✅ 上传成功！视频已保存到草稿箱")
    print("请登录视频号查看草稿箱")
else:
    print("❌ 上传失败，请检查日志")
print("=" * 60)