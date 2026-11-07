# -*- coding: utf-8 -*-
"""服务启动后台钩子（阶段一 4.4 从 app.py 抽出）。

每个函数接收所需组件作为参数，由 app.py 在装配阶段起 daemon 线程调用；
不自行实例化 manager / service，避免循环 import。
"""
import time
import threading

from artifact_cleanup import STRATEGY_RECYCLE


def start_cut_reconcile(batch_cut_manager, logger):
    """启动状态对账：恢复中断任务、修复错误 running、标记缺成片/可恢复现场。"""
    def _run():
        try:
            report = batch_cut_manager.reconcile()
            if report['changed']:
                logger.log('system', 'INFO', f'切片对账：{report}')
        except Exception as e:
            logger.log('system', 'ERROR', f'切片对账失败: {e}')

    threading.Thread(target=_run, daemon=True).start()


def start_download_resume(processor, process_controller, logger, CONFIG):
    """P3: 启动恢复崩溃遗留的下载任务（心跳过期且 status=downloading 的自动续传）。"""
    def _run():
        try:
            orphan_jobs = processor.find_orphaned_download_jobs()
            if not orphan_jobs:
                return
            for _ in range(2):
                job = orphan_jobs[0]
                params = job.get('params') or {}
                link = params.get('link')
                if not link:
                    return
                # 失败任务自动续传：记录次数，防永久失效源无限重试。
                if job.get('status') == 'failed':
                    processor.bump_download_auto_resume(job.get('id'))
                pwd = params.get('pwd') or CONFIG['download'].get('default_pwd', '9999')
                msg = link if 'pwd=' in link else f"{link}?pwd={pwd}"
                logger.log('system', 'WARN', f'启动恢复：续传中断下载 {link}')
                body, status = process_controller.start(msg)
                # start 成功返回 202（Accepted），非 200；判 <300 才算接受，
                # 否则每次启动恢复都误报「被拒」并多余重试一次。
                if isinstance(status, int) and status < 300:
                    logger.log('system', 'INFO', f'启动恢复：已提交续传任务')
                    return
                logger.log('system', 'WARN',
                           f'启动恢复：续传被拒（{status}），{20}s后重试')
                time.sleep(20)
        except Exception as e:
            logger.log('system', 'ERROR', f'启动下载恢复失败: {e}')

    threading.Thread(target=_run, daemon=True).start()


def start_logs_cleanup(logger):
    """后台周期性清理过期日志。"""
    def _run():
        while True:
            time.sleep(3600 * 24)  # 每小时检查一次
            deleted = logger.cleanup_logs(30)
            if deleted > 0:
                logger.log('system', 'INFO', f'清理了{deleted}个过期日志文件')

    threading.Thread(target=_run, daemon=True).start()


def start_cleanup_sweep(artifact_cleanup, logger):
    """服务启动补扫：上次进程结束前已标记完成且启用自动清理、但清理未落地的批次。

    处理「服务重启后自动清理不会执行」的缺口（#4）：后端 worker 在批次自然结束时
    同步执行清理，正常不会遗留；仅当进程在「标记完成」与「清理执行」之间崩溃时，
    此补扫在下一次启动时补跑。execute_cleanup 幂等（已回收的路径不会重复移动）。
    """
    def _run():
        try:
            for batch_id in artifact_cleanup.list_startup_cleanup_batches():
                try:
                    logger.log('system', 'INFO', f'启动补扫：批次 {batch_id} 补跑自动清理')
                    artifact_cleanup.execute_cleanup(batch_id, strategy=STRATEGY_RECYCLE)
                except Exception as e:
                    logger.log('system', 'ERROR', f'启动补扫：批次 {batch_id} 清理失败: {e}')
        except Exception as e:
            logger.log('system', 'ERROR', f'启动补扫失败: {e}')

    threading.Thread(target=_run, daemon=True).start()
