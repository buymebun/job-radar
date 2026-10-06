# Остановить радар: и автозапуск-задачу, и сторож, и сам run.py.
#   powershell -ExecutionPolicy Bypass -File tools\stop.ps1
$Root = Split-Path -Parent $PSScriptRoot
Stop-ScheduledTask -TaskName "JobRadar" -ErrorAction SilentlyContinue
# все python/pythonw из venv радара (сторож, run.py, ручной запуск в терминале)
$procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
         Where-Object { $_.ExecutablePath -like "$Root\.venv\*" -and $_.CommandLine -notlike "*mcp_server.py*" }
# mcp_server.py не трогаем: его держит ИИ-ассистент (MCP-клиент), с радаром он не связан
if (-not $procs) { Write-Host "Радар не запущен."; exit 0 }
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "Остановлен PID $($p.ProcessId): $($p.CommandLine)"
}
Write-Host "Готово. Автозапуск при следующем входе в Windows останется (убрать: tools\install_autostart.ps1 -Remove)."
