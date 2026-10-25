# cleanup.ps1 - 清理脚本
# 用法: .\cleanup.ps1 "D:\短剧工坊	asks\YYYYMMDD_剧名"

param(
    [Parameter(Mandatory=$true)][string]$TaskDir
)

Write-Host "=== 清理任务目录: $TaskDir ===" -ForegroundColor Cyan

# 检查目录是否存在
if (-not (Test-Path $TaskDir)) {
    Write-Host "❌ 目录不存在: $TaskDir" -ForegroundColor Red
    exit 1
}

# 要删除的目录
$trashDirs = @("03_剪映工程", "05_查重报告")
# 要保留的目录
$keepDirs = @("00_素材", "01_分析", "02_剪辑计划", "04_成片")

# 删除临时目录到回收站
foreach ($dir in $trashDirs) {
    $path = Join-Path $TaskDir $dir
    if (Test-Path $path) {
        $trashPath = Join-Path "D:\短剧工坊\.trash" $dir
        # 移动到回收站（使用 PowerShell 的 Move-Item 到 trash 目录）
        try {
            Move-Item -Path $path -Destination "D:\短剧工坊\.trash" -Force
            Write-Host "  已清理: $dir" -ForegroundColor Yellow
        } catch {
            Write-Host "  ⚠️ 无法移动 $dir，尝试删除" -ForegroundColor Red
            Remove-Item -Path $path -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# 验证保留目录
foreach ($dir in $keepDirs) {
    $path = Join-Path $TaskDir $dir
    if (Test-Path $path) {
        Write-Host "  保留: $dir" -ForegroundColor Green
    } else {
        Write-Host "  ⚠️ 缺失: $dir" -ForegroundColor Yellow
    }
}

Write-Host "`n✅ 清理完成" -ForegroundColor Green
