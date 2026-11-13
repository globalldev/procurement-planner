# Ежедневный цикл: новая выгрузка -> расчёт -> ИИ-сводка -> дашборд -> публикация -> уведомление.
# Запускается Планировщиком Windows (см. install_schedule.ps1) или вручную.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$log = Join-Path $root "out\logs"
New-Item -ItemType Directory -Force $log | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$env:PYTHONIOENCODING = "utf-8"
python -m planner all *>&1 | Tee-Object -FilePath (Join-Path $log "run-$stamp.log")
exit $LASTEXITCODE
