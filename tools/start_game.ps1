# 启动 Balatro 并加载 BalatroBot（游戏必须由这个脚本启动，mod 才能拿到正确的环境变量）
#
# 用法：
#     .\tools\start_game.ps1              默认：直接启动游戏并等 mod 就绪
#     .\tools\start_game.ps1 -Normal      正常速度（想看画面时用）
#     .\tools\start_game.ps1 -Port 8080   换端口
#     .\tools\start_game.ps1 -ViaCli      改用 `uvx balatrobot serve` 启动（见下方说明）
#     .\tools\start_game.ps1 -Timeout 240 等待 mod 就绪的上限秒数
#
# 为什么默认不用官方 CLI（-ViaCli）
# ---------------------------------
# `uvx balatrobot serve` 里的健康检查是**硬编码 30 秒**（manager.py 的
# HEALTH_TIMEOUT），超时它就会把游戏进程杀掉并报错。而本机冷启动实测要
# 60~70 秒（首次还要编译着色器），于是 CLI 自己把刚起来的游戏杀了，
# 留下一个"健康检查失败"的报错——但游戏其实只是慢。
#
# 所以默认改成：脚本自己设好 BALATROBOT_* 环境变量、直接启动 Balatro.exe，
# 然后用更宽的超时轮询 mod 是否就绪。游戏窗口保持打开，关掉它即结束。

param(
    [int]$Port = 12346,
    [switch]$Normal,
    [switch]$Headless,
    [switch]$ViaCli,
    [int]$Timeout = 240,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Extra
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$gameDir = 'D:\Steam\steamapps\common\Balatro'
$exe = Join-Path $gameDir 'Balatro.exe'
$lovely = Join-Path $gameDir 'version.dll'

if (-not (Test-Path $exe)) {
    throw "找不到 Balatro：$exe`n请修改本脚本里的 `$gameDir。"
}
if (-not (Test-Path $lovely)) {
    throw "找不到 Lovely 注入库：$lovely`nLovely 没装好的话 mod 不会加载。"
}

# uv 默认把缓存和工具装在 %APPDATA%，这里改到项目内，便于隔离和清理
$env:UV_CACHE_DIR    = Join-Path $root '.uv-cache'
$env:UV_TOOL_DIR     = Join-Path $root '.uv-tools'
$env:UV_TOOL_BIN_DIR = Join-Path $root '.uv-bin'

# mod 通过环境变量读取端口和运行参数，这些必须在启动游戏**之前**设好
$env:BALATROBOT_HOST     = '127.0.0.1'
$env:BALATROBOT_PORT     = "$Port"
$env:BALATROBOT_PLATFORM = 'windows'
$env:BALATROBOT_LOGS_PATH = Join-Path $root 'logs'
$env:BALATROBOT_LOVE_PATH = $exe
$env:BALATROBOT_LOVELY_PATH = $lovely
if (-not $Normal) { $env:BALATROBOT_FAST = '1' }
if ($Headless)    { $env:BALATROBOT_HEADLESS = '1' }

function Wait-ForMod {
    param([int]$Port, [int]$Timeout)
    $deadline = (Get-Date).AddSeconds($Timeout)
    $attempt = 0
    while ((Get-Date) -lt $deadline) {
        $attempt++
        try {
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port" -Method Post `
                -ContentType 'application/json' `
                -Body '{"jsonrpc":"2.0","method":"health","id":1}' -TimeoutSec 4
            if ($response.result.status -eq 'ok') { return $true }
        } catch { }
        if ($attempt % 5 -eq 0) {
            Write-Host "  仍在等待 mod 就绪（已 $($attempt * 3) 秒）..." -ForegroundColor DarkGray
        }
        Start-Sleep -Seconds 3
    }
    return $false
}

Write-Host "启动 Balatro：端口 $Port，目录 $gameDir" -ForegroundColor Cyan

if ($ViaCli) {
    # 官方 CLI 路径（健康检查只有 30 秒，冷启动慢的机器上会误杀）
    $arguments = @(
        'balatrobot', 'serve',
        '--port', $Port,
        '--platform', 'windows',
        '--love-path', $exe,
        '--lovely-path', $lovely,
        '--logs-path', (Join-Path $root 'logs')
    )
    if (-not $Normal) { $arguments += '--fast' }
    if ($Headless) { $arguments += '--headless' }
    if ($Extra) { $arguments += $Extra }
    Push-Location $root
    try { & uvx @arguments } finally { Pop-Location }
    exit $LASTEXITCODE
}

Push-Location $gameDir
try {
    Start-Process -FilePath $exe -WorkingDirectory $gameDir | Out-Null
} finally {
    Pop-Location
}

if (Wait-ForMod -Port $Port -Timeout $Timeout) {
    Write-Host "mod 已就绪。" -ForegroundColor Green
    Write-Host "另开一个窗口运行机器人，例如：" -ForegroundColor DarkGray
    Write-Host "    tools\run_bot.cmd status" -ForegroundColor DarkGray
    Write-Host "    tools\run_bot.cmd eval --games 40" -ForegroundColor DarkGray
} else {
    Write-Host "等待 $Timeout 秒后 mod 仍未就绪。排查：" -ForegroundColor Yellow
    Write-Host "  1. 游戏窗口是否打开了？没打开说明 Balatro 启动就失败了"
    Write-Host "  2. Lovely 日志：%AppData%\Balatro\Mods\lovely\log\ 最新一个"
    Write-Host "  3. mod 日志：$(Join-Path $root 'logs')\<时间戳>\$Port.log"
    exit 1
}
