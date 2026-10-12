$ErrorActionPreference = "Stop"

$TaskName = "paqu 每日视频发现 Agent"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner = Join-Path $ProjectDir "run_daily_agent.bat"

if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "找不到每日运行脚本：$Runner"
}

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/d /c `"$Runner`"" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At "09:00"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 4)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "每天搜索并筛选近 30 天的 YouTube 绳索事故与吊装测试视频；审核后才下载。" -Force | Out-Null

$Task = Get-ScheduledTask -TaskName $TaskName
[PSCustomObject]@{
    TaskName = $Task.TaskName
    State = $Task.State
    StartWhenAvailable = $Task.Settings.StartWhenAvailable
    DailyAt = "09:00"
    Runner = $Runner
} | Format-List
