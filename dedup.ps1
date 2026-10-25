# dedup.ps1 - 去重优化脚本
# 用法: .\dedup.ps1 "输入视频" "输出视频"

param(
    [Parameter(Mandatory=$true)][string]$InputVideo,
    [Parameter(Mandatory=$true)][string]$OutputVideo
)

Write-Host "=== 去重优化 ===" -ForegroundColor Cyan
Write-Host "输入: $InputVideo"
Write-Host "输出: $OutputVideo"

# 创建 ffmpeg 命令
# 1. 变速 1.05x
# 2. 裁切画幅（左右各裁 10px）
# 3. 调色（饱和度 +3%，亮度 +2%）
# 4. 镜像翻转
# 5. 重新编码

$ffmpegCmd = @"
ffmpeg -y -i "$InputVideo" `
-filter:v "setpts=PTS/1.05,
scale=iw-20:ih,
eq=brightness=0.02:saturation=1.03,
hflip" `
-c:v libx264 -crf 18 -preset slow `
-c:a aac -b:a 192k `
"$OutputVideo"
"@

Write-Host "`n执行 ffmpeg 命令..." -ForegroundColor Cyan
& ffmpeg -y -i $InputVideo -filter:v "setpts=PTS/1.05,scale=iw-20:ih,eq=brightness=0.02:saturation=1.03,hflip" -c:v libx264 -crf 18 -preset slow -c:a aac -b:a 192k $OutputVideo

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n✅ 去重优化完成: $OutputVideo" -ForegroundColor Green
} else {
    Write-Host "`n❌ ffmpeg 执行失败" -ForegroundColor Red
    exit 1
}
