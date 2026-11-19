# 短剧工坊 服务控制脚本
# 用法：pwsh -File 服务控制.ps1 -Action status|start|stop|restart
# 固定用 .venv 解释器启动，依赖版本见 requirements-lock.txt。
param(
    [ValidateSet('status', 'start', 'stop', 'restart')]
    [string]$Action = 'status',
    [int]$Port = 8765
)

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Webapp = Join-Path $Root 'webapp'
$LogDir = Join-Path $Root 'logs'
$OutLog = Join-Path $LogDir 'webapp_stdout.log'
$ErrLog = Join-Path $LogDir 'webapp_stderr.log'

function Get-ServicePid {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($conn) { return $conn.OwningProcess }
    return $null
}

function Show-Status {
    $procId = Get-ServicePid
    if ($procId) {
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        $since = if ($p) { $p.StartTime.ToString('yyyy-MM-dd HH:mm:ss') } else { 'unknown' }
        "运行中  pid=$procId  启动于=$since  端口=$Port"
    } else {
        "未运行  端口=$Port"
    }
}

function Stop-Service {
    $procId = Get-ServicePid
    if (-not $procId) { "已停止（端口 $Port 无监听）"; return }
    "停止中  pid=$procId"
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-ServicePid)) { "已停止"; return }
    }
    "停止超时，仍占用端口：$Port"
}

function Start-Service {
    if (Get-ServicePid) { "已在运行，不重复启动"; Show-Status; return }
    if (-not (Test-Path $Python)) { "找不到解释器：$Python"; return }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $proc = Start-Process -FilePath $Python -ArgumentList 'app.py' `
        -WorkingDirectory $Webapp -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog `
        -PassThru
    "已启动  pid=$($proc.Id)，等待健康检查…"
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) {
                $body = $r.Content
                "健康检查通过（${i}s）：$body"
                return
            }
        } catch { }
    }
    "健康检查未通过，请查看 $ErrLog"
}

switch ($Action) {
    'status'  { Show-Status }
    'start'   { Start-Service }
    'stop'    { Stop-Service }
    'restart' { Stop-Service; Start-Sleep -Seconds 2; Start-Service }
}
