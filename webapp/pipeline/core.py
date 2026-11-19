# -*- coding: utf-8 -*-
"""PipelineProcessor 拆分后共享的常量与基础类型（P1-5）。

原先散在 processor.py 顶部：标点表、切制默认参数、安全边界、下载停滞异常、
链接提取器。processor 与各 mixin 都从这里导入，避免循环依赖。
"""
import re

from job_runtime import JobCancelled

PUNCTUATIONS = ['，', '。', '？', '！', '、', '：', '；', '“', '”', '（', '）', ' ', '\t']

DEFAULT_CUT_CONFIG = {
    'version_count': None,     # None = 自动（集数判定 + 素材容量 clamp）
    'min_dur': 180.0,          # 单版成片下限（s）
    'max_dur': 330.0,          # 单版成片上限（s）≈5.5 分钟
    'reuse_material': False,   # False = 跨版本零复用（版本差异化）
    'watermark': None,         # None = 输出目录账号名
}

CUT_CONFIG_ABS = {
    'version_min': 1, 'version_max': 8,
    'dur_min': 60.0, 'dur_max': 1200.0,
}


CutCancelled = JobCancelled


class DownloadStalled(Exception):
    """下载卡顿：进程存活但长时间无新文件产出（P1 卡顿检测）。"""
    pass


class LinkExtractor:
    """链接提取器"""

    # 分享 ID 允许 -（如 1ytqFIfwwAZyi-u0swd4krQ），漏掉会在 - 处截断成无效链接
    BAIDU_PAN_PATTERN = r'https?://pan\.baidu\.com/s/[a-zA-Z0-9_\-]+(?:\?pwd=[a-zA-Z0-9]+)?'

    @classmethod
    def extract(cls, message):
        """从消息中提取百度网盘链接，支持URL内pwd和正文提取码两种形式"""
        matches = re.findall(cls.BAIDU_PAN_PATTERN, message)

        # 正文里的提取码（"提取码：9999" / "提取码 9999" / "pwd:9999"）
        body_pwd = ''
        m = re.search(r'提取码[:：\s]*([a-zA-Z0-9]{4})', message) \
            or re.search(r'\bpwd[:：=]\s*([a-zA-Z0-9]{4})\b', message, re.I)
        if m:
            body_pwd = m.group(1)

        results = []
        seen = set()

        for match in matches:
            link_id = re.search(r'pan\.baidu\.com/s/([a-zA-Z0-9_\-]+)', match)
            if link_id:
                link_id = link_id.group(1)
                if link_id not in seen:
                    seen.add(link_id)
                    pwd = re.search(r'pwd=([a-zA-Z0-9]+)', match)
                    pwd = pwd.group(1) if pwd else body_pwd
                    results.append({'link': match, 'link_id': link_id, 'pwd': pwd})

        return results
