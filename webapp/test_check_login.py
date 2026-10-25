# -*- coding: utf-8 -*-
"""视频号登录验证测试"""

import sys
import os

sys.path.insert(0, r'D:\短剧工坊\webapp')

from wechat_uploader import WeChatChannelsUploader

# 测试两个账号
accounts = ['福桃说剧', '小桃追剧中']

print("=" * 60)
print("视频号登录状态验证")
print("=" * 60)

for account in accounts:
    print(f"\n【{account}】")
    uploader = WeChatChannelsUploader(account_name=account)
    
    if os.path.exists(uploader.user_data_path):
        print(f"  数据目录: {uploader.user_data_path}")
        print(f"  验证登录状态...")
        
        if uploader.check_login():
            print(f"  ✅ 登录有效")
        else:
            print(f"  ❌ 登录失效，需要重新登录")
    else:
        print(f"  ⚠️ 未找到登录数据，需要登录")

print("\n" + "=" * 60)
print("验证完成")
print("=" * 60)