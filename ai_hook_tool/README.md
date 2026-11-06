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

## 依赖

ffmpeg/ffprobe（路径见 `config/settings.json`）；PIL（测试拼图用，非主链路必需）；
faster-whisper 可选（无已有转写时才需要，缺失记 PENDING，不阻断）。
