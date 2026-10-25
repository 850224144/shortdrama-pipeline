"""
短剧自动化流水线 - 完整版本
串联：下载 → 切片 → 上传

用法：
    # 只下载
    python full_pipeline.py download --url <分享链接> --code <提取码>
    
    # 只切片
    python full_pipeline.py cut --drama <剧名> --source <素材目录>
    
    # 只上传
    python full_pipeline.py upload --output <输出目录>
    
    # 完整流程（下载+切片+上传）
    python full_pipeline.py all --url <分享链接> --code <提取码>
"""
import os
import sys
import re
import json
import time
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

# 配置
PCS_PATH = r"D:\短剧工坊\tools\BaiduPCS-Go-v4.0.2-windows-x64\BaiduPCS-Go.exe"
FFMPEG = r"D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe"
SOURCE_ROOT = r"E:\短剧素材"
OUTPUT_ROOT = r"D:\短剧工作区\output"
SCRIPTS_DIR = r"D:\短剧工坊\scripts"
WEBAPP_DIR = r"D:\短剧工坊\webapp"
PYTHON = r"C:\Users\86159\AppData\Local\Programs\Python\Python311\python.exe"

# 添加路径
sys.path.insert(0, WEBAPP_DIR)
sys.path.insert(0, SCRIPTS_DIR)

def log(msg):
    """带时间戳的日志"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def download_materials(share_url, passwd):
    """下载素材"""
    log(f"📥 开始下载素材...")
    log(f"   链接: {share_url}")
    log(f"   提取码: {passwd}")
    
    # 解析分享链接
    m = re.search(r'/s/1([A-Za-z0-9_-]+)', share_url)
    if not m:
        log("❌ 无法解析分享链接")
        return False
    
    surl = m.group(1)
    
    # 先获取分享信息
    cmd = f'"{PCS_PATH}" shared show "https://pan.baidu.com/s/1{surl}" "{passwd}"'
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", shell=True)
    
    if result.returncode != 0:
        log(f"❌ 获取分享信息失败: {result.stderr[:200]}")
        return False
    
    # 解析剧名
    output = result.stdout
    drama_name = None
    for line in output.split('\n'):
        if '目录' in line or '文件夹' in line:
            parts = line.strip().split()
            if parts:
                drama_name = parts[0]
                break
    
    if not drama_name:
        log("❌ 无法解析剧名")
        return False
    
    log(f"✅ 解析剧名: {drama_name}")
    
    # 创建素材目录
    drama_dir = os.path.join(SOURCE_ROOT, drama_name)
    os.makedirs(drama_dir, exist_ok=True)
    
    # 下载所有文件
    cmd = f'"{PCS_PATH}" shared save "https://pan.baidu.com/s/1{surl}" "{passwd}" "{drama_dir}"'
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", shell=True)
    
    if result.returncode != 0:
        log(f"❌ 下载失败: {result.stderr[:200]}")
        return False
    
    # 检查下载结果
    mp4s = [f for f in os.listdir(drama_dir) if f.endswith('.mp4')]
    log(f"✅ 下载完成: {len(mp4s)} 集素材")
    
    return drama_name, drama_dir

def cut_drama(drama_name, source_dir, account="小桃追剧中"):
    """切片：生成多个版本"""
    log(f"✂️ 开始切片: {drama_name}")
    
    # 检查是否已存在输出目录
    output_dir = os.path.join(OUTPUT_ROOT, account, drama_name)
    if os.path.exists(output_dir):
        mp4s = [f for f in os.listdir(output_dir) if f.endswith('.mp4') and '去重版' in f]
        if len(mp4s) > 0:
            log(f"⚠️ 已存在 {len(mp4s)} 个成品，跳过切片")
            return output_dir
    
    # 调用 render_generic.py
    render_script = os.path.join(SCRIPTS_DIR, "render_generic.py")
    if not os.path.exists(render_script):
        log(f"❌ 找不到 render_generic.py: {render_script}")
        return None
    
    cmd = [
        PYTHON, render_script,
        '--source', source_dir,
        '--output', output_dir,
        '--drama', drama_name,
        '--count', '8',
    ]
    
    log(f"   运行: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    
    if result.returncode != 0:
        log(f"❌ 切片失败: {result.stderr[:500]}")
        return None
    
    # 检查输出
    if os.path.exists(output_dir):
        mp4s = [f for f in os.listdir(output_dir) if f.endswith('.mp4') and '去重版' in f]
        log(f"✅ 切片完成: {len(mp4s)} 个版本")
        return output_dir
    
    return None

def upload_videos(output_dir, account="福桃说剧"):
    """上传到视频号草稿箱"""
    log(f"📤 开始上传: {output_dir}")
    
    # 检查交付报告
    report_files = list(Path(output_dir).glob('*_交付报告.md'))
    if not report_files:
        log(f"❌ 找不到交付报告")
        return False
    
    report_path = str(report_files[0])
    log(f"   交付报告: {report_path}")
    
    # 调用 batch_upload.py
    upload_script = os.path.join(WEBAPP_DIR, "batch_upload.py")
    if not os.path.exists(upload_script):
        log(f"❌ 找不到 batch_upload.py: {upload_script}")
        return False
    
    cmd = [
        PYTHON, upload_script,
        '--output-dir', output_dir,
        '--account', account,
    ]
    
    log(f"   运行: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    
    if result.returncode != 0:
        log(f"❌ 上传失败: {result.stderr[:500]}")
        return False
    
    log(f"✅ 上传完成")
    return True

def run_full_pipeline(share_url, passwd, account="小桃追剧中"):
    """完整流水线：下载 → 切片 → 上传"""
    log("=" * 60)
    log("短剧自动化流水线 - 完整流程")
    log("=" * 60)
    
    # 1. 下载
    log("\n📥 步骤 1/3: 下载素材")
    download_result = download_materials(share_url, passwd)
    if not download_result:
        log("❌ 下载失败，终止流水线")
        return False
    
    drama_name, source_dir = download_result
    
    # 2. 切片
    log("\n✂️ 步骤 2/3: 切片")
    output_dir = cut_drama(drama_name, source_dir, account)
    if not output_dir:
        log("❌ 切片失败，终止流水线")
        return False
    
    # 3. 上传
    log("\n📤 步骤 3/3: 上传到草稿箱")
    upload_account = "福桃说剧"  # 默认上传到福桃说剧
    upload_ok = upload_videos(output_dir, upload_account)
    
    if upload_ok:
        log("\n" + "=" * 60)
        log("🎉 流水线完成!")
        log(f"   剧名: {drama_name}")
        log(f"   输出: {output_dir}")
        log("=" * 60)
        return True
    else:
        log("❌ 上传失败，但切片已完成")
        log(f"   可手动运行: python full_pipeline.py upload --output {output_dir}")
        return False

def main():
    parser = argparse.ArgumentParser(description='短剧自动化流水线')
    subparsers = parser.add_subparsers(dest='command', help='命令')
    
    # download 命令
    dl_parser = subparsers.add_parser('download', help='只下载素材')
    dl_parser.add_argument('--url', required=True, help='分享链接')
    dl_parser.add_argument('--code', required=True, help='提取码')
    
    # cut 命令
    cut_parser = subparsers.add_parser('cut', help='只切片')
    cut_parser.add_argument('--drama', required=True, help='剧名')
    cut_parser.add_argument('--source', required=True, help='素材目录')
    cut_parser.add_argument('--account', default='小桃追剧中', help='账号')
    
    # upload 命令
    up_parser = subparsers.add_parser('upload', help='只上传')
    up_parser.add_argument('--output', required=True, help='输出目录')
    up_parser.add_argument('--account', default='福桃说剧', help='上传账号')
    
    # all 命令
    all_parser = subparsers.add_parser('all', help='完整流程')
    all_parser.add_argument('--url', required=True, help='分享链接')
    all_parser.add_argument('--code', required=True, help='提取码')
    all_parser.add_argument('--account', default='小桃追剧中', help='账号')
    
    args = parser.parse_args()
    
    if args.command == 'download':
        result = download_materials(args.url, args.code)
        if result:
            drama_name, source_dir = result
            print(f"\n✅ 下载完成")
            print(f"   剧名: {drama_name}")
            print(f"   目录: {source_dir}")
        else:
            print("❌ 下载失败")
            sys.exit(1)
    
    elif args.command == 'cut':
        output_dir = cut_drama(args.drama, args.source, args.account)
        if output_dir:
            print(f"\n✅ 切片完成")
            print(f"   输出: {output_dir}")
        else:
            print("❌ 切片失败")
            sys.exit(1)
    
    elif args.command == 'upload':
        ok = upload_videos(args.output, args.account)
        if ok:
            print(f"\n✅ 上传完成")
        else:
            print("❌ 上传失败")
            sys.exit(1)
    
    elif args.command == 'all':
        ok = run_full_pipeline(args.url, args.code, args.account)
        if not ok:
            sys.exit(1)
    
    else:
        parser.print_help()

if __name__ == '__main__':
    main()
