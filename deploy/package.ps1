<#
.SYNOPSIS
  黄区(Windows,有网)一键打包脚本 —— LDO 建模器离线气隙包,你自己在黄区跑。
  Yellow-zone (Windows + network) one-command packager. Thin wrapper over deploy\package.py.

  做的事:① 找一个 Python 3.11(优先仓库 .venv,否则 py -3.11,否则 python)
          ② full 模式先软探 PyPI 可达 ③ 调 package.py(跨平台下载红区轮子+审计 glibc≤2.17
             +冻结 lock+写 MANIFEST+sha256+打 tar) ④ 列出要传给红区的文件。

  注意:取 Python 版本用的是“无引号”探测(print(major*100+minor) -> 311),
  刻意避开 Windows PowerShell 5.1 给原生程序传“含双引号参数”会被吞引号的坑。

.PARAMETER Mode
  full(默认)= 完整包,带轮子,首次部署/依赖改动时用。
  incremental = 仅代码,几十KB,首次 full 之后只改了代码时用(自动读 dist\MANIFEST.full.json 作基准)。

.PARAMETER Out
  产物目录(默认 dist)。

.PARAMETER SkipNetCheck
  跳过 PyPI 可达性预检。

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
# 用无引号的版本码探测(3.11 -> 311),避开 PS 5.1 传双引号给原生程序被吞的坑。
$ver = (& $exe @pre -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])')
$ver = "$ver".Trim()
if ($ver -ne '311') {
    throw "需要 Python 3.11(轮子是 cp311);探测到版本码=$ver(311=3.11)。试 'py -3.11',或装一个 3.11。"
}

Write-Host "[pkg] python : $exe $($pre -join ' ')  (3.11)"
Write-Host "[pkg] mode   : $Mode"
Write-Host "[pkg] out    : $Out"

# --- full 需联网下轮子:软探 PyPI(纯 PowerShell,失败只告警不中止)---
# 走代理的黄区里,原始 HEAD 可能误报失败,但 pip 仍会用你配置的代理下载,所以这里不阻断。
if ($Mode -eq 'full' -and -not $SkipNetCheck) {
    Write-Host "[pkg] 检查 PyPI 可达(full 需联网下载红区轮子)..."
    try {
        $req = [System.Net.WebRequest]::Create('https://pypi.org/simple/')
        $req.Method = 'HEAD'; $req.Timeout = 8000
        $resp = $req.GetResponse(); $resp.Close()
        Write-Host "  pypi OK"
    }
    catch {
        Write-Warning "PyPI 预检失败:$($_.Exception.Message)"
        Write-Warning "若黄区走代理,这多半是误报——下面仍会让 pip 去下载(pip 会用你配的代理)。确认完全无网请 Ctrl+C。"
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
