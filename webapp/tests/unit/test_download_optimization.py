# -*- coding: utf-8 -*-
"""下载优化 P1-P7 聚焦单测（开发分支验证用，不触碰生产数据）。"""
import os, sys, time, tempfile, threading, shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import processor as proc
from processor import PipelineProcessor, DownloadStalled, CONFIG


class MockLogger:
    def __init__(self):
        self.lines = []
    def log(self, drama, level, msg):
        self.lines.append(f'{drama}|{level}|{msg}')


def make_proc(tmp_data_dir):
    """构造无副作用 PipelineProcessor，data_dir 指向临时目录。"""
    p = PipelineProcessor.__new__(PipelineProcessor)
    p.logger = MockLogger()
    p.state_manager = None
    p.processing = False
    p._job_store = None
    # 备份并指向临时 data_dir（_download_job_store 用 CONFIG['data_dir']/jobs.db）
    p._saved_data_dir = CONFIG['data_dir']
    CONFIG['data_dir'] = tmp_data_dir
    return p


def test_p1_stall():
    """P1: 下载进程存活但长时间无进度 → _run_command_cancellable 抛 DownloadStalled。"""
    p = PipelineProcessor.__new__(PipelineProcessor)
    p.logger = MockLogger()
    p.state_manager = None
    p.processing = False
    p._job_store = None
    cancel_event = threading.Event()  # 不置位，走卡顿分支而非取消分支
    cmd = [sys.executable, '-c', 'import time; time.sleep(60)']
    t0 = time.monotonic()
    try:
        p._run_command_cancellable(
            cmd, timeout=60, cancel_event=cancel_event,
            progress_probe=lambda: time.monotonic() - 100,  # 100s 前最后一次进度 → 卡顿
            stall_timeout=1)
        raise AssertionError('应抛出 DownloadStalled')
    except DownloadStalled as e:
        elapsed = time.monotonic() - t0
        assert elapsed < 10, f'卡顿检出太慢: {elapsed:.1f}s'
        print(f'  P1 OK: {e}（{elapsed:.1f}s 检出）')
    finally:
        cancel_event.set()  # 清理


def test_p3_persistence():
    """P3: 持久化 → 心跳过期 → find_orphaned 找回；finalize 后不再孤儿。"""
    tmp = tempfile.mkdtemp(prefix='dl_p3_')
    p = None
    try:
        p = make_proc(tmp)
        store = p._download_job_store()
        jid = p._persist_download_job(
            {'link': 'https://pan.baidu.com/s/abc?pwd=9999', 'pwd': '9999',
             'link_id': 'abc'}, '测试剧', os.path.join(tmp, '测试剧'))
        assert jid, '持久化应返回 job_id'
        job = store.get(jid)
        assert job['status'] == 'downloading'
        # 新鲜心跳 → 不算孤儿
        assert p.find_orphaned_download_jobs() == [], '新鲜任务不应是孤儿'
        # 置空心跳（stale_jobs 视"从未心跳"为僵死）→ 孤儿
        store.update(jid, heartbeat_at=None)
        orphan = p.find_orphaned_download_jobs()
        assert len(orphan) == 1 and orphan[0]['id'] == jid, f'应找回孤儿: {orphan}'
        # finalize 成功 → 不再孤儿
        p._finalize_download_job(jid, True, None)
        assert p.find_orphaned_download_jobs() == [], 'done 后不应是孤儿'
        assert store.get(jid)['status'] == 'done'
        print('  P3 OK: 持久化→孤儿发现→finalize 全通过')
    finally:
        if p is not None:
            CONFIG['data_dir'] = p._saved_data_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_p4_missing_eps():
    """P4: 网盘/本地集号比对 → 缺失集计算正确。"""
    tmp = tempfile.mkdtemp(prefix='dl_p4_')
    p = None
    try:
        p = make_proc(tmp)
        cloud_names = [f'第{i}集.mp4' for i in range(1, 34)]
        local_dir = os.path.join(tmp, '剧')
        os.makedirs(local_dir, exist_ok=True)
        for i in [1, 2, 4, 5, 9, 12, 13, 23, 27, 30, 31, 33]:
            with open(os.path.join(local_dir, f'第{i}集.mp4'), 'wb') as f:
                f.write(b'x' * (6 * 1024 * 1024))  # >5MB
        cloud_eps = sorted({p.parse_episode_number(os.path.basename(x))
                            for x in cloud_names if p.parse_episode_number(os.path.basename(x)) is not None})
        local_eps = sorted({p.parse_episode_number(os.path.basename(x))
                            for x in os.listdir(local_dir) if p.parse_episode_number(x) is not None})
        missing = sorted(set(cloud_eps) - set(local_eps))
        expected_missing = [3, 6, 7, 8, 10, 11, 14, 15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26, 28, 29, 32]
        assert missing == expected_missing, f'缺失集不符: {missing}'
        print(f'  P4 OK: 云端{len(cloud_eps)}集，本地{len(local_eps)}集，缺失{len(missing)}集')
    finally:
        if p is not None:
            CONFIG['data_dir'] = p._saved_data_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_p6_cleanup():
    """P6: 清理 .downloading 与 <5MB mp4，保留正常文件。"""
    tmp = tempfile.mkdtemp(prefix='dl_p6_')
    p = None
    try:
        p = make_proc(tmp)
        d = os.path.join(tmp, '剧2')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '第1集.mp4.downloading'), 'wb') as f:
            f.write(b'x' * 100)
        with open(os.path.join(d, '第2集.mp4'), 'wb') as f:
            f.write(b'x' * (1024 * 1024))  # 1MB → 损坏
        with open(os.path.join(d, '第3集.mp4'), 'wb') as f:
            f.write(b'x' * (6 * 1024 * 1024))  # 6MB → 保留
        removed = p._clean_partial_downloads(d)
        assert removed == 2, f'应清理2个，实际{removed}'
        assert os.path.exists(os.path.join(d, '第3集.mp4'))
        assert not os.path.exists(os.path.join(d, '第2集.mp4'))
        assert not os.path.exists(os.path.join(d, '第1集.mp4.downloading'))
        print('  P6 OK: 清理 .downloading + <5MB，保留正常集')
    finally:
        if p is not None:
            CONFIG['data_dir'] = p._saved_data_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_p7_gate_logic():
    """P7: 门控条件 —— 明显残缺拦截，完成/达阈值放行。"""
    min_eps = 10
    min_ratio = 0.5
    def gate(downloaded, total, ep_complete):
        incomplete = (ep_complete is False)
        ratio = (downloaded / total) if (incomplete and total) else 1.0
        blocked = incomplete and (downloaded < min_eps or ratio < min_ratio)
        return 'blocked' if blocked else 'pass'
    # 12/33 → blocked（36% < 50%）
    assert gate(12, 33, False) == 'blocked'
    # 25/33 → pass（76% ≥ 50%，≥10集）—— 允许部分产出
    assert gate(25, 33, False) == 'pass'
    # 完整 33/33 → pass
    assert gate(33, 33, True) == 'pass'
    # 8/8 完整短剧 → pass（不误杀完整短剧）
    assert gate(8, 8, True) == 'pass'
    # 9/60 → blocked（<10 集）
    assert gate(9, 60, False) == 'blocked'
    print('  P7 OK: 完整短剧放行/明显残缺拦截/达阈值放行 全部符合')


def test_dedupe_episodes():
    """去重：36集顶层 + 36集嵌套副本（72文件）→ 36集，每集保留顶层最浅路径一份。"""
    p = PipelineProcessor.__new__(PipelineProcessor)
    p.logger = MockLogger()
    p.state_manager = None
    p.processing = False
    p._job_store = None
    top = r'E:\短剧素材\迟来的父爱\迟来的父爱'
    nested = r'E:\短剧素材\迟来的父爱\迟来的父爱\迟来的父爱'
    # 模拟 72 个文件：36 顶层 第N集.mp4 + 36 嵌套 迟来的父爱 - 第N集.mp4
    files = []
    for i in range(1, 37):
        files.append(os.path.join(top, f'第{i}集.mp4'))
        files.append(os.path.join(nested, f'迟来的父爱 - 第{i}集.mp4'))
    assert len(files) == 72
    deduped = p._dedupe_episodes_by_number(files)
    assert len(deduped) == 36, f'应去重为36，实际{len(deduped)}'
    # 每集保留顶层（更浅）路径
    for f in deduped:
        assert f.startswith(top) and '\\' not in os.path.relpath(f, top), f'应保留顶层路径: {f}'
    # 集号 1-36 全覆盖
    eps = sorted(n for n in (p.parse_episode_number(os.path.basename(f)) for f in deduped) if n is not None)
    assert eps == list(range(1, 37)), f'集号应为1-36: {eps}'
    # 不可解析文件保留
    odd = [os.path.join(top, '预告片.mp4')]
    deduped2 = p._dedupe_episodes_by_number(files + odd)
    assert os.path.join(top, '预告片.mp4') in deduped2, '不可解析文件应保留'
    print('  DEDUPE OK: 72→36集，每集保留顶层一份，不可解析文件保留')


if __name__ == '__main__':
    print('=== 下载优化单测 ===')
    test_p1_stall()
    test_p3_persistence()
    test_p4_missing_eps()
    test_p6_cleanup()
    test_p7_gate_logic()
    test_dedupe_episodes()
    print('=== 全部通过 ===')
