# 首集短剧钩子策划：架构、需求与开发设计

## 1. 目标与范围

### 目标

给一部新剧时，只分析**第一集**，从第一集可核实的对白、时间码和剧本上下文中挑选钩子素材，产出可审核、可追溯的约 30 秒短视频策划方案，并可沿用现有人工视频生成、片段收集、拼接和合规检查流程。

核心原则：

- 第一集正片是事实来源；其他集不做 ASR、剧情分析或候选搜索。
- ASR 提供实际对白和时间码。剧本只补充角色、场景、动作，且对白须与 ASR 对齐。
- 简介仅帮助理解题材和长线背景，不得补成本集台词、动作或结局证据。
- 先输出可供人确认的方案，再进入生成和拼接；证据不足时标记待审核或阻断，不编造素材凑数。
- 规则模式默认运行且不请求外部 LLM；LLM 仅作为显式启用的辅助规划器。

### 不在本期范围

- 不扫描全剧剧情来寻找最佳钩子，不调用后续集补反转。
- 不自动理解整段视频的视觉内容或保证生成画面与原片一致。
- 不自动生成最终视频；当前豆包网页版生成仍由人工完成，工具负责准备、收集、拼接和校验。
- 不承诺每部剧一定能选出合格 30 秒钩子。素材不足时应返回明确原因。

## 2. 用户需求与输入输出

### 必需输入

- 新剧素材目录。
- 至少一段可识别为第一集的视频。

### 可选输入

- 第一集剧本或字幕/转写文件。
- 剧情简介（只作背景）。
- 角色参考图（用于后续生成时的角色映射）。
- 用户指定题材、成片目标时长及平台要求；未提供时使用项目配置默认值。

### 用户可见输出

1. 第一集素材检查结果：选中的首集文件、转写状态、剧本/角色图/简介是否可用。
2. 钩子候选及选择理由：ASR 原句、源时间、题材信号、剧本对齐信息。
3. 分段分镜草案：开场、冲突/升级、后钩子；每镜包含对白、源时间、角色/场景/动作证据和生成提示词。
4. QC 与待人工确认项：例如 ASR 识别疑问、角色参考图缺失、闪回、跨场景、候选时间覆盖不足。
5. 若进入制作：生成片段槽位、拼接视频和最终 PASS/FAIL/PENDING/WARN 报告。

## 3. 目标架构

```text
素材目录
  │
  ▼
第一集素材扫描与预检
  │  只取最小集号的视频；其余集仅允许文件名级识别，不解码、不转写
  ▼
第一集 ASR / 已有字幕与转写
  │  每条对白保留原始转写行索引、起止时间、ASR 文本
  ├──────── 第一集剧本解析 ──对白对齐后补角色/场景/动作
  ├──────── 简介/题材识别 ──只作背景与排序辅助
  ▼
候选生成与排序（规则默认）
  │  显式 --planner llm 时，LLM 只能重排/标注已提供候选
  ▼
证据校验 + 钩子结构规划
  │  开场（首 3 秒证据）→ 冲突/升级 → 后钩子
  ▼
人工审核与候选确认（建议新增的显式关口）
  ▼
分镜 manifest / 生成提示词
  ▼
人工生成视频片段 → collect → assemble → gate
```

### 架构边界

- **素材层**负责选出第一集和辅助文件，不承担剧情判断。
- **证据层**负责 ASR 时间码和剧本对齐；不得让模型创作新的事实。
- **策划层**负责候选排序、结构建议和镜头拍法；规则是默认路径，LLM 是可选路径。
- **审核层**确认选段、身份/动作/场景连续性和内容一致性；当前 CLI 的人工审核输入较零散，建议形成可保存的审核记录。
- **制作层**继续沿用网页生成、片段收集、FFmpeg 拼接和现有合规门禁。

## 4. 现状与目标设计的区分

| 能力 | 当前实现 | 目标/后续设计 |
|---|---|---|
| 第一集选择 | `material.scan_material()` 按视频文件名解析集号并取最小集号；ASR 只处理该文件 | 保持该边界。目录扫描可读取文件名以识别首集，不读取/分析其他集媒体内容 |
| ASR | 复用目录里的转写文件，否则尝试 faster-whisper；首集转写缓存为 `transcript_E01.txt` | 保存模型/转写来源、时间码和原始行 ID，避免缓存被误当成其他版本素材 |
| 剧本辅助 | 解析文本、对齐对白；未对齐台词不作为源对白 | 继续坚持 ASR 为事实来源；扩展格式时需有对齐覆盖率与未匹配原因 |
| 题材与排序 | 通用词 + 题材词配置；规则排序 | 规则结果可解释；题材只影响候选排序，不能成为剧情事实 |
| LLM 规划 | `--planner llm` 可复用 GLM→SenseNova；两阶段逻辑调用：候选建议、受控拍法；规则模式不触网 | 保持显式启用、有限字段/候选 ID 校验、失败回退；不向模型发送整集视频 |
| 视觉分析 | 未实现，manifest 标记 `visual_analysis: not_performed` | 第一阶段由人工看原片核对；后续可生成候选时间点联系表，不把它描述成自动视觉理解 |
| 分镜与 QC | 生成前核/后钩子分组、时间码、证据字段和硬阻断 | 增加可编辑/可审阅的候选计划与用户确认状态，避免直接把草案视为已批准 |
| 视频生产 | 网页生成由人工操作；collect/assemble/gate 自动化 | 维持现状。生成服务 API 化单独立项，不纳入首集选段核心需求 |

当前主要代码位置：`hooktool/material.py::scan_material`、`transcribe.py::load_or_transcribe`、`script_source.py::align_dialogues`、`quotes.py::rank_quotes`、`hook_planner.py::plan_candidates/suggest_camera`、`script.py::build_script`、`collect.py::collect`、`assemble.py::run_assemble`、`gate.py::evaluate`。

## 5. 端到端业务流程

### 阶段 A：预检与范围锁定

1. 扫描素材目录，选择文件名可识别的最小集号作为 E01。
2. 检查视频可读性、转写文件、剧本、简介、角色图及 FFmpeg 环境。
3. 将选中的第一集路径和素材指纹写入本次 manifest，后续步骤不可静默切换到其他集。
4. 无首集视频时停止；缺剧本或角色图可以继续，但输出相应 PENDING。

### 阶段 B：取得可追溯对白

1. 优先复用明确归属第一集的 SRT/VTT/TXT 转写；否则按配置尝试转写第一集。
2. 每条 ASR 事件保存 `event_id/source_index/start/end/text`。
3. 解析第一集剧本，将对白与 ASR 做匹配；匹配成功后附带角色、场景、动作上下文和匹配覆盖率。
4. 仅 ASR 对齐的原句可以成为片中对白。简介、未对齐剧本对白不能进入候选。

### 阶段 C：候选召回与排序

1. 使用通用和题材词典召回冲突、升级、悬念候选。
2. 对每项候选记录关键词命中、强度分数、时间码、ASR ID、剧本对齐和说话人信息。
3. rules 模式排序并选择；LLM 模式可对最多 20 个已存在 ASR 候选给出有限角色标签（opening/escalation/cliffhanger/exclude），未知 ID 和非法结果丢弃。
4. 简介只作为背景提示，不可把简介中的身份反转、人物或情节当成本集证据。

### 阶段 D：约 30 秒结构与分镜草案

- 结构目标为“快速开场 → 冲突/升级 → 后钩子”。现有实现以“前核15s + 后钩子15s”作为分组；目标时长和各段长度应由配置控制。
- 首镜必须有第一集首 3 秒内的可追溯内容，这是当前硬检查。
- 后钩子必须有独立的第一集后段证据，不得拿前段镜头重复补齐。
- 每镜提示词引用原句、源时间、剧本动作证据和已知角色参考图；无动作来源时不新增动作。
- 当前 `15s` 是策划分组名称，视频生成实际采用约 10 秒片段槽位并拼接到不少于 30 秒；两者不是已自动完成的一一映射。后续应在 manifest 中显式保存“分镜→生成片段槽位”的映射，避免人工分配不清。
- 若第一集缺首 3 秒开场或足够的后钩子候选，状态为 `blocked`，不创建占位分镜或假证据。

### 阶段 E：人工确认（建议成为生成前必经关口）

审核人针对每个候选确认：

- 原句、时间码是否与第一集相符；
- 角色、动作、场景是否能从剧本/原片得到支持；
- 闪回、跳场和对白切点是否可接受；
- 选段是否形成清楚的开场、升级和悬念；
- 是否批准进入视频生成。

审核结果写入 manifest，例如 `review.status=approved|changes_requested|rejected`、审核时间、候选 ID 和备注。未批准前允许导出预览/联系表，但不进入自动化 collect/assemble。现有 gate 的相似度、脸一致性和题材审核参数可复用，但它们目前是 CLI 手动输入，不是自动判定。

### 阶段 F：生成、拼接与最终门禁

继续使用当前链路：人工在生成平台按提示词生成片段并落入槽位 → `collect` 检查时长/画幅/音轨 → `assemble` 拼接、字幕和 AI 标注 → `gate` 检查时长、内容一致性、题材与钩子质量。FAIL 阻止交付；PENDING 表示证据/人工签核未齐；WARN 允许产出但不代表完全通过。

## 6. 数据设计

沿用现有 manifest JSON，建议补齐以下稳定字段（现有字段保留）：

```json
{
  "episode_scope": {
    "policy": "first_episode_only",
    "episode_label": "E01",
    "source_episode": "首集视频路径",
    "source_fingerprint": "可选的文件大小/修改时间或哈希"
  },
  "planning": {
    "method": "rules_only",
    "visual_analysis": "not_performed",
    "evidence_status": "verified|pending",
    "logical_calls": 0,
    "fallbacks": 0
  },
  "candidates": [
    {
      "candidate_id": "asr:71",
      "source_time": {"start": 82.51, "end": 83.57},
      "dialogue": "ASR 原句",
      "asr_event_ids": [71],
      "script_alignment": {"status": "matched", "coverage": 1.0},
      "role": "温知宁",
      "scene": "第一集剧本场次",
      "source_actions": [],
      "rule_score": 0,
      "planning_hint": {}
    }
  ],
  "review": {
    "status": "pending",
    "approved_candidate_ids": [],
    "notes": []
  }
}
```

校验不变量：

- 所有候选及分镜的 `source_episode` 必须等于锁定的 E01 路径。
- 每条对白必须能映射到一个或多个带时间码的 ASR event。
- LLM 不得创建 `candidate_id`、`source_time`、对白、角色或动作；它只返回受控标签/选项。
- 无 source action 时，`action` 标记 `not_available`，不得将“抬手”推断为“打人”等具体行为。
- 简介派生的内容不得进入 `candidates` 和 `segments`。
- 审核未批准与生成状态分离；LLM 返回成功不等于人工批准或 QC PASS。

## 7. 模块与接口设计

### 复用现有模块

- `material.scan_material(src_dir)`：素材扫描和首集选择。
- `transcribe.load_or_transcribe(material)`：获取首集转写。
- `script_source.parse_script_txt/align_dialogues`：解析剧本并对齐 ASR。
- `quotes.rank_quotes(...)`：题材相关候选排序。
- `hook_planner.plan_candidates(...)`：可选 LLM 候选功能建议。
- `script.build_script(...)`：生成 manifest、分镜和硬性 QC。
- `collect.collect(...)`、`assemble.run_assemble(...)`、`gate.evaluate(...)`：制作后段。

### 建议的增量接口

```python
prepare_first_episode(src_dir, planner="rules") -> manifest
validate_episode_scope(manifest, material) -> list[Issue]
apply_candidate_review(manifest, approved_ids, notes) -> manifest
export_review_bundle(manifest, output_dir) -> ReviewBundle
```

- `prepare_first_episode` 协调现有扫描、转写、对齐、候选排序和分镜生成；只接受第一集素材。
- `validate_episode_scope` 在保存、collect 和 assemble 前检查来源集号及 ASR 证据闭环。
- `apply_candidate_review` 保存用户的选择与审核状态，不修改原始证据字段。
- `export_review_bundle` 第一版可输出 JSON/HTML 或候选帧联系表供人工审阅；不需要先做 Web UI。

CLI 建议保持简单且兼容：

```text
hooktool preflight --src <剧集素材目录>
hooktool script --src <剧集素材目录> [--planner rules|llm]
hooktool review --manifest <manifest.json> --approve asr:71,asr:7,...
hooktool collect
hooktool assemble
aihooktool gate --src <剧集素材目录> ...
```

`review` 是建议新增命令；在它实现前可人工审阅 manifest，但不能将现状误写成已有签核流程。

## 8. 错误与降级策略

| 情况 | 处理 |
|---|---|
| 找不到可识别的首集视频 | 预检 FAIL，停止后续流程 |
| 无现成转写且 ASR 依赖/模型不可用 | 标记 PENDING；不得凭剧本台词生成有时间码的镜头 |
| 剧本不存在或对白无法对齐 | 仍可使用 ASR 原句候选；说话人/动作信息留空并标记 PENDING |
| 题材识别失败 | 使用通用词典，题材标记待人工确认 |
| LLM 未配置、异常、JSON 无效或无可用建议 | 回退完整规则规划；保留失败状态和原因，不阻断 rules 模式 |
| 候选过少、开场或后钩子缺证据 | `blocked`，说明缺失项；不复制镜头、不引入后续集 |
| 人工审核拒绝/要求修改 | 不进入生成；保留原候选及审核意见，重新规划时产生新版本 |
| 收集片段不足或最终门禁 FAIL | 阻止拼接/交付并输出可定位原因 |

## 9. 非功能要求与安全边界

- **成本**：rules 模式零 LLM 调用；LLM 模式复用既有 provider，调用数和 fallback 写入 manifest。
- **隐私**：API key 不进入 manifest、日志或提示词；只在显式 LLM 模式下初始化 provider。
- **最小化发送**：候选规划仅发送有限数量候选文本、必要角色/时间和题材/简介背景；镜头建议只发送候选 ID 与段落标签。不得上传视频或角色图片到当前文本链路。
- **可复现**：记录规则配置版本、planner 模式、候选分数、来源 ID、转写来源与审核记录。
- **可解释**：每个候选记录规则命中和选择原因；LLM 的自然语言解释仅作为建议，不作为证据。
- **失败安全**：任何缺证据、越界集号、无效 candidate ID 或硬性 QC 未通过时都不能伪造 PASS。

## 10. 开发分期与验收标准

### P1：首集范围与证据闭环（优先）

- 固化 `first_episode_only`，manifest 记录锁定首集路径及转写来源。
- 检查第一集剧本对白与 ASR 的匹配证据；所有输出分镜能回溯 ASR event。
- 增加跨集污染测试：目录中有多集时，除文件名识别首集外，不对其他视频调用转写/候选处理。
- 验收：每个分镜对白和时间码均来自 E01；简介、未对齐剧本和 E02+ 均不能成为候选证据。

### P2：候选计划与人工审核（推荐下一阶段）

- 将 `candidates`、选择理由和 `review.status` 持久化。
- 增加审核导出及 `review` CLI，批准候选后才可进入生成片段收集/拼接。
- 显式保存分镜到约 10 秒生成片段槽位的映射，并让目标总时长可配置。
- 验收：未审批不能进入制作；批准记录可追踪；结构缺段时保持硬阻断。

### P3：审核辅助与质量优化

- 可选用 FFmpeg 从 E01 候选时间点抽取少量帧，生成联系表供人工查看；先不引入视觉模型 API。
- 增加跨场景/闪回、台词过长、时间重叠及字幕切分检查。
- 使用不同题材的第一集 fixture 验证通用词与题材词表现，防止只针对婆媳题材过拟合。
- 验收：画面审核明确标记人工完成与否；任何未实际运行的视觉分析仍标 `not_performed`。

### P4：生成服务 API 化（可选独立项目）

- 在生成平台提供稳定授权 API 后，才设计 provider adapter、任务状态、重试和成本限制。
- 不阻塞首集钩子策划的交付，不改变 ASR 与来源证据约束。

### 测试基线

- 素材目录排序/多集只处理首集；ASR 时间码和原始行索引保留。
- 对齐/未对齐剧本对白，动作缺失、角色图缺失和闪回场景边界。
- rules 模式零外部调用；LLM 越界 candidate ID、坏 JSON、超长/非法枚举及 provider 失败均安全回退。
- 首 3 秒开场、后段独立后钩子、候选不足硬阻断，以及 manifest 来源集号一致性。
- review 未批准/批准状态、collect/assemble/gate 现有回归测试。

## 11. 结论与建议

整体架构不另起系统，继续以 `ai_hook_tool` 的 CLI、manifest 和现有制作流水线为核心。下一阶段最值得投入的是 **P1 首集范围与证据闭环 + P2 可审阅候选计划**；视觉理解和生成 API 接入暂不作为先决条件。这样新剧输入后能稳定得到“第一集证据 → 候选钩子 → 人工确认 → 提示词/制作”的流程，并避免为了找钩子消耗资源处理全剧。
