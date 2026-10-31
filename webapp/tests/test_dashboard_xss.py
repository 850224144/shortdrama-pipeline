# -*- coding: utf-8 -*-
"""R7 前端安全静态门禁：dashboard.html 内数据驱动 HTML 不得插入未转义文本。

用户/远端文本（剧名、文件名、日志消息、错误信息）进入 innerHTML 前必须经
escapeHtml() 转义，防止 DOM XSS。此测试扫描模板字面量插值，拒绝「裸标识符/
点路径」类插值（如 ${task.drama_name}、${log.message}），这些是最常见的注入点。
"""

import os
import re
import unittest

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(sys_path, 'templates', 'dashboard.html')

# 允许的插值：含 escapeHtml(...)，或非裸标识符（如三元/拼接/字面量）。
# 裸标识符/点路径（如 task.drama_name、log.message、item.name）必须转义。
BARE = re.compile(r'^[\w$]+(?:\.[\w$]+)*$')


def _script_blocks(html):
    return re.findall(r'<script[^>]*>([\s\S]*?)</script>', html)


def _innerhtml_interpolations(js):
    """返回 (line_no, interpolation_text) 列表：innerHTML 模板里的 ${...} 插值。"""
    out = []
    for m in re.finditer(r'(?:innerHTML|insertAdjacentHTML|outerHTML)\s*=\s*(?:[^;]*`[^`]*`)', js):
        block = m.group(0)
        # 找到该赋值中的模板字面量，逐个提取 ${...}（忽略嵌套反引号，项目未用嵌套）
        tmpl = re.search(r'`([^`]*)`', block)
        if not tmpl:
            continue
        line = js[:m.start()].count('\n') + 1
        for i in re.finditer(r'\$\{([^{}]*)\}', tmpl.group(1)):
            out.append((line, i.group(1).strip()))
    return out


class DashboardXssTests(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(os.path.isfile(HTML), f'缺少 {HTML}')

    def test_no_bare_identifier_interpolation_in_innerhtml(self):
        with open(HTML, encoding='utf-8') as f:
            html = f.read()
        bad = []
        for js in _script_blocks(html):
            for line, expr in _innerhtml_interpolations(js):
                if 'escapeHtml(' in expr:
                    continue
                if BARE.match(expr):
                    bad.append((line, expr))
        self.assertEqual(bad, [], f'innerHTML 中存在未转义插值: {bad}')

    def test_escape_html_helper_exists(self):
        with open(HTML, encoding='utf-8') as f:
            html = f.read()
        self.assertGreaterEqual(html.count('function escapeHtml('), 1,
                                'dashboard.html 缺少 escapeHtml 辅助函数')


if __name__ == '__main__':
    unittest.main()
