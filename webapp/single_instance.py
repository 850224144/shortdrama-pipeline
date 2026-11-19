# -*- coding: utf-8 -*-
"""单实例守卫：同一台机器上只允许一个 webapp 进程绑定端口并跑后台任务。

背景：Windows venv 的 python.exe 是 redirector，进程表里天然成对出现
（父=venv、子=基础解释器），这不是双实例；但如果外部脚本/旧 start.bat
又拉起一份，两份都会执行 startup_tasks（续传、恢复批次），导致同一批
任务被提交两遍。这里用 OS 级文件锁做互斥：后来者拿不到锁直接退出。
"""
import os
import sys


def acquire(lock_path):
    """尝试获取独占锁；成功返回文件句柄（需持有到进程结束），失败返回 None。"""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def acquire_or_exit(lock_path, hint=''):
    """拿不到锁说明已有实例在跑，打印后直接退出（退出码 0，避免被守护脚本判失败）。"""
    handle = acquire(lock_path)
    if handle is None:
        print(f'[single_instance] 已有实例在运行，本进程退出（{lock_path}）{hint}')
        sys.exit(0)
    return handle
