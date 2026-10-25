import os
import re

base_dir = 'D:/短剧素材/恶霸占我橘山'
seen = set()
to_delete = []

for root, dirs, files in os.walk(base_dir):
    for f in files:
        if f.endswith('.mp4') and not f.endswith('.downloading'):
            match = re.search(r'第(\d+)集', f)
            if match:
                ep_num = int(match.group(1))
                if ep_num <= 45:
                    if ep_num not in seen:
                        seen.add(ep_num)
                    else:
                        to_delete.append(os.path.join(root, f))
                else:
                    to_delete.append(os.path.join(root, f))

print(f'Found {len(seen)} unique episodes (1-45)')
print(f'To delete: {len(to_delete)} files')
for f in to_delete:
    print(f'  DELETE: {f}')
    os.remove(f)
    print(f'  DELETED: {f}')

# 删除空的嵌套目录
for root, dirs, files in os.walk(base_dir, topdown=False):
    if not files and not dirs:
        try:
            os.rmdir(root)
            print(f'  Removed empty dir: {root}')
        except:
            pass

print('Done!')
