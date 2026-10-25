#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ASR转写脚本 - 接受目录参数
用法: python transcribe_dir.py <素材目录>
"""
import os
import sys
import json
import re
from pathlib import Path

# CUDA DLL路径
_nv_base = Path(r"C:\Users\86159\AppData\Local\Programs\Python\Python311\Lib\site-packages\nvidia")
_nv_paths = []
for _sub in ["cublas/bin", "cudnn/bin", "cuda_nvrtc/bin"]:
    _p = _nv_base / _sub
    if _p.exists():
        _nv_paths.append(str(_p))
os.environ["PATH"] = os.pathsep.join(_nv_paths) + os.pathsep + os.environ.get("PATH", "")
for _p in _nv_paths:
    os.add_dll_directory(_p)

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from faster_whisper import WhisperModel

MODEL_PATH = r"D:\models\models\keepitsimple--faster-whisper-large-v3\snapshots\master"

def find_eps(drama_dir):
    """找到所有mp4，按集数排序"""
    files = list(drama_dir.glob("*.mp4"))
    def ep_num(f):
        m = re.search(r'(\d+)', f.stem)
        return int(m.group(1)) if m else 0
    files.sort(key=ep_num)
    return files

def transcribe_ep(model, video_path):
    segments, info = model.transcribe(
        str(video_path),
        language="zh",
        beam_size=5,
        vad_filter=True,
    )
    result = []
    for seg in segments:
        result.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        })
    return result

def main():
    if len(sys.argv) < 2:
        print("用法: python transcribe_dir.py <素材目录>")
        sys.exit(1)
    
    src_dir = Path(sys.argv[1])
    if not src_dir.exists():
        print(f"目录不存在: {src_dir}")
        sys.exit(1)
    
    print(f"加载模型 {MODEL_PATH} (cuda/float16)...")
    model = WhisperModel(MODEL_PATH, device="cuda", compute_type="float16")
    
    out_dir = src_dir / "transcribe"
    out_dir.mkdir(exist_ok=True)
    full_txt = out_dir / "full_transcript.txt"
    
    eps = find_eps(src_dir)
    print(f"\n=== {src_dir.name}: {len(eps)}集 ===")
    
    all_lines = []
    for i, ep in enumerate(eps, 1):
        # 检查是否已转写
        ep_json = out_dir / f"EP{ep.stem}.json"
        if ep_json.exists():
            print(f"  [{i}/{len(eps)}] {ep.name} - 已存在")
            with open(ep_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
            all_lines.extend(data)
            continue
        
        print(f"  [{i}/{len(eps)}] 转写 {ep.name}...")
        try:
            data = transcribe_ep(model, ep)
            with open(ep_json, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            all_lines.extend(data)
            print(f"    ✓ {len(data)}条")
        except Exception as e:
            print(f"    ✗ 失败: {e}")
    
    # 写入完整转录
    with open(full_txt, 'w', encoding='utf-8') as f:
        for line in all_lines:
            f.write(f"[{line['start']:.1f}-{line['end']:.1f}] {line['text']}\n")
    
    print(f"\n完成: {len(all_lines)}条 -> {full_txt}")

if __name__ == '__main__':
    main()