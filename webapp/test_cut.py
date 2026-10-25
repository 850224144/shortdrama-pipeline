#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试切片脚本 - 跳过下载，直接使用现有素材切片
"""
import sys
import os
import json
import time
from datetime import datetime

# 添加webapp目录到路径
sys.path.insert(0, r'D:\短剧工坊\webapp')

from processor import PipelineProcessor, Logger, StateManager, CONFIG

# 测试配置
TEST_Drama = '馒头姑娘旺翻沈家'
TEST_SrcDir = r'D:\短剧素材\馒头姑娘旺翻沈家\馒头姑娘旺翻沈家'

def main():
    print('='*60)
    print('短剧工坊 - 切片测试')
    print('='*60)
    print(f'素材目录：{TEST_SrcDir}')
    print(f'输出目录：{CONFIG["output_dir"]}')
    print()
    
    # 检查素材
    if not os.path.exists(TEST_SrcDir):
        print(f'错误：素材目录不存在 {TEST_SrcDir}')
        return
    
    mp4_files = [f for f in os.listdir(TEST_SrcDir) if f.endswith('.mp4')]
    print(f'找到 {len(mp4_files)} 个MP4文件')
    print(f'文件列表：{sorted(mp4_files)}')
    print()
    
    # 创建日志记录器
    logger = Logger(CONFIG['log_dir'])
    
    # 创建状态管理器
    state_manager = StateManager(CONFIG['state_file'])
    
    # 清除该剧的状态（允许重新测试）
    state = state_manager.load()
    state['completed'] = [c for c in state.get('completed', []) if c.get('name') != TEST_Drama]
    state['failed'] = [f for f in state.get('failed', []) if f.get('name') != TEST_Drama]
    state_manager.save(state)
    
    # 删除已切完标记
    marker_file = os.path.join(TEST_SrcDir, '已切完.txt')
    if os.path.exists(marker_file):
        os.remove(marker_file)
        print(f'已删除标记文件')
    
    print(f'已清除 {TEST_Drama} 的状态')
    
    # 创建处理器
    processor = PipelineProcessor(logger, state_manager)
    
    # 模拟下载结果
    download_result = {
        'success': True,
        'link_id': TEST_Drama,
        'downloaded_files': [os.path.join(TEST_SrcDir, f) for f in mp4_files],
        'target_dir': TEST_SrcDir,
        'error': None
    }
    
    print('开始切片...')
    print('-'*60)
    
    start_time = time.time()
    
    try:
        # 调用切片函数
        cut_result = processor._cut_once(download_result)
        
        elapsed = time.time() - start_time
        print(f'\n耗时：{elapsed:.1f}秒')
        print('='*60)
        
        if cut_result['success']:
            print('✅ 切片成功！')
            print(f'输出目录：{cut_result["output_dir"]}')
            
            # 检查输出文件
            output_dir = cut_result['output_dir']
            if output_dir and os.path.exists(output_dir):
                outputs = os.listdir(output_dir)
                print(f'\n输出文件 ({len(outputs)}个):')
                for f in sorted(outputs):
                    filepath = os.path.join(output_dir, f)
                    if os.path.isfile(filepath):
                        size = os.path.getsize(filepath)
                        print(f'  {f} ({size/1024/1024:.1f}MB)')
        else:
            print(f'❌ 切片失败：{cut_result["error"]}')
            
    except Exception as e:
        print(f'\n❌ 异常：{e}')
        import traceback
        traceback.print_exc()
    
    print('='*60)
    print('测试完成')

if __name__ == '__main__':
    main()