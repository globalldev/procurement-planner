# Ежедневный цикл: новая выгрузка -> расчёт -> ИИ-сводка -> дашборд -> публикация -> уведомление.
# Запускается Планировщиком Windows (см. install_schedule.ps1) или вручную.
# Код возврата: 0 — всё прошло; 1 — не удалась публикация; 2 — выгрузка не годится для расчёта.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$log = Join-Path $root "out\logs"
New-Item -ItemType Directory -Force $log | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logFile = Join-Path $log "run-$stamp.log"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
# Вывод перенаправляет cmd, а не PowerShell: в PowerShell 5.1 любая строка stderr (предупреждение Python)
# при перенаправлении становится ошибкой и обрывает скрипт, а Tee-Object пишет лог в UTF-16 с испорченной кириллицей.
cmd /c "python -m planner all > `"$logFile`" 2>&1"
$code = $LASTEXITCODE
Get-Content -Path $logFile -Encoding UTF8
exit $code
