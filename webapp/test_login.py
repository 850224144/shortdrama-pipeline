# -*- coding: utf-8 -*-
"""视频号登录测试"""

import sys
import os

sys.path.insert(0, r'D:\短剧工坊\webapp')

from wechat_uploader import WeChatChannelsUploader

# 创建上传器实例
uploader = WeChatChannelsUploader(account_name='福桃说剧')

# 执行登录
print("=" * 60)
print("视频号登录测试")
print("=" * 60)
print(f"账号: {uploader.account_name}")
print(f"数据目录: {uploader.user_data_path}")
print()

# 检查是否已登录
if os.path.exists(uploader.user_data_path):
    print("检测到已有登录数据，尝试验证...")
    if uploader.check_login():
        print("✅ 登录有效，无需重新登录")
        sys.exit(0)
    else:
        print("❌ 登录失效，需要重新登录")

# 启动登录流程
print()
print("请确保已安装 Chrome 浏览器")
print("登录窗口将自动打开，请在窗口中扫码登录")
print()

if uploader.login():
    print("\n✅ 登录成功！")
    print("登录数据已保存，下次无需再次扫码")
else:
    print("\n❌ 登录失败")
    sys.exit(1)