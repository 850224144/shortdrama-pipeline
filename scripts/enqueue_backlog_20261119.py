"""把 6 部无档案剧的链接补进待跑队列（补跑口径按建议默认执行）。"""
import os
import sys

sys.path.insert(0, r'D:\短剧工坊\webapp')
from pending_queue import add_pending, load_pending

ITEMS = [
    ('https://pan.baidu.com/s/1ltU-Km7mLKH__RI9ZF6JpA', '扶人反被讹诈'),
    ('https://pan.baidu.com/s/1jFhudKtwLPlDDr9zj7o0-A', '悔赶持家婆'),
    ('https://pan.baidu.com/s/1jSGh7QVAGD_G_ZMhlY3eCg', '红酒覆纱仇'),
    ('https://pan.baidu.com/s/1wKpIArWggq8iEGAzKmnMTg', '亲情止损'),
    ('https://pan.baidu.com/s/1r4OJWDmwlakw6XQE-IxVZA', '卤味夫妻甜日子'),
    ('https://pan.baidu.com/s/12H8QdzxsjW35_pkc-GksAQ', '错嫁后我怀了三宝'),
]

items = []
for link, name in ITEMS:
    lid = link.rsplit('/s/', 1)[-1]
    items.append({'link': link, 'link_id': lid, 'pwd': '9999',
                  'drama_name': name, 'msg': f'{link}?pwd=9999'})

added = add_pending(items)
print('新增', added, '条')
batch = load_pending() or {}
rows = batch.get('links', [])
print('队列总数', len(rows))
for r in rows:
    print(' ', r.get('status'), r.get('drama_name') or r.get('link_id'))
