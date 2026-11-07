# -*- coding: utf-8 -*-
"""按业务域拆分的 Flask Blueprint 集合（阶段一 4.4）。

每个模块暴露 ``create_<域>_bp(deps)`` 工厂：蓝图不自行实例化 manager / service，
所需组件由 app.py 构造后通过 ``deps`` 注入，避免循环 import。路由函数体与拆分前
保持一致，仅把对 app 模块级全局对象的引用改为闭包捕获的注入依赖。
"""
