# setup.ps1 - 短剧工坊环境初始化
Write-Host "=== 短剧工坊环境设置 ===" -ForegroundColor Cyan

# 创建目录结构
$dirs = @(
    "inbox", "tasks", "成片归档", "tools", "template", ".trash"
)
foreach ($d in $dirs) {
    $path = Join-Path "D:\短剧工坊" $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Write-Host "  创建: $path" -ForegroundColor Green
    } else {
        Write-Host "  已存在: $path" -ForegroundColor Yellow
    }
}

# 检查 Python
Write-Host "`n=== 检查 Python ===" -ForegroundColor Cyan
try {
    $pyVersion = & python --version 2>&1
    Write-Host "  Python: $pyVersion" -ForegroundColor Green
} catch {
    Write-Host "  ❌ Python 未找到，请安装 Python 3.11+" -ForegroundColor Red
}

# 检查 ffmpeg
Write-Host "`n=== 检查 ffmpeg ===" -ForegroundColor Cyan
try {
    $ffVersion = & ffmpeg -version 2>&1 | Select-Object -First 1
    Write-Host "  ffmpeg: $ffVersion" -ForegroundColor Green
} catch {
    Write-Host "  ❌ ffmpeg 未找到，运行: winget install ffmpeg" -ForegroundColor Red
}

# 检查 GPU
Write-Host "`n=== 检查 GPU ===" -ForegroundColor Cyan
try {
    $gpuInfo = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1
    Write-Host "  GPU: $gpuInfo" -ForegroundColor Green
} catch {
    Write-Host "  ⚠️ NVIDIA GPU 未检测到" -ForegroundColor Yellow
}

Write-Host "`n=== 设置完成 ===" -ForegroundColor Cyan
Write-Host "手动安装项：" -ForegroundColor Yellow
Write-Host "  1. video_checker GUI（从飞书文档下载）"
Write-Host "  2. 剪映 PC（官网下载）"
