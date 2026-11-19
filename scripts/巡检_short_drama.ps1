# 短剧工坊巡检脚本（每 2 小时，由 Windows 计划任务调用）
# 正常：只写日志；异常：额外写 data\巡检异常_待读.md 并以退出码 1 结束
$ErrorActionPreference = 'SilentlyContinue'
$Root = 'D:\短剧工坊'
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$Today = Get-Date -Format 'yyyyMMdd'
$Out = Join-Path $LogDir "巡检_$Today.log"
$AlertFile = Join-Path $Root 'data\巡检异常_待读.md'

function Say($text) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text
    Add-Content -LiteralPath $Out -Value $line -Encoding UTF8
    Write-Output $text
}

$problems = @()

# 1) 健康检查
$health = $null
try { $health = Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 10 } catch { }
if (-not $health -or $health.status -ne 'ok') {
    Say '健康检查失败，尝试重启服务'
    pwsh -NoProfile -File (Join-Path $Root '服务控制.ps1') -Action restart | Out-Null
    Start-Sleep 20
    try { $health = Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 10 } catch { }
    if ($health -and $health.status -eq 'ok') { Say '重启后健康检查恢复' } else { $problems += '服务不可用且重启失败' }
} else {
    Say '健康检查 ok'
}

# 2) 队列状态 + 今日失败数（统一由 Python 输出 JSON，避免 PowerShell 引号问题）
$statsJson = & $Py (Join-Path $Root 'scripts\巡检_stats.py') 2>$null
try { $stats = $statsJson | ConvertFrom-Json } catch { $stats = $null }
$pending = 0; $done = 0; $failed = 0
if ($stats) {
    foreach ($l in $stats.links) {
        switch ($l.status) {
            'pending' { $pending++ }
            'done' { $done++ }
            'failed' { $failed++ }
        }
    }
}
Say ("队列：pending={0} done={1} failed={2}，今日失败任务={3}，running={4}" -f $pending, $done, $failed, $stats.fail_today, $stats.running)
if ($failed -gt 0) { $problems += "队列有 $failed 条 failed" }
if ([int]$stats.fail_today -gt 5) { $problems += "今日失败任务 $($stats.fail_today) 个（阈值 5）" }

# 3) 是否卡住：最后一条日志距今 > 40 分钟且无 ffmpeg
$dailyLog = Join-Path $LogDir "20261119.log"
$latestLog = Get-ChildItem $LogDir -Filter '2026*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latestLog) { $dailyLog = $latestLog.FullName }
$idleMin = 0
if (Test-Path $dailyLog) { $idleMin = [int]((Get-Date) - (Get-Item $dailyLog).LastWriteTime).TotalMinutes }
$ffmpeg = @(Get-Process ffmpeg -ErrorAction SilentlyContinue).Count
Say ("日志静默 {0} 分钟，ffmpeg 进程 {1} 个" -f $idleMin, $ffmpeg)
if ($idleMin -gt 40 -and $ffmpeg -eq 0 -and $pending -gt 0) {
    $problems += "可能卡住：日志 $idleMin 分钟无更新、无 ffmpeg、队列还有 $pending 条"
}

# 4) 磁盘
$drive = Get-PSDrive D
$freeGB = [math]::Round($drive.Free / 1GB, 1)
Say ("磁盘 D 可用 {0} GB" -f $freeGB)
if ($freeGB -lt 100) { $problems += "磁盘 D 可用仅 $freeGB GB（阈值 100GB）" }

# 5) 今日失败任务已在统计脚本中输出，这里不再重复查询

# 6) 收尾提醒
if ($pending -eq 0 -and $done -gt 0) {
    Say '队列已全部完成，可收尾（核对成片 + 上传排期）'
}

# 汇总
if ($problems.Count -gt 0) {
    $body = "## 巡检异常 {0}`n`n{1}`n`n队列 pending={2} done={3} failed={4}`n" -f (Get-Date -Format 'yyyy-MM-dd HH:mm'), ($problems -join "`n- "), $pending, $done, $failed
    Add-Content -LiteralPath $AlertFile -Value $body -Encoding UTF8
    Say ("发现异常：" + ($problems -join '；'))
    exit 1
} else {
    Say '巡检正常'
    exit 0
}
