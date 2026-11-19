# -*- coding: utf-8 -*-
"""等主流水线空闲后，把 11/19 剩余 12 部剧一次性提交（只提交一次）。

用途：当前任务（三年婚姻续传+切片）跑完后自动接着跑积压剧集，
避免 409 忙线导致链接丢失。轮询 /api/status，见到 processing=false 才提交。
"""
import json
import sys
import time

import urllib.request

BASE = 'http://127.0.0.1:8765'
MAX_WAIT_SEC = 72 * 3600

LINKS = [
    # 素材已就绪，直接切片
    'https://pan.baidu.com/s/1cLBRqJOKLUhgvv6jCyYaDQ?pwd=9999',   # 重生后我不当后妈
    'https://pan.baidu.com/s/1DA7cOY9KI-lv7u78LP-sOg?pwd=9999',   # 婆婆撑腰我翻身
    'https://pan.baidu.com/s/1iQ-HAahh1-hdJagzFM2vpQ?pwd=9999',   # 我生娃被明码标价
    'https://pan.baidu.com/s/1CaCXK4rWL3EBSV8RxlYOuA?pwd=9999',   # 婚礼为难我，我直接换新郎
    'https://pan.baidu.com/s/1rQKYreXSvv3Oa6uBU7XPHQ?pwd=9999',   # 八年付出换来背叛
    'https://pan.baidu.com/s/1ytqFIfwwAZyi-u0swd4krQ?pwd=9999',   # 瘸腿妈妈的光（素材在，补交付）
    # 从未开始：需下载
    'https://pan.baidu.com/s/1n92zWcYRVnndf65_D8o4DQ?pwd=9999',   # 一声求救一场局
    'https://pan.baidu.com/s/1c5gUxWJxBrkRykLQs2ntZQ?pwd=9999',   # 真假千金认亲
    # 素材实体已丢（archive 记 done 但目录空）：重下后重切
    'https://pan.baidu.com/s/1ZeD6KcwhhUF1JLkMGjkKxA?pwd=9999',   # 秘书的坏账我来算
    'https://pan.baidu.com/s/1uPsdNe7_FLI9HRtBF5Obqw?pwd=9999',   # 婆婆寿宴替我撑腰
    'https://pan.baidu.com/s/1WElUNQ8HYCROoEL6aQXL5w?pwd=9999',   # 广场舞赴余生
    'https://pan.baidu.com/s/10ocv9TlzEVjdDp8ACKNirQ?pwd=9999',   # 失忆婆婆未忘家
]


def status():
    with urllib.request.urlopen(BASE + '/api/status', timeout=15) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    t0 = time.time()
    while time.time() - t0 < MAX_WAIT_SEC:
        try:
            st = status()
        except Exception as e:
            print(f'[watch] status 失败：{e}', flush=True)
            time.sleep(60)
            continue
        if not st.get('processing'):
            break
        task = (st.get('current_task') or {})
        print(f'[watch] 忙：{task.get("message")} '
              f'{task.get("progress")}% job={task.get("job_id")}', flush=True)
        time.sleep(120)
    else:
        print('[watch] 超时未等到空闲，放弃提交', flush=True)
        return 1

    msg = '\n'.join(LINKS)
    data = json.dumps({'message': msg}).encode('utf-8')
    req = urllib.request.Request(BASE + '/api/process', data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode('utf-8')
        print('[watch] 提交结果：' + body, flush=True)
    except urllib.error.HTTPError as e:
        print(f'[watch] 提交失败 HTTP {e.code}: {e.read().decode("utf-8")}',
              flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
