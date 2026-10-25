# -*- coding: utf-8 -*-
"""
视频查重工具 - 原创度检测
替代付费 video_checker，使用感知哈希（dHash + pHash）比较视频帧相似度

用法：
    python video_checker.py "成片路径" "原片路径1" "原片路径2" ...

输出：
    原创度评分 + 相似帧对比 + 优化建议
"""
import sys
import os
import json
import numpy as np
import cv2
import imagehash
from PIL import Image
from datetime import datetime

def extract_frames(video_path, fps=0.5, max_frames=None):
    """从视频提取帧（默认每秒1帧）"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"  ❌ 无法打开视频: {video_path}")
        return [], 0
    
    fps_actual = cap.get(cv2.CAP_PROP_FPS)
    interval = max(1, int(fps_actual / fps))
    frames = []
    idx = 0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % interval == 0:
            small = cv2.resize(frame, (64, 64))
            frames.append(small)
            if max_frames and len(frames) >= max_frames:
                break
        idx += 1
    
    total_frames = idx
    cap.release()
    return frames, total_frames

def compute_hashes(frames):
    """计算 dHash 和 pHash"""
    dhashes, phashes = [], []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        pil_img = Image.fromarray(gray)
        dhashes.append(imagehash.dhash(pil_img, hash_size=8))
        phashes.append(imagehash.phash(pil_img, hash_size=8))
    return dhashes, phashes

def hamming_distance(h1, h2):
    """计算两个哈希的海明距离"""
    return int(h1 - h2)

def find_similar_frames(out_hashes, src_hashes_list, src_names, threshold_d=10, threshold_p=10):
    """对每个输出帧，找到最相似的原片帧"""
    results = []
    for i, (od, op) in enumerate(zip(out_hashes[0], out_hashes[1])):
        best_match = None
        best_dist = 999
        
        for src_idx, src_hashes in enumerate(src_hashes_list):
            src_d, src_p = src_hashes
            for j, (sd, sp) in enumerate(zip(src_d, src_p)):
                dist_d = hamming_distance(od, sd)
                dist_p = hamming_distance(op, sp)
                avg_dist = (dist_d + dist_p) / 2
                
                if avg_dist < best_dist:
                    best_dist = avg_dist
                    best_match = (src_idx, j, dist_d, dist_p)
        
        is_matched = best_dist < max(threshold_d, threshold_p)
        results.append({
            'frame_idx': i,
            'best_src': src_names[best_match[0]] if best_match else None,
            'src_frame_idx': best_match[1] if best_match else None,
            'dist_dhash': best_match[2] if best_match else None,
            'dist_phash': best_match[3] if best_match else None,
            'avg_dist': best_dist,
            'matched': is_matched
        })
    
    return results

def calculate_originality(output_video, source_videos, source_names, threshold=10):
    """计算原创度评分"""
    print(f"\n{'='*60}")
    print(f"📊 视频查重报告")
    print(f"{'='*60}")
    print(f"\n🎬 输出视频: {os.path.basename(output_video)}")
    print(f"📦 原片数量: {len(source_videos)}")
    for i, (v, n) in enumerate(zip(source_videos, source_names)):
        print(f"  [{i+1}] {n} ({os.path.basename(v)})")
    
    # Extract output frames
    print(f"\n📺 提取输出视频帧...")
    out_frames, out_total = extract_frames(output_video, fps=0.5)
    print(f"  提取帧数: {len(out_frames)} (总帧: {out_total})")
    
    if len(out_frames) == 0:
        return None
    
    # Compute output hashes
    print(f"  计算输出哈希...")
    out_hashes = compute_hashes(out_frames)
    print(f"  完成 ✅")
    
    # Extract and hash source frames
    src_hashes_list = []
    for i, src in enumerate(source_videos):
        print(f"\n📺 提取原片 [{i+1}/{len(source_videos)}]: {source_names[i]}...")
        src_frames, _ = extract_frames(src, fps=0.5)
        if len(src_frames) > 0:
            src_hashes = compute_hashes(src_frames)
            src_hashes_list.append(src_hashes)
            print(f"  提取帧数: {len(src_frames)} ✅")
        else:
            src_hashes_list.append(([], []))
            print(f"  ⚠️ 无有效帧")
    
    # Compare
    print(f"\n🔍 比较帧相似度...")
    results = find_similar_frames(out_hashes, src_hashes_list, source_names, threshold_d=threshold, threshold_p=threshold)
    
    # Calculate metrics
    total = len(results)
    matched = sum(1 for r in results if r['matched'])
    unique = total - matched
    originality = unique / total * 100
    avg_dist = np.mean([r['avg_dist'] for r in results])
    max_dist = np.max([r['avg_dist'] for r in results])
    min_dist = np.min([r['avg_dist'] for r in results])
    
    # Find top similar segments
    sorted_results = sorted(results, key=lambda x: x['avg_dist'])
    top_matches = [r for r in sorted_results[:10] if r['matched']]
    
    # Generate report
    print(f"\n{'='*60}")
    print(f"📋 查重结果")
    print(f"{'='*60}")
    print(f"\n  原创度: {originality:.1f}%")
    print(f"  搬运比例: {100-originality:.1f}%")
    print(f"  总帧数: {total}")
    print(f"  匹配帧数: {matched}")
    print(f"  独立帧数: {unique}")
    print(f"\n  平均相似度距离: {avg_dist:.2f}")
    print(f"  最小距离: {min_dist:.2f}")
    print(f"  最大距离: {max_dist:.2f}")
    
    if top_matches:
        print(f"\n  🔴 最相似帧 TOP {len(top_matches)}:")
        for i, m in enumerate(top_matches[:5]):
            print(f"    [{i+1}] 距离={m['avg_dist']:.1f} | 匹配: {m['best_src']} 帧{m['src_frame_idx']}")
    
    # Quality assessment
    print(f"\n{'='*60}")
    print(f"📝 质量评估")
    print(f"{'='*60}")
    if originality >= 80:
        print(f"  ✅ 优秀 - 原创度 {originality:.1f}%，风险低")
    elif originality >= 65:
        print(f"  ⚠️ 中等 - 原创度 {originality:.1f}%，建议优化")
    else:
        print(f"  🔴 风险 - 原创度 {originality:.1f}%，建议大幅修改")
    
    # Optimization suggestions
    print(f"\n{'='*60}")
    print(f"💡 优化建议")
    print(f"{'='*60}")
    suggestions = []
    if originality < 80:
        suggestions.append("1. 变速 1.05-1.10x（轻微加速，不影响观感）")
    if originality < 75:
        suggestions.append("2. 裁切画幅（左右各裁 10-20px）")
    if originality < 70:
        suggestions.append("3. 调色（色温+5%，对比度+3%）")
    if originality < 65:
        suggestions.append("4. 镜像翻转（水平翻转）")
    if originality < 60:
        suggestions.append("5. 重新剪辑片段顺序，增加原创片段")
        suggestions.append("6. 添加解说/字幕覆盖（提升原创度）")
    
    if suggestions:
        for s in suggestions:
            print(f"  • {s}")
    else:
        print(f"  ✅ 无需优化，可直接发布")
    
    estimated_after = min(95, originality + 10 + (5 if len(suggestions) > 2 else 0))
    print(f"\n  预计优化后原创度: {estimated_after:.0f}-{min(98, estimated_after+5):.0f}%")
    
    # Return structured results
    return {
        'output_video': output_video,
        'source_videos': source_videos,
        'source_names': source_names,
        'originality': round(originality, 2),
        'copy_ratio': round(100 - originality, 2),
        'total_frames': total,
        'matched_frames': matched,
        'unique_frames': unique,
        'avg_similarity_distance': round(float(avg_dist), 2),
        'top_matches': top_matches[:10],
        'suggestions': suggestions,
        'estimated_after_optimization': round(estimated_after, 1),
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

def main():
    if len(sys.argv) < 3:
        print("用法: python video_checker.py <输出视频> <原片1> [原片2] ...")
        print("\n示例:")
        print('  python video_checker.py "output.mp4" "EP01.mp4" "EP02.mp4" "EP03.mp4"')
        sys.exit(1)
    
    output_video = sys.argv[1]
    source_videos = sys.argv[2:]
    source_names = [f"原片{i+1}: {os.path.basename(v)}" for i, v in enumerate(source_videos)]
    
    # Check files exist
    if not os.path.exists(output_video):
        print(f"❌ 输出视频不存在: {output_video}")
        sys.exit(1)
    
    missing = [v for v in source_videos if not os.path.exists(v)]
    if missing:
        print(f"⚠️ 以下原片不存在:")
        for m in missing:
            print(f"  - {m}")
        source_videos = [v for v in source_videos if v not in missing]
        source_names = [f"原片{i+1}: {os.path.basename(v)}" for i, v in enumerate(source_videos)]
    
    if not source_videos:
        print("❌ 无可用原片")
        sys.exit(1)
    
    # Run check
    result = calculate_originality(output_video, source_videos, source_names)
    
    if result:
        # Save results to JSON
        report_path = os.path.join(os.path.dirname(output_video) or '.', 
                                    os.path.basename(output_video).replace('.mp4', '_查重结果.json'))
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 详细结果已保存: {report_path}")

if __name__ == "__main__":
    main()
