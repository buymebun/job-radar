# Статус радара: запущен ли, автозапуск, последние строки логов.
#   powershell -ExecutionPolicy Bypass -File tools\status.ps1
$Root = Split-Path -Parent $PSScriptRoot

$task = Get-ScheduledTask -TaskName "JobRadar" -ErrorAction SilentlyContinue
if ($task) { Write-Host "Автозапуск: установлен ($($task.State))" } else { Write-Host "Автозапуск: не установлен" }

$procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
         Where-Object { $_.ExecutablePath -like "$Root*" }
if ($procs) {
    Write-Host "Процессы радара:"
    $procs | ForEach-Object { Write-Host ("  PID {0}  {1}" -f $_.ProcessId, ($_.CommandLine -replace '.*\\', '')) }
} else { Write-Host "Процессы радара: не запущен" }

try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:8765/state"
    Write-Host "Дашборд: работает -> http://127.0.0.1:8765"
    Write-Host ("  " + ($r.Content -replace '<[^>]+>', ' ' -replace '\s+', ' ').Trim())
} catch { Write-Host "Дашборд: не отвечает" }

foreach ($log in "radar.log", "watchdog.log") {
    $f = Join-Path $Root "data\$log"
    if (Test-Path $f) { Write-Host "`n--- последние строки $log ---"; Get-Content $f -Tail 8 -Encoding UTF8 }
}
