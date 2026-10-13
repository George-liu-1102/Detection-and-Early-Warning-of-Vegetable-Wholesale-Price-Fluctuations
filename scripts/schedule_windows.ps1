# 注册 Windows 计划任务：每天 08:30 自动运行蔬菜价格监控并生成报告
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
# 查看任务：
#   schtasks /query /tn "VegPriceMonitor" /v /fo list
# 删除任务：
#   schtasks /delete /tn "VegPriceMonitor" /f
# 说明：如需告警推送，先在系统环境变量中配置 DINGTALK_WEBHOOK 或
#       WECHAT_WORK_WEBHOOK，再注册任务（脚本默认带 --notify）。

param([string]$PythonExe = "")

$ErrorActionPreference = "Stop"

$TaskName = "VegPriceMonitor"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\run_veg_monitor.py"
$OutputDir = Join-Path $RepoRoot "outputs"
if (-not $PythonExe) {
    $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonExe = $VenvPython
    } else {
        $PythonExe = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    }
}
$PythonExe = (Get-Command $PythonExe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source

$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --generate-report --notify --out-dir `"$OutputDir`"" `
    -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At 08:30
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Settings $Settings -Principal $Principal -Description "蔬菜批发价格波动检测与异常告警（每日 08:30）" -Force | Out-Null
    Write-Host "已注册计划任务：$TaskName（每天 08:30 运行）" -ForegroundColor Green
    Write-Host "Python：$PythonExe"
    Write-Host "完整业务摘要：$(Join-Path $OutputDir 'business_summary.md')"
} catch {
    Write-Host "注册失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "提示：如提示权限不足，请以管理员身份运行 PowerShell 后重试。" -ForegroundColor Yellow
    exit 1
}
