<#
.SYNOPSIS
  黄区(Windows,有网)一键打包脚本 —— LDO 建模器离线气隙包,你自己在黄区跑。
  Yellow-zone (Windows + network) one-command packager. Thin wrapper over deploy\package.py.

  做的事:① 找一个 Python 3.11(优先仓库 .venv,否则 py -3.11,否则 python)
          ② full 模式先查 PyPI 可达 ③ 调 package.py(跨平台下载红区轮子+审计 glibc≤2.17
             +冻结 lock+写 MANIFEST+sha256+打 tar) ④ 列出要传给红区的文件。

.PARAMETER Mode
  full(默认)= 完整包,带轮子,首次部署/依赖改动时用。
  incremental = 仅代码,几十KB,首次 full 之后只改了代码时用(自动读 dist\MANIFEST.full.json 作基准)。

.PARAMETER Out
  产物目录(默认 dist)。

.PARAMETER SkipNetCheck
  跳过 PyPI 可达性预检(代理环境/已确认有网时用)。

.EXAMPLE
  .\deploy\package.ps1                       # 完整包 -> dist\
.EXAMPLE
  .\deploy\package.ps1 -Mode incremental     # 增量包(代码-only)
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\package.ps1   # 若脚本执行被策略拦住
#>
[CmdletBinding()]
param(
    [ValidateSet('full', 'incremental')]
    [string]$Mode = 'full',
    [string]$Out = 'dist',
    [switch]$SkipNetCheck
)
$ErrorActionPreference = 'Stop'

# --- 定位仓库根目录(本脚本在 <root>\deploy\)---
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --- 选一个 Python 3.11(轮子是 cp311,版本必须对)---
$exe = $null
$pre = @()
$venv = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $venv) {
    $exe = $venv
}
elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $exe = 'py'; $pre = @('-3.11')
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $exe = 'python'
}
else {
    throw "未找到 Python。请先装 Python 3.11(含 pip)。/ No Python 3.11 (with pip) on PATH."
}

# --- 校验确实是 3.11 ---
$ver = & $exe @pre -c 'import sys;print("%d.%d"%sys.version_info[:2])'
if ("$ver".Trim() -ne '3.11') {
    throw "需要 Python 3.11(轮子是 cp311),当前=$ver。试 'py -3.11' 或一个 3.11 的 venv。"
}

Write-Host "[pkg] python : $exe $($pre -join ' ')  (v$ver)"
Write-Host "[pkg] mode   : $Mode"
Write-Host "[pkg] out    : $Out"

# --- full 需联网下轮子:先探 PyPI ---
if ($Mode -eq 'full' -and -not $SkipNetCheck) {
    Write-Host "[pkg] 检查 PyPI 可达(full 需联网下载红区轮子)..."
    & $exe @pre -c 'import urllib.request;urllib.request.urlopen("https://pypi.org/simple/",timeout=8);print("  pypi OK")'
    if ($LASTEXITCODE -ne 0) {
        throw "PyPI 不可达。黄区需联网才能 full 打包(增量包不下轮子)。确认有网后可加 -SkipNetCheck 跳过本检查。"
    }
}

# --- 调用打包引擎(下载+审计 glibc≤2.17+冻结 lock+MANIFEST+sha256+tar)---
& $exe @pre (Join-Path $Root 'deploy\package.py') $Mode --out $Out
if ($LASTEXITCODE -ne 0) {
    throw "package.py 失败(exit $LASTEXITCODE)。看上面输出:AUDIT FAIL 就按提示在 requirements-gui.txt 降版重跑。"
}

# --- 列出要传给红区的两个文件 ---
if ($Mode -eq 'full') { $stem = 'ldo_modeler_full.tar.gz' } else { $stem = 'ldo_modeler_incremental.tar.gz' }
Write-Host ""
Write-Host "== 产物($Out)—— 把下面 .tar.gz 和它的 .sha256 一起传到红区 =="
Get-ChildItem -Path $Out -Filter "$stem*" -ErrorAction SilentlyContinue |
    Select-Object Name, @{N = 'MB'; E = { [math]::Round($_.Length / 1MB, 2) } }, LastWriteTime |
    Format-Table -AutoSize
Write-Host "红区落地后:"
Write-Host "  sha256sum -c $stem.sha256"
if ($Mode -eq 'full') {
    Write-Host "  tar xzf $stem -C /tmp/ldo && cd /tmp/ldo && ./bootstrap.sh /opt/ldo_modeler"
}
else {
    Write-Host "  tar xzf $stem -C /tmp/ldo && cd /tmp/ldo && ./update.sh   /opt/ldo_modeler"
}
