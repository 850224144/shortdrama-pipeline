# AI 短剧钩子生成工具（feature/ai-hook-tool）

换一部剧也能产出符合平台要求的 ≥30s AI 短剧钩子成片。脚本负责准备/归集/校验/拼接/落盘；
锁脸出图、图生视频在豆包网页版人工完成，片段落槽位后由脚本收集。

## 链路

```
preflight → script → （网页版人工：Seedream 锁脸图 → Seedance Mini 单段 10s ×3 段 → 下载落 segments）→ collect → assemble → gate
```

> 免费档 Seedance 2.0 Mini 单段上限 10s；3 段 ×10s 本地 FFmpeg 拼接凑 ≥30s。
> 会员档才支持单段 15s/30s 与 Seedance 2.5，后期 API 化时只换生成适配器。

## 运行（Windows，Python 3.11）

```powershell
cd D:\短剧工坊\ai_hook_tool
python -m hooktool.cli preflight --src "E:\短剧素材\娇妻婆婆为我挡风雨"
python -m hooktool.cli script    --src "E:\短剧素材\娇妻婆婆为我挡风雨"
# 可选文本规划（需在 D:\短剧工坊\.env 配好 ZHIPU_API_KEY 或 SENSENOVA_API_KEY）
python -m hooktool.cli script --src "E:\短剧素材\娇妻婆婆为我挡风雨" --planner llm
python -m hooktool.cli collect
python -m hooktool.cli assemble
python -m hooktool.cli gate --src "E:\短剧素材\娇妻婆婆为我挡风雨" --similarity 0.6
```

不在模块根目录时，先设 `$env:PYTHONPATH="D:\短剧工坊\ai_hook_tool"` 再执行上述命令。

## 目录

- `config/`：合规阈值、题材词表、冲突/悬念词、ffmpeg/画幅/标注样式（外置，不写死）。
- `hooktool/`：纯函数与流水线模块，不 import 工坊 `processor.py` 大类。
- `workdir/`：运行产物（素材库/脚本分镜/AI 片段槽位 `前核15s`·`后钩子15s`·扁平 `segments`/成片/合规校验），已 gitignore。
- `tests/`：纯函数、素材扫描、三态闸、拼接校验单测。

## 合规口径

- 硬阻断：片段缺失、时长 <30s、纯动物钩子未接正剧、相同度 <0.5/跨题材。
- 软告警：脸不一致、角标压失败——不中断、照常产出，记 WARN 不算通过。
- PENDING：缺定妆参照、题材未命中、相同度/脸一致性待人工复核、音字幕未实测。

## P0–P4 完整流程（新入口，不影响旧链路）

`hooktool.cli` 的 `preflight → script → collect → assemble → gate` 保持原契约不变；
完整流程走独立入口 `hooktool.flow_cli`，状态存于新项目目录
`workdir/projects/<uuid>/project.sqlite`，不读取也不改写旧工作区。

```powershell
python -m hooktool.flow_cli project --src "E:\短剧素材\剧A" --episode "第1集.mp4" --confirm-first-episode --transcript "第1集.srt" --confirm-transcript
python -m hooktool.flow_cli analyze  --project "<project_dir>"
python -m hooktool.flow_cli propose  --project "<project_dir>"
python -m hooktool.flow_cli proposals --project "<project_dir>"
python -m hooktool.flow_cli plan-review --project "<project_dir>" --plan plan_xxx --decision approved --reviewer 人工A --facts-reviewed
python -m hooktool.flow_cli generation --project "<project_dir>" --plan plan_xxx
python -m hooktool.flow_cli import-root --project "<project_dir>" --dir "E:\AI生成片段"
python -m hooktool.flow_cli import --project "<project_dir>" --task task_xxx --file "E:\AI生成片段\shot1.mp4"
python -m hooktool.flow_cli assemble --project "<project_dir>" --plan plan_xxx
python -m hooktool.flow_cli render  --project "<project_dir>" --timeline tl_xxx
python -m hooktool.flow_cli qc      --project "<project_dir>" --timeline tl_xxx
python -m hooktool.flow_cli final-review --project "<project_dir>" --timeline tl_xxx --decision approved --reviewer 人工A
python -m hooktool.flow_cli export  --project "<project_dir>" --timeline tl_xxx
```

- 首集必须显式确认，第 2 集不会被静默当成 E01；字幕与源片哈希绑定。
- 方案审批绑定 `plan_revision_id + semantic_hash + facts_reviewed`；剧情相关编辑生成新版本并让旧审批失效。
- 预演只作内部粗剪；正式交付必须通过来源/叙事/生成/交付四层质检与最终人工验收。
- 本流程不调用付费生成接口、不外传素材、不发布；生成任务只输出任务包与提示词。

Web 侧由 `webapp/api/hooks.py` 提供 `/api/hooks` 蓝图（长任务 202 + `job_id`，冲突 409，内容不满足 422）。

## 依赖

ffmpeg/ffprobe（路径见 `config/settings.json`）；PIL（测试拼图用，非主链路必需）；
faster-whisper 可选（无已有转写时才需要，缺失记 PENDING，不阻断）。
