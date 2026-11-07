# 测试分层说明（阶段一 4.5）

## 目录结构

```
tests/
├── conftest.py          # 公共 fixture：sys.path 注入、临时目录、内存 JobStore、隔离配置
├── unit/                # 单元测试：纯函数 / 内存 / 临时 sqlite / mock，不起浏览器、不跑 ffmpeg 子进程
├── integration/         # 集成测试：manager 线程协作、子进程、完整链路、浏览器上传等
└── manual/              # 人工冒烟脚本：需登录态 / 真实素材 / 浏览器手动操作，不进 pytest 默认收集
```

## 运行方式（Python 3.11）

```powershell
# 全量自动化（unit + integration，manual 排除）
python -m pytest tests/unit tests/integration

# 仅单元测试（快速反馈）
python -m pytest tests/unit

# 仅集成测试（完整链路，可能依赖本机 ffmpeg / 浏览器）
python -m pytest tests/integration
```

`pytest.ini` 已配置 `testpaths = tests/unit tests/integration`，直接 `python -m pytest` 等价于全量自动化。

## 手动脚本

`tests/manual/` 下脚本用 `python tests/manual/<脚本名>.py` 独立运行，内部含硬编码账号与素材路径，需按本机环境修改；不参与 pytest 收集。

## 公共 fixture（conftest.py）

- `tmp_dir`：独立临时目录，用例结束自动清理。
- `memory_store`：进程内内存 sqlite `JobStore(':memory:')`，不落盘。
- `isolated_config`：清空 `SHORTDRAMA_*` 环境变量、指向空 yml 的隔离配置单例，结束自动复原。

## 备注

- 路由层（Blueprint）当前无 Flask test_client 自动化兜底；待 app 装配层工厂化参数化后，再在 conftest 补 `flask_client` fixture。
- 个别集成用例依赖本机 ffmpeg / ffprobe 或 `RELIABILITY_SMOKE=1` 环境开关，缺环境时按 `skipUnless` 跳过。
