# -*- coding: utf-8 -*-
"""为已有成片但缺交付/QC报告的剧补写报告（只读素材、只写 md）。

背景：瘸腿妈妈的光 6 个成片早已渲染完成，但 11/15 23:xx 崩溃后没写交付报告
和 QC 报告，切片档案卡在 cutting。补写报告后可直接上传，无需重渲染。
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBAPP = os.path.join(ROOT, 'webapp')
sys.path.insert(0, WEBAPP)

import processor as P  # noqa: E402

DRAMA = '瘸腿妈妈的光'
ACCOUNT = '福桃说剧'
OUT_DIR = os.path.join(P.CONFIG['output_dir'], ACCOUNT, DRAMA)
SRC_DIR = os.path.join(P.CONFIG['download_base_dir'], DRAMA)


def main():
    logger = P.Logger(os.path.join(ROOT, 'logs'))
    sm = P.StateManager(os.path.join(ROOT, 'data', 'state.json'))
    p = P.PipelineProcessor(logger, sm)

    if not os.path.isdir(OUT_DIR):
        print(f'输出目录不存在：{OUT_DIR}')
        return 1
    files = sorted(glob.glob(os.path.join(OUT_DIR, '*_去重版.mp4')),
                   key=lambda x: int(os.path.basename(x).split('_')[1]))
    if not files:
        print('没有成片，跳过')
        return 1
    best = [{'filename': os.path.basename(f), 'path': f} for f in files]
    print(f'成片 {len(best)} 个，开始补报告')

    p._write_report(OUT_DIR, DRAMA, best,
                    cut_method='历史产物补报告（11/15 崩溃后未写报告）')
    p._generate_qc_report(best, OUT_DIR, DRAMA, src_dir=SRC_DIR)

    for n in (f'{DRAMA}_交付报告.md', f'{DRAMA}_QC报告.md'):
        path = os.path.join(OUT_DIR, n)
        print(('OK   ' if os.path.exists(path) else 'MISS ') + path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
