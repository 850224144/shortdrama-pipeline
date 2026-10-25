"""
批量上传成品到视频号草稿箱
- 读取交付报告中的文案
- 遍历所有成品视频
- 自动匹配文案
- 上传到草稿箱
"""
import os
import sys
import re
import time
import json
import argparse
from pathlib import Path

# 添加路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wechat_uploader import WeChatChannelsUploader

def parse_delivery_report(report_path):
    """解析交付报告，提取文案信息"""
    with open(report_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 提取账号
    account_match = re.search(r'账号[：:]\s*(\S+)', content)
    account = account_match.group(1) if account_match else '福桃说剧'
    
    # 提取文案
    versions = {}
    current_version = None
    current_data = {}
    
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        
        # 匹配版本标题，格式: ### V1: 逆袭 或 ### V1
        version_match = re.match(r'###\s+(V\d+)', line)
        if version_match:
            # 保存上一个版本的数据
            if current_version and current_data:
                versions[current_version] = current_data
            
            current_version = version_match.group(1)
            current_data = {}
            continue
        
        if current_version:
            # 匹配标题，格式: **标题：** xxx 或 标题：xxx
            title_match = re.match(r'\*{0,2}标题[：:]\*{0,2}\s*(.+)', line)
            if title_match:
                current_data['title'] = title_match.group(1).strip().strip('*')
            
            # 匹配话题，格式: **话题：** #tag1 #tag2 或 话题：#tag1 #tag2
            tags_match = re.match(r'\*{0,2}话题[：:]\*{0,2}\s*(.+)', line)
            if tags_match:
                tags_text = tags_match.group(1).strip().strip('*')
                # 解析话题标签
                tags = [t.strip().lstrip('#') for t in tags_text.split() if t.strip()]
                current_data['tags'] = tags
            
            # 匹配描述，格式: **描述：** xxx 或 描述：xxx
            desc_match = re.match(r'\*{0,2}描述[：:]\*{0,2}\s*(.+)', line)
            if desc_match:
                current_data['description'] = desc_match.group(1).strip().strip('*')
    
    # 保存最后一个版本
    if current_version and current_data:
        versions[current_version] = current_data
    
    return account, versions

def find_video_files(output_dir):
    """查找所有成品视频文件"""
    videos = []
    covers_dir = os.path.join(output_dir, 'covers')
    
    for f in Path(output_dir).glob('*.mp4'):
        # 跳过测试文件
        if '测试' in f.name or '最终' in f.name:
            continue
        
        # 匹配文件名格式：剧名_数字_主题_去重版.mp4
        # 例如：恶霸占我橘山_1_逆袭_去重版.mp4
        version_match = re.search(r'_\d+_', f.name)
        if version_match:
            # 提取版本号（数字）
            num_match = re.search(r'_(\d+)_', f.name)
            if num_match:
                version_num = int(num_match.group(1))
                version = f'V{version_num}'
                
                # 查找对应的封面
                cover_path = None
                if os.path.exists(covers_dir):
                    # 尝试多种封面命名格式
                    # 格式1: 剧名_数字_封面.jpg (例如: 恶霸占我橘山_1_封面.jpg)
                    # 格式2: 剧名_数字_主题_封面.jpg
                    cover_patterns = [
                        f'{f.stem.split("_")[0]}_{version_num}_封面.jpg',
                        f'{f.stem.replace("_去重版", "_封面")}.jpg',
                        f'{f.stem}_封面.jpg',
                    ]
                    for pattern in cover_patterns:
                        cover_file = os.path.join(covers_dir, pattern)
                        if os.path.exists(cover_file):
                            cover_path = cover_file
                            break
                
                videos.append({
                    'path': str(f),
                    'version': version,
                    'name': f.name,
                    'cover': cover_path,
                })
    
    # 按版本号排序
    videos.sort(key=lambda x: int(x['version'][1:]))
    
    return videos

def match_copy_to_videos(versions, videos):
    """将文案匹配到视频文件"""
    matched = []
    
    for video in videos:
        version = video['version']
        
        # 查找对应版本的文案
        copy_data = versions.get(version, {})
        
        # 如果没有找到对应版本，尝试按顺序匹配
        if not copy_data:
            version_num = int(version[1:])
            # 尝试匹配第一个可用的文案
            for v_name, v_data in versions.items():
                if v_name not in [v['version'] for v in matched]:
                    copy_data = v_data
                    break
        
        matched.append({
            **video,
            'title': copy_data.get('title', video['name']),
            'tags': copy_data.get('tags', ['短剧', '短剧推荐', '追剧', '热门短剧', '好剧推荐']),
            'description': copy_data.get('description', ''),
        })
    
    return matched

def batch_upload(account_name, videos, output_dir=None):
    """批量上传视频到草稿箱"""
    uploader = WeChatChannelsUploader(account_name)
    
    results = {
        'total': len(videos),
        'success': 0,
        'failed': 0,
        'details': [],
    }
    
    for i, video in enumerate(videos, 1):
        print(f"\n{'='*60}")
        print(f"上传 [{i}/{len(videos)}]: {video['name']}")
        print(f"标题: {video['title']}")
        print(f"话题: {video['tags']}")
        print(f"{'='*60}")
        
        try:
            success = uploader.upload_to_draft(
                video_path=video['path'],
                title=video['title'],
                tags=video['tags'],
                description=video.get('description', ''),
                cover_path=video.get('cover'),
            )
            
            if success:
                results['success'] += 1
                results['details'].append({
                    'file': video['name'],
                    'status': 'success',
                })
                print(f"✅ 上传成功")
            else:
                results['failed'] += 1
                results['details'].append({
                    'file': video['name'],
                    'status': 'failed',
                })
                print(f"❌ 上传失败")
            
            # 等待一下再上传下一个
            if i < len(videos):
                print("等待 5 秒后继续...")
                time.sleep(5)
                
        except Exception as e:
            results['failed'] += 1
            results['details'].append({
                'file': video['name'],
                'status': 'error',
                'error': str(e),
            })
            print(f"❌ 发生错误: {e}")
    
    # 保存结果
    if output_dir:
        result_path = os.path.join(output_dir, 'batch_upload_results.json')
        with open(result_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存: {result_path}")
    
    return results

def main():
    parser = argparse.ArgumentParser(description='批量上传成品到视频号草稿箱')
    parser.add_argument('--output-dir', required=True, help='成品输出目录')
    parser.add_argument('--report', help='交付报告路径（可选）')
    parser.add_argument('--account', default='福桃说剧', help='视频号账号名称')
    parser.add_argument('--dry-run', action='store_true', help='只显示将要上传的内容，不实际上传')
    parser.add_argument('--yes', action='store_true', help='跳过确认，直接上传')
    
    args = parser.parse_args()
    
    # 查找交付报告
    if args.report:
        report_path = args.report
    else:
        # 自动查找报告
        report_files = list(Path(args.output_dir).glob('*_交付报告.md'))
        if report_files:
            report_path = str(report_files[0])
        else:
            print(f"错误: 未找到交付报告，请指定 --report 参数")
            sys.exit(1)
    
    print(f"📄 读取交付报告: {report_path}")
    account, versions = parse_delivery_report(report_path)
    print(f"📋 找到 {len(versions)} 个版本的文案")
    for v_name, v_data in versions.items():
        print(f"  {v_name}: {v_data.get('title', '无标题')}")
    
    # 查找视频文件
    print(f"\n📁 查找视频文件: {args.output_dir}")
    videos = find_video_files(args.output_dir)
    print(f"🎬 找到 {len(videos)} 个视频文件")
    for v in videos:
        cover_info = f" + 封面" if v['cover'] else ""
        print(f"  {v['name']}{cover_info}")
    
    # 匹配文案
    matched_videos = match_copy_to_videos(versions, videos)
    
    # 显示将要上传的内容
    print(f"\n{'='*60}")
    print("即将上传:")
    print(f"{'='*60}")
    for i, v in enumerate(matched_videos, 1):
        print(f"\n[{i}] {v['name']}")
        print(f"    标题: {v['title']}")
        print(f"    话题: {' '.join(['#' + t for t in v['tags']])}")
        if v.get('cover'):
            print(f"    封面: {os.path.basename(v['cover'])}")
    
    if args.dry_run:
        print("\n✅ Dry run 模式，不实际上传")
        return
    
    # 确认上传
    print(f"\n确认上传 {len(matched_videos)} 个视频到 {account} 的草稿箱？")
    if not args.yes:
        input("按 Enter 继续，或 Ctrl+C 取消...")
    
    # 批量上传
    results = batch_upload(account, matched_videos, args.output_dir)
    
    # 显示结果
    print(f"\n{'='*60}")
    print(f"上传完成!")
    print(f"{'='*60}")
    print(f"总计: {results['total']}")
    print(f"成功: {results['success']}")
    print(f"失败: {results['failed']}")
    
    if results['failed'] > 0:
        print(f"\n失败的详情:")
        for d in results['details']:
            if d['status'] != 'success':
                print(f"  ❌ {d['file']}: {d.get('error', '失败')}")

if __name__ == '__main__':
    main()
