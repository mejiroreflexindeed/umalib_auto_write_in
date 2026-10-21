# TASK-014 W02：在项目根生成"启动录入助手.lnk"（指向中文 CMD；不写桌面）。
param(
    [string]$TargetDir = ""
)
$ErrorActionPreference = "Stop"
if ($TargetDir -eq "") {
    $TargetDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $TargetDir = Split-Path -Parent $TargetDir
}
$cmdPath = Join-Path $TargetDir "启动录入助手.cmd"
$lnkPath = Join-Path $TargetDir "启动录入助手.lnk"
if (-not (Test-Path -LiteralPath $cmdPath)) {
    Write-Error "未找到启动录入助手.cmd"
    exit 2
}
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath = $cmdPath
$sc.WorkingDirectory = $TargetDir
$sc.Description = "大书库录入助手"
$sc.Save()
Write-Output "已生成快捷方式: $lnkPath"
