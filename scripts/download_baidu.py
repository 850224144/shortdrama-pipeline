# -*- coding: utf-8 -*-
"""百度网盘下载（独立 CLI，带剧档案状态追踪 + 防重 + 嵌套规避）。

用法：
  python download_baidu.py <分享链接> <提取码> <剧名> [--dry-run]

流程：
  1. 防重检查（archive.should_download）：本地目录已存在 或 历史已下载成功 → 不下载
  2. 标记 download_state=downloading
  3. BaiduPCS-Go download 下载到临时目录 .downloading/（规避 saveto 同名嵌套）
  4. 完成后上移内容到 E:\\短剧素材\\<剧名>\\ 直接层，回填 done + 集数/大小/文件数
  5. 失败标记 failed

注：BaiduPCS-Go 需已登录（手动 BaiduPCS-Go login 或 /api/baidupcs/login）。
"""
import os
import re
import sys
import shutil
import subprocess
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'webapp'))

import archive  # webapp/archive.py（剧档案：防重 + 状态 + 搜索）

PCS_PATH = r"D:\短剧工坊\素材\BaiduPCS-Go\BaiduPCS-Go-v4.0.2-windows-x64\BaiduPCS-Go.exe"
SAVE_ROOT = r"E:\短剧素材"
DOWNLOAD_TIMEOUT = 7200  # 单次下载最长 2 小时


def parse_share_url(url):
    """从分享链接提取 surl。"""
    m = re.search(r'/s/1([A-Za-z0-9_-]+)', url)
    if not m:
        raise ValueError(f"无法解析分享链接: {url}")
    return m.group(1)


def _run_pcs(args, timeout=60):
    """运行 BaiduPCS-Go，返回 CompletedProcess。"""
    return subprocess.run(
        [PCS_PATH] + args,
        capture_output=True, text=True, encoding='utf-8',
        timeout=timeout, cwd=os.path.dirname(PCS_PATH))


def check_login():
    """检查 BaiduPCS-Go 登录态。返回 (bool, info)。"""
    try:
        r = _run_pcs(['who'], timeout=10)
        out = r.stdout + r.stderr
        return (r.returncode == 0 and ('uid' in out.lower() or '账号' in out)), out[:300]
    except Exception as e:
        return False, str(e)


def _count_episodes(dir_path):
    """递归扫描唯一集号数（不校验完整性，仅回填展示）。"""
    eps = set()
    for root, _, files in os.walk(dir_path):
        for f in files:
            m = re.search(r'(\d+)\.mp4$', f)
            if m:
                eps.add(int(m.group(1)))
    return len(eps)


def _dir_size(dir_path):
    """返回 (总字节, 文件数)。"""
    total, n = 0, 0
    for root, _, files in os.walk(dir_path):
        for f in files:
            p = os.path.join(root, f)
            try:
                total += os.path.getsize(p)
                n += 1
            except OSError:
                pass
    return total, n


def _flatten(tmp_dir, local_dir):
    """下载后把 tmp_dir 下的内容上移到 local_dir 直接层（规避嵌套）。

    BaiduPCS-Go 保留网盘相对路径：--saveto 与网盘文件夹同名会产生嵌套子目录。
    先下载到不同名的 .downloading/，再递归上移所有 mp4 + 辅助文件到直接层。
    """
    os.makedirs(local_dir, exist_ok=True)
    moved = 0
    for root, _, files in os.walk(tmp_dir):
        for f in files:
            src = os.path.join(root, f)
            dst = os.path.join(local_dir, f)
            if os.path.exists(dst):
                # 同名冲突：保留目标，源跳过（不覆盖已有）
                continue
            try:
                shutil.move(src, dst)
                moved += 1
            except (OSError, shutil.Error) as e:
                print(f'  移动失败 {f}: {e}')
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return moved


def main():
    if len(sys.argv) < 4:
        print('用法：python download_baidu.py <分享链接> <提取码> <剧名> [--dry-run]')
        print('示例：python download_baidu.py "https://pan.baidu.com/s/1xxx?pwd=9999" 9999 剧名')
        sys.exit(1)
    share_url, passwd, drama_name = sys.argv[1], sys.argv[2], sys.argv[3]
    dry_run = '--dry-run' in sys.argv

    archive.init_database()
    local_dir = os.path.join(SAVE_ROOT, drama_name)

    # 登录检查
    logged_in, info = check_login()
    if not logged_in:
        print(f'BaiduPCS-Go 未登录：{info}')
        print('请先登录：BaiduPCS-Go login 或 /api/baidupcs/login')
        sys.exit(1)

    # 防重检查
    ok, reason = archive.should_download(drama_name, local_dir)
    if not ok:
        print(f'防重：{reason}')
        print('不下载（本地已有 或 历史已下载成功）。')
        sys.exit(0)

    surl = parse_share_url(share_url)
    print(f'可下载：{drama_name}（surl={surl}）')
    if dry_run:
        print(f'[dry-run] 将执行：BaiduPCS-Go download /短剧素材/{drama_name} '
              f'--saveto {os.path.join(local_dir, ".downloading")} --overwrite=never')
        print('[dry-run] 完成后上移到', local_dir)
        sys.exit(0)

    # 标记 downloading
    archive.upsert_archive(drama_name, source_url=share_url, source_passwd=passwd,
                           local_dir=local_dir, download_state='downloading')
    tmp_dir = os.path.join(local_dir, '.downloading')
    os.makedirs(tmp_dir, exist_ok=True)
    print(f'开始下载：/短剧素材/{drama_name} → {tmp_dir}（临时，规避嵌套）')

    try:
        r = _run_pcs(['download', f'/短剧素材/{drama_name}',
                      '--saveto', tmp_dir, '--overwrite=never'],
                     timeout=DOWNLOAD_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError((r.stdout or '')[-300:] + (r.stderr or '')[-300:])
        moved = _flatten(tmp_dir, local_dir)
        size, files = _dir_size(local_dir)
        eps = _count_episodes(local_dir)
        archive.upsert_archive(drama_name, download_state='done', download_size=size,
                               download_files=files, episode_count=eps,
                               downloaded_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        print(f'下载完成：{drama_name} {eps}集 {size / 1e9:.2f}G {files}文件（上移{moved}个）')
    except Exception as e:
        archive.upsert_archive(drama_name, download_state='failed', download_error=str(e))
        print(f'下载失败：{e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
