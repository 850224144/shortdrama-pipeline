# archive/legacy-pipeline/ —— 遗留独立调度流水线（已停用）

这四个脚本是 **webapp 上线之前**的独立调度路径，现在生产全部走 `webapp/`（Flask + dashboard，端口 8765）。
归档而非删除：保留可随时恢复，但**不要再改、不要再跑**。

| 文件 | 原本作用 | 归档原因 |
|---|---|---|
| `auto_cut.py` | 单机定时切片入口 | 与 webapp 的 BatchCutManager 功能重复；`post_process` 不看 returncode、无条件删 raw、无条件写 `已切完.txt`，失败也不补切 |
| `full_pipeline.py` | 下载→切片→上传全链路 | 调 `render_generic.py` 的 CLI 契约不匹配（传 `--source/--output/--drama/--count`，render_generic 只认单个 config.json 位置参数），**`python full_pipeline.py cut` 100% 崩溃** |
| `pipeline_main.py` | 定时扫描新剧并调度 | 只跳过 `completed` 和 `已切完.txt`，**完全不查 `failed`**，永久性失败剧每轮都被重新切片 |
| `gen_reports.py` | 生成交付报告 | 报告宣称的参数（CRF 23 / 29.97fps / 按 V 编号分四角水印）与 `render_generic.py` 实际（crf 22 / fps=None / `mod(t,12)` 时间轮换）不符；硬编码剧名与目录 |

## 共同的根因：输出根目录漂移

这四个脚本用 `D:\短剧工坊\output`，而 `webapp/processor.py` 实际写 `D:\短剧工作区\output`。
两条路径同时存在 = 同一批剧被重复渲染、交付常找漏。

## 如果确实还要用

不要直接跑。至少先修：`post_process` 检查 returncode、`urlretrieve` 加 timeout、
`scan_new_dramas` 把 `failed` 纳入跳过、统一到 `webapp/processor.py` 的输出根目录。
更推荐：在 webapp 里补对应功能，而不是复活这条路径。

## 相关删除（未归档，已 `git rm`）

- `webapp/wechat_uploader_v2.py` —— 全仓零引用，且缺 `login()` 无法独立扫码，成功判定比 v1 更弱（否定式信号 + 无条件 success）
- `data/auto_cut.py` —— 放错目录的早期残稿（83 行），已被 `scripts/auto_cut.py`（187 行）取代
