# Автозапуск радара при входе в Windows (без окна, через сторожа run_forever.py).
#   Установить:  powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
#   Удалить:     powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Remove
# Перед установкой хотя бы раз запусти `python run.py` в терминале и войди в аккаунт-парсер.
param([switch]$Remove)

$TaskName = "JobRadar"
$Root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Автозапуск удалён."
    exit 0
}

$Pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pyw)) { Write-Error "Нет $Pyw — сначала создай venv (см. README)"; exit 1 }
if (-not (Test-Path (Join-Path $Root "data\parser.session"))) {
    Write-Error "Нет data\parser.session — сначала один раз запусти 'python run.py' и войди"; exit 1
}

$Action   = New-ScheduledTaskAction -Execute $Pyw -Argument "run_forever.py" -WorkingDirectory $Root
$Trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Готово: радар будет запускаться при входе в Windows. Логи: data\radar.log, data\watchdog.log"
