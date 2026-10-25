# -*- coding: utf-8 -*-
"""
百度网盘自动下载脚本
用法：python download_baidu.py <分享链接> <提取码>
自动下载到 E:\短剧素材\对应剧名\
"""
import os
import re
import sys
import subprocess

PCS_PATH = r"D:\短剧工坊\tools\BaiduPCS-Go-v3.9.7-windows-x64\BaiduPCS-Go.exe"
SAVE_ROOT = r"E:\短剧素材"


def parse_share_url(url):
    """从分享链接提取surl"""
    m = re.search(r'/s/1([A-Za-z0-9_-]+)', url)
    if not m:
        raise ValueError("无法解析分享链接")
    return m.group(1)


def main():
    if len(sys.argv) < 3:
        print("用法：python download_baidu.py <分享链接> <提取码>")
        sys.exit(1)

    share_url = sys.argv[1]
    passwd = sys.argv[2]
    surl = parse_share_url(share_url)

    print(f"分享链接: {share_url}")
    print(f"提取码: {passwd}")
    print(f"surl: {surl}")

    # 先获取分享信息，拿到剧名
    cmd = [PCS_PATH, "shared", "show", f"https://pan.baidu.com/s/1{surl}", passwd]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    print("分享信息：")
    print(result.stdout[:500])

    # TODO: 解析剧名，创建目录，执行下载
    print("等登录完成后测试下载")


if __name__ == "__main__":
    main()
