# -*- coding: utf-8 -*-
import json
with open(r'D:\短剧工坊\data\dramaid_cache.json','r',encoding='utf-8') as f:
    cache = json.load(f)
keywords = ['赶我出门', '救人先护己', '恶霸抢我', '桔园', '绝后', '前夫却']
for k in keywords:
    print(f'=== 搜 {k} ===')
    for norm, entries in cache['dramas'].items():
        if k in norm:
            for e in entries:
                print(f"  {e['raw_name']} | ID: {e['drama_id'][:60]} | 状态: {e['is_published']}")
