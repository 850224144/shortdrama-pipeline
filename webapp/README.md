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
├── app.py              # Web服务主文件
├── processor.py        # 流水线处理器
├── requirements.txt    # Python依赖
├── start.bat           # Windows启动脚本
├── README.md           # 说明文档
├── templates\
│   └── dashboard.html  # 仪表盘页面
├── data\
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

- 时长检查（110-130秒）
- 文件大小检查（至少10MB）

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
4. **Python版本** 需要3.8+

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