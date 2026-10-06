"""Сторож: держит run.py запущенным. Упал — перезапуск с нарастающей паузой (30 с … 10 мин).
Код выхода 2 = нет VPN/прокси (kill-switch) — ждём и пробуем снова, в прямое соединение не уходим.
Запуск без окна: pythonw run_forever.py  (автозапуск: tools/install_autostart.ps1)"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "watchdog.log"
LOG.parent.mkdir(exist_ok=True)
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def log(msg: str):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%d.%m %H:%M:%S} {msg}\n")


def main():
    delay = 30
    while True:
        started = time.time()
        log("старт run.py")
        code = subprocess.call([str(PY if PY.exists() else sys.executable), str(ROOT / "run.py")], cwd=ROOT,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if time.time() - started > 600:
            delay = 30          # проработал долго — значит, разовый сбой
        reason = {2: "нет VPN/прокси", 1: "ошибка (см. data/radar.log)"}.get(code, f"код {code}")
        log(f"run.py завершился: {reason}; перезапуск через {delay} с")
        time.sleep(delay)
        delay = min(delay * 2, 600)


if __name__ == "__main__":
    main()
