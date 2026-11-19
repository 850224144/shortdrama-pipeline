"""第一版完整流程（P1–P4）。旧 `hooktool.cli` 命令保持不变，新流程只通过
`python -m hooktool.flow_cli` 与 `/api/hooks` 显式启用。
"""

FLOW_VERSION = "1.0.0"

__all__ = ["FLOW_VERSION"]
