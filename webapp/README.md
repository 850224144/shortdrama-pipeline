# 短剧工坊 Web服务

## 功能

- 📤 消息处理：粘贴包含百度网盘链接的消息，自动下载+切片
- 📊 实时状态：查看当前处理进度
- 📝 实时日志：查看处理日志
- 📋 历史记录：查看历史处理记录
- 🧹 日志清理：自动+手动清理过期日志

## 快速启动

### 方式1：双击启动（推荐）

```bash
# 双击 start.bat
start.bat
```

### 方式2：命令行启动

```bash
cd D:\短剧工坊\webapp

# 安装依赖
pip install -r requirements.txt

# 启动服务
python app.py
```

## 访问地址

- **仪表盘**：http://localhost:8765
- **API演示**：http://localhost:8765/api/demo

## API接口

### GET /api/status

获取当前状态

```bash
curl http://localhost:8765/api/status
```

### GET /api/history?limit=50

获取历史记录

```bash
curl "http://localhost:8765/api/history?limit=50"
```

### GET /api/logs?limit=100

获取日志

```bash
curl "http://localhost:8765/api/logs?limit=100"
```

### POST /api/process

处理消息

```bash
curl -X POST http://localhost:8765/api/process \
  -H "Content-Type: application/json" \
  -d '{"message": "🥰总裁爹地宠女狂魔 @url:`https://pan.baidu.com/s/xxx?pwd=yyy`"}'
```

### POST /api/cleanup-logs

清理日志

```bash
curl -X POST http://localhost:8765/api/cleanup-logs \
  -H "Content-Type: application/json" \
  -d '{"days": 30}'
```

### GET /api/health

启动健康检查：目录、ffmpeg/ffprobe、BaiduPCS-Go、ASR 脚本、磁盘空间、NVENC 探测、AI 密钥。返回 `ok/degraded/failed` 与逐项 `{name, level, detail, suggestion}`。

```bash
curl http://localhost:8765/api/health
```

### 切片队列（持久化）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cut/status` | 队列/当前任务/统计/日志 |
| POST | `/api/cut/start` | 启动切片（恢复 pending/cutting） |
| POST | `/api/cut/stop` | 停止当前（取消信号，任务回到 pending） |
| POST | `/api/cut/pause` / `/api/cut/resume` | 暂停 / 恢复 |
| POST | `/api/cut/clear` | 清队列（`status: all/pending/...`） |
| POST | `/api/cut/retry` | 失败任务重新入队（续跑保留 EDL 现场） |
| POST | `/api/cut/reconcile` | 状态对账（僵死归位/缺成片标失败/可恢复标记） |
| GET | `/api/cut/events?job_id=&limit=` | 任务级/队列级持久化事件（含阶段耗时） |

任务持久化于 `data/jobs.db`（表 `jobs` + `job_events`，WAL），服务重启自动恢复；`cutting`（崩溃现场）归位 `pending`，停止任务保持 `pending` 可续跑。终态事件携带 `metadata.duration_seconds`（阶段指标）。

### 上传队列（原子批处理）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload/batches` | 一次请求整批建任务（重复/缺失/校验失败结构化明细，无半批次） |
| GET | `/api/upload/dramas?account=` | 可上传剧集列表 |
| POST | `/api/upload/start` / `/api/upload/resume` | 开始 / 恢复上传 |
| POST | `/api/upload/open-draft` | 打开视频号草稿箱 |

### 其他

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/archive/search?q=&download_state=&slice_state=&upload_state=` | 剧级档案（下载/切片/上传状态） |

## 环境变量覆盖

配置可被 `SHORTDRAMA_*` 环境变量进程内覆盖（启动时 `apply_env_overrides(CONFIG)`，优先于配置文件）：

| 环境变量 | 覆盖 CONFIG 键 |
|---|---|
| `SHORTDRAMA_FFMPEG` | `ffmpeg_path`（未显式覆盖 `SHORTDRAMA_FFPROBE` 时由 `ffmpeg_dir` 推导 `ffprobe.exe`） |
| `SHORTDRAMA_DOWNLOAD_DIR` | `download_base_dir` |
| `SHORTDRAMA_OUTPUT_DIR` | `output_dir` |
| `SHORTDRAMA_DATA_DIR` | `data_dir` |
| `SHORTDRAMA_LOG_DIR` | `log_dir` |
| `SHORTDRAMA_BAIDUPCS` | `baidupcs_path` |
| `SHORTDRAMA_ASR_SCRIPT` | `asr_script` |
| `SHORTDRAMA_SCRIPTS_DIR` | `scripts_dir` |

## 媒体质量门禁

渲染产物进入交付前经 `delivery_quality.validate_media`：拒收 文件不存在/0 字节/无视频流/无音轨/时长为 0/时长过短/时长过长；ffprobe 失败保守拒收。不达标成片记为 `quality` 失败，不进入交付/上传。

## 配置文件

### CONFIG (processor.py)

```python
CONFIG = {
    'baidupcs_path': r'D:\短剧工坊\tools\BaiduPCS-Go-v3.9.7-windows-x64\BaiduPCS-Go.exe',
    'download_base_dir': r'E:\短剧素材',
    'scripts_dir': r'D:\短剧工坊\scripts',
    'output_dir': r'D:\短剧工坊\output',
    'cache_path': r'D:\短剧工坊\data\dramaid_cache.json',
    'state_file': r'D:\短剧工坊\data\pipeline_state.json',
    'temp_dir': os.environ.get('TEMP', r'C:\Users\86159\AppData\Local\Temp'),
    'ffmpeg_path': r'D:\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe',
    
    'download': {
        'max_workers': 5,
        'max_retries': 3,
        'timeout': 1800
    },
    
    'ai': {
        'min_duration': 180,
        'max_duration': 230,
        'count': 3,
        'retry': 2
    },
    
    'edl': {
        'target_duration': 120,
        'count': 3,
        'retry': 2
    },
    
    'quality': {
        'min_duration': 110,
        'max_duration': 130,
        'min_size_mb': 10
    },
    
    'resource': {
        'min_disk_gb': 100,
        'max_concurrent': 2
    }
}
```

## 目录结构

```
D:\短剧工坊\webapp\
├── app.py              # Web服务主文件（/api/status|health|cut|upload|archive ...）
├── processor.py        # 流水线处理器（下载/ASR/EDL/后处理/交付）
├── job_runtime.py      # 任务上下文、取消信号、资源闸门（R1）
├── process_job.py      # 单任务控制器（/api/stop 真实取消）
├── cut_manager.py      # 批量切片状态机（持久化队列 + 心跳 + 对账）
├── job_store.py        # 任务/事件持久化（data/jobs.db，WAL）
├── upload_manager.py   # 上传队列（add_batch 原子整批）
├── delivery_quality.py # 媒体质量门禁（0 字节/无音轨/无视频流/时长异常拒收）
├── health.py           # 启动健康检查 + SHORTDRAMA_* 环境变量覆盖
├── artifact_cleanup.py # 交付产物自动清理
├── requirements.txt    # Python依赖
├── start.bat           # Windows启动脚本
├── README.md           # 说明文档
├── templates\
│   └── dashboard.html  # 仪表盘页面（DOM XSS 转义 + 阶段/通知实时展示）
├── data\
│   ├── jobs.db         # 切片任务/事件库（WAL）
│   ├── pipeline_state.json  # 状态文件
│   └── dramaid_cache.json   # CPS缓存
├── output\             # 输出目录
└── logs\               # 日志目录
```

## 功能特性

### 1. 多层去重

- 状态文件检查
- 素材目录标记检查
- 输出目录检查
- 模糊去重
- 文件锁

### 2. 错误处理和重试

- 下载失败重试3次
- 切片失败重试1次
- 错误日志记录

### 3. 资源限制检查

- 磁盘空间检查（至少100GB）
- 并发任务限制（最多2个）

### 4. 进度监控

- 实时状态更新
- WebSocket推送
- 详细日志记录

### 5. 质量检查

- 交付质量门禁（`delivery_quality.validate_media`）：时长/音视频流/文件大小检查
- 渲染端 `verify_output`：时长 ≥60s、1080x1920、含音视频流、≥1MB
- 0 字节 / 无音轨 / 无视频流 / 时长异常产物被拒收，不进入交付与上传

### 6. 日志管理

- 实时日志查看
- 自动清理（30天）
- 手动清理按钮

## 混合切片方案

### AI版本（3个）

- 素材：前60%的集数
- 时长：180-230秒
- 方法：mediakit-cli高光智剪

### EDL版本（3-5个）

- 素材：后40%的集数
- 时长：120秒
- 方法：手动EDL配置

### 版本数判断

- 集数 >= 30集 → 8个版本
- 默认 → 6个版本

## 注意事项

1. **BaiduPCS-Go** 需要提前登录SVIP6账号
2. **ffmpeg** 需要安装在指定路径
3. **磁盘空间** 至少需要100GB
4. **Python版本** 需要3.11（SQLite 3.45+ 支持 UPSERT；`data/jobs.db` 任务持久化）

## 企业微信集成（后期）

后期可以加入企业微信群聊机器人：

```python
# 配置企业微信webhook
WECHAT_WEBHOOK = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx'

# 发送消息
def send_wechat(message):
    import requests
    data = {
        'msgtype': 'text',
        'text': {'content': message}
    }
    requests.post(WECHAT_WEBHOOK, json=data)
```

## 许可证

MIT License