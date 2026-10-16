# 把决策日志面板装进 balatrobot mod（PowerShell 版）
#
# 为什么需要这个版本：某些安全软件会拦 python.exe 写 %AppData%，但 PowerShell
# 的自带命令可以通过。所以职责拆开——
#     python  install.py --emit-entry   生成"打好补丁的入口文件"（只写工作区）
#     powershell install.ps1            拷贝进 mod 目录（只做文件拷贝）
#
# 用法：
#     Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#     .\mod_additions\install.ps1
#     .\mod_additions\install.ps1 -Check      # 只检查
#     .\mod_additions\install.ps1 -Uninstall  # 卸载

param(
    [switch]$Check,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
$dst = Join-Path $env:APPDATA 'Balatro\Mods\balatrobot'

$files = @(
    'src/lua/ui/journal.lua',
    'src/lua/endpoints/journal.lua'
)
# 被"打补丁"的既有文件：源文件是 install.py 生成好的，这里只负责覆盖 + 备份
$patched = @(
    @{ rel = 'balatrobot.lua'; backup = 'balatrobot.lua.bak-journal' },
    @{ rel = 'src/lua/utils/gamestate.lua'; backup = 'gamestate.lua.bak-discard' },
    @{ rel = 'src/lua/endpoints/cash_out.lua'; backup = 'cash_out.lua.bak-roundeval' },
    @{ rel = 'src/lua/endpoints/pack.lua'; backup = 'pack.lua.bak-handwait' }
)
$entry = 'balatrobot.lua'
$backup = 'balatrobot.lua.bak-journal'
$marker = '-- >>> 决策日志面板'
$discardMarker = '-- >>> 导出弃牌堆'
$cashOutMarker = '-- >>> 等结算 UI 建好'

function Test-Install {
    Write-Host "mod 目录：$dst"
    Write-Host "  存在：$(Test-Path $dst)"
    foreach ($rel in $files) {
        $p = Join-Path $dst ($rel -replace '/', '\')
        Write-Host "  $(if (Test-Path $p) { '已装' } else { '缺失' })  $rel"
    }
    $e = Join-Path $dst $entry
    if (Test-Path $e) {
        $text = Get-Content $e -Raw -Encoding UTF8
        Write-Host "  入口补丁：$(if ($text -match [regex]::Escape($marker)) { '已打' } else { '未打' })"
        Write-Host "  端点注册：$(if ($text -match 'endpoints/journal\.lua') { '已注册' } else { '未注册' })"
    }
    $gs = Join-Path $dst 'src\lua\utils\gamestate.lua'
    if (Test-Path $gs) {
        $text = Get-Content $gs -Raw -Encoding UTF8
        Write-Host "  弃牌堆导出：$(if ($text -match [regex]::Escape($discardMarker)) { '已打' } else { '未打' })"
    }
    Write-Host "  备份：$(if (Test-Path (Join-Path $dst $backup)) { '有' } else { '无' })"
}

if ($Check) { Test-Install; exit 0 }

if ($Uninstall) {
    foreach ($item in $patched) {
        $rel = $item.rel
        $p = Join-Path $dst ($rel -replace '/', '\')
        $b = Join-Path $dst $item.backup
        if (Test-Path $b) {
            Copy-Item $b $p -Force
            Write-Host "  已从备份还原 $rel"
        } else {
            Write-Host "  没有备份，请手动还原 $rel"
        }
    }
    foreach ($rel in $files) {
        $p = Join-Path $dst ($rel -replace '/', '\')
        if (Test-Path $p) { Remove-Item $p -Force; Write-Host "  已删除 $rel" }
    }
    exit 0
}

if (-not (Test-Path $dst)) { Write-Host "找不到 mod 目录：$dst" -ForegroundColor Red; exit 1 }

# 源文件齐了吗（被打补丁的文件要先由 python --emit-entry 生成）
$missing = @()
foreach ($rel in $files + ($patched | ForEach-Object { $_.rel })) {
    $p = Join-Path $src ($rel -replace '/', '\')
    if (-not (Test-Path $p)) { $missing += $rel }
}
if ($missing.Count -gt 0) {
    Write-Host "缺少源文件：" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  $_" }
    Write-Host "先跑： .venv\Scripts\python.exe mod_additions\install.py --emit-entry"
    exit 1
}

# 1) 建目录
foreach ($rel in $files) {
    $target = Join-Path $dst ($rel -replace '/', '\')
    $dir = Split-Path $target -Parent
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "  已建目录 $($dir.Replace($dst, '.'))"
    }
}

# 2) 备份被打补丁的文件（各只备一次）
foreach ($item in $patched) {
    $rel = $item.rel
    $p = Join-Path $dst ($rel -replace '/', '\')
    $b = Join-Path $dst $item.backup
    if ((Test-Path $p) -and -not (Test-Path $b)) {
        Copy-Item $p $b -Force
        Write-Host "  已备份 $rel -> $($item.backup)"
    }
}

# 3) 拷贝新文件 + 覆盖打好补丁的既有文件
foreach ($rel in $files + ($patched | ForEach-Object { $_.rel })) {
    $from = Join-Path $src ($rel -replace '/', '\')
    $to = Join-Path $dst ($rel -replace '/', '\')
    Copy-Item $from $to -Force
    Write-Host "  已拷贝 $rel"
}

Write-Host ""
Write-Host "安装完成。重启游戏后生效（面板默认显示在窗口右侧，F8 隐藏）。" -ForegroundColor Green
