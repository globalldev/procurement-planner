# Регистрирует ежедневный запуск планировщика закупок (по умолчанию 07:00).
param([string]$At = "07:00", [string]$Name = "ProcurementPlanner")
$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$root\scripts\daily.ps1`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Задача '$Name' зарегистрирована: ежедневно в $At. Запустить сейчас: Start-ScheduledTask -TaskName $Name"
