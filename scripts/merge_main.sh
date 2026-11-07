#!/usr/bin/env bash
# 本地兜底：验证通过后自动合并开发分支到 main（远程 Actions 不可用时用）。
#
# 用法：bash scripts/merge_main.sh
# 流程：确认在开发分支 → 全量测试（验证门禁）→ ff-only 合并 main → 推送（绕过代理）→ 切回开发分支。
# 安全：测试失败或 main 有并发新提交（ff-only 失败）则不合并，不改写历史。
set -euo pipefail

cd "$(dirname "$0")/.."

DEV_BRANCH="develop"
PYEXE="C:/Users/86159/AppData/Local/Programs/Python/Python311/python.exe"

echo "== 当前分支 =="
CUR=$(git branch --show-current)
echo "当前: $CUR"
if [ "$CUR" != "$DEV_BRANCH" ]; then
  echo "❌ 请在开发分支 $DEV_BRANCH 上运行（当前 $CUR）" >&2
  exit 1
fi

echo "== 检查工作区 =="
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠️  工作区有未提交改动（可能阻碍分支切换）：" >&2
  git status --short | head -20 >&2
  read -r -p "仍有未提交改动，继续合并？（改动会带到 main，如无冲突才成功）[y/N] " ans
  if [ "${ans:-N}" != "y" ]; then
    echo "已取消" >&2
    exit 1
  fi
fi

echo "== 验证门禁：全量测试 =="
if ! (cd webapp && "$PYEXE" -m unittest discover -s tests -p "test_*.py" 2>&1 | tail -3); then
  echo "❌ 测试失败，不合并" >&2
  exit 1
fi

echo "== ff-only 合并到 main =="
git checkout main
git merge --ff-only "$DEV_BRANCH"
git -c http.proxy= -c https.proxy= push origin main
echo "✅ 已推送 origin/main"

echo "== 切回开发分支 =="
git checkout "$DEV_BRANCH"
echo "✅ 完成：$DEV_BRANCH -> main 已合并并推送"
