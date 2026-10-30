# -*- coding: utf-8 -*-
"""历史数据回填：扫描现有来源，补录 drama_archive 剧级档案。

用户需求：历史处理过的短剧 + 状态全部补录（现表为空）。

三源（幂等 upsert，可重复执行）：
- ① 下载：E:\\短剧素材\\<剧名>\\*.mp4 → download_state=done + 集数/文件数/大小/时间
- ② 切片：D:\\短剧工作区\\output\\<平台>\\<剧名>\\edl_config*.json → slice_state=done + 版本数/时间
- ③ 来源：素材\\精选下载列表.csv（剧名匹配已处理剧）→ source_url/source_passwd/rating/category

上传：upload_records 查询时聚合派生（历史无表则不补，状态由聚合给出）。
"""
import os
import sys
import csv
import re
import time

# 使 webapp/archive.py 可导入（脚本在 scripts/ 下）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'webapp'))
import archive  # noqa: E402

DL_BASE = r'E:\短剧素材'
OUTPUT_BASE = r'D:\短剧工作区\output'
CSV_PATH = r'D:\短剧工坊\素材\精选下载列表.csv'


def scan_downloads():
    """扫描 E:\短剧素材：每剧顶层 mp4 数/大小/目录 mtime。"""
    result = {}
    if not os.path.isdir(DL_BASE):
        return result
    for name in sorted(os.listdir(DL_BASE)):
        d = os.path.join(DL_BASE, name)
        if not os.path.isdir(d):
            continue
        mp4s = [f for f in os.listdir(d) if f.lower().endswith('.mp4')]
        if not mp4s:
            continue
        total = sum(os.path.getsize(os.path.join(d, f)) for f in mp4s if os.path.exists(os.path.join(d, f)))
        result[name] = {
            'episode_count': len(mp4s),
            'download_files': len(mp4s),
            'download_size': total // (1024 * 1024),
            'downloaded_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(d))),
        }
    return result


def scan_slices():
    """扫描 output：edl_config*.json 存在 → 切片 done + 版本数/时间。"""
    result = {}
    if not os.path.isdir(OUTPUT_BASE):
        return result
    for root, _dirs, files in os.walk(OUTPUT_BASE):
        cfgs = [f for f in files if f.startswith('edl_config') and f.endswith('.json')]
        if not cfgs:
            continue
        drama = os.path.basename(root)
        mtimes = [os.path.getmtime(os.path.join(root, f)) for f in cfgs]
        result[drama] = {
            'slice_version_count': len(cfgs),
            'sliced_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(max(mtimes))),
        }
    return result


def load_csv_sources():
    """解析 CSV：剧名 → source_url/source_passwd/rating/category。"""
    result = {}
    if not os.path.exists(CSV_PATH):
        return result
    rows = None
    for enc in ('utf-8-sig', 'gbk'):
        try:
            with open(CSV_PATH, encoding=enc) as f:
                rows = list(csv.reader(f))
            break
        except Exception:
            continue
    if not rows:
        return result
    header = rows[0]

    def idx(names):
        for n in names:
            for i, h in enumerate(header):
                if n in h:
                    return i
        return None

    i_name, i_url, i_rating, i_cat = idx(['剧名']), idx(['素材链接']), idx(['评级']), idx(['类目'])
    for row in rows[1:]:
        if not row or i_name is None or i_name >= len(row):
            continue
        name = row[i_name].strip()
        if not name:
            continue
        item = {}
        if i_url is not None and i_url < len(row):
            url = row[i_url].strip()
            if url:
                item['source_url'] = url
                m = re.search(r'[?&]pwd=([A-Za-z0-9]+)', url)
                if m:
                    item['source_passwd'] = m.group(1)
        if i_rating is not None and i_rating < len(row) and row[i_rating].strip():
            item['rating'] = row[i_rating].strip()
        if i_cat is not None and i_cat < len(row) and row[i_cat].strip():
            item['category'] = row[i_cat].strip()
        if item:
            result[name] = item
    return result


def main():
    archive.init_database()
    downloads = scan_downloads()
    slices = scan_slices()
    csv_src = load_csv_sources()

    # ① 下载
    for name, flds in downloads.items():
        archive.upsert_archive(name, download_state='done', **flds)
    # ② 切片
    for name, flds in slices.items():
        archive.upsert_archive(name, slice_state='done', **flds)
    # ③ CSV 来源：仅补已处理剧（下载/切片存在），不插未处理新剧
    processed = set(downloads) | set(slices)
    for name, flds in csv_src.items():
        if name in processed:
            archive.upsert_archive(name, **flds)

    total = archive.search_archive(limit=1000)
    print(f'回填完成：下载 {len(downloads)} 剧 + 切片 {len(slices)} 剧 + CSV 来源匹配 {sum(1 for n in csv_src if n in processed)} 剧')
    print(f'archive 总行数：{len(total)}')
    # 抽样验证：下载/切片/来源各一
    for probe in ('前夫你来迟了', '断情七载，前夫求复合'):
        a = archive.get_archive(probe)
        if a:
            print(f'  抽样[{probe}]: dl={a["download_state"]}({a.get("episode_count")}集) slice={a["slice_state"]}({a.get("slice_version_count")}版) '
                  f'rating={a.get("rating")} cat={a.get("category")} url={"有" if a.get("source_url") else "无"}')


if __name__ == '__main__':
    main()
