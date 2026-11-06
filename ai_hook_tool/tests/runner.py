# -*- coding: utf-8 -*-
"""Windows pytest 静默取证 runner：subprocess 独立调用，stdout/stderr/rc/junit 分别落盘。

用法：python tests/runner.py
"""
import glob
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(MODULE_ROOT, "tests")
OUT_DIR = os.path.join(MODULE_ROOT, "output")
os.makedirs(OUT_DIR, exist_ok=True)


def main() -> int:
    files = sorted(glob.glob(os.path.join(TEST_DIR, "test_*.py")))
    if not files:
        print("无测试文件")
        return 0

    xml_path = os.path.join(OUT_DIR, "pytest_junit.xml")
    stdout_path = os.path.join(OUT_DIR, "pytest_stdout.txt")
    stderr_path = os.path.join(OUT_DIR, "pytest_stderr.txt")

    cmd = [sys.executable, "-m", "pytest", *files, "-q", "-rf",
           f"--basetemp={os.path.join(OUT_DIR, 'pytest_basetemp')}",
           f"--junitxml={xml_path}"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=MODULE_ROOT)

    for path, text in ((stdout_path, proc.stdout), (stderr_path, proc.stderr)):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")

    print(proc.stdout)
    print(proc.stderr)
    print(f"[runner] returncode={proc.returncode}")

    failures = []
    if os.path.exists(xml_path):
        tree = ET.parse(xml_path)
        for tc in tree.iter("testcase"):
            for node in list(tc):
                if node.tag in ("failure", "error"):
                    failures.append((tc.get("classname"), tc.get("name"),
                                     (node.text or "")[:200]))
    else:
        print("[runner] 警告：junit xml 未生成，需单独复跑")

    if failures:
        print("\n=== 失败清单 ===")
        for cls, name, body in failures:
            print(f"- {cls}::{name}\n  {body}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
