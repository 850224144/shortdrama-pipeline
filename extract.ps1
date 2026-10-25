# extract.ps1 - 素材提取脚本
# 用法: .\extract.ps1 "D:\短剧工坊\inbox\剧名" "D:\短剧工坊	asks\YYYYMMDD_剧名_分析"

param(
    [Parameter(Mandatory=$true)][string]$InputDir,
    [Parameter(Mandatory=$true)][string]$OutputDir
)

Write-Host "=== 素材提取: $InputDir -> $OutputDir ===" -ForegroundColor Cyan

# 创建输出目录
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

# 激活虚拟环境
$venvPath = "D:\短剧工坊	ools\whisper\.venv\Scripts\Activate.ps1"
if (Test-Path $venvPath) {
    . $venvPath
    Write-Host "虚拟环境已激活" -ForegroundColor Green
} else {
    Write-Host "⚠️ 虚拟环境不存在，使用系统 Python" -ForegroundColor Yellow
}

# 查找视频文件
$videos = Get-ChildItem -Path $InputDir -Filter "*.mp4" | Sort-Object Name
Write-Host "找到 $($videos.Count) 个视频文件"

foreach ($video in $videos) {
    $epName = $video.BaseName
    Write-Host "`n处理: $epName" -ForegroundColor Cyan
    
    # 1. 提取音轨
    $wavPath = Join-Path $OutputDir "$epName_audio.wav"
    & ffmpeg -y -i $video.FullName -acodec pcm_s16le -ar 16000 -ac 1 $wavPath -loglevel error
    Write-Host "  ✅ 音轨提取完成"
    
    # 2. Whisper 转写
    $srtPath = Join-Path $OutputDir "$epName.srt"
    & whisper $wavPath --model large-v3 --language zh --output_format srt --output_dir $OutputDir 2>&1 | Out-Null
    Write-Host "  ✅ 转写完成: $srtPath"
    
    # 3. 删除临时 wav
    Remove-Item $wavPath -Force -ErrorAction SilentlyContinue
    Write-Host "  ✅ 临时文件已清理"
}

Write-Host "`n=== 提取完成 ===" -ForegroundColor Green
Write-Host "输出目录: $OutputDir"
