"""Питание и сон (Windows).
1) keep_awake: пока радар работает и ноут на зарядке — система не уходит в сон (экран гаснет как обычно).
   На батарее запрет снимается, чтобы не посадить ноут. Закрытую крышку программа не переопределяет —
   это настройка Windows (см. README → «Сон и крышка»).
2) wake_watch: если процесс «пропадал» дольше порога (сон/гибернация) — догоняем пропущенные посты."""
import asyncio
import ctypes
import logging
import sys
import time

from .settings import CONFIG

log = logging.getLogger("power")
CFG = CONFIG.get("power", {}) or {}

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


class _SPS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_ac_power() -> bool:
    if sys.platform != "win32":
        return True
    s = _SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return True
    return s.ACLineStatus != 0   # 1 — от сети, 0 — батарея, 255 — неизвестно (десктоп)


def _set_awake(on: bool):
    if sys.platform != "win32":
        return
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


async def keep_awake():
    """SetThreadExecutionState действует на поток, который его вызвал, — вызываем всегда из одного (event loop)."""
    if not CFG.get("keep_awake", True):
        return
    only_ac = CFG.get("only_on_ac", True)
    state = None
    while True:
        want = on_ac_power() or not only_ac
        if want != state:
            _set_awake(want)
            log.info("Сон системы: %s", "запрещён (на зарядке, радар работает)" if want else
                     "разрешён (работа от батареи)")
            state = want
        await asyncio.sleep(120)


async def wake_watch(on_wake):
    """Ловим «провал во времени»: цикл раз в 30 с, если прошло заметно больше — система спала."""
    gap = CFG.get("wake_gap_min", 3) * 60
    last = time.monotonic()
    wall = time.time()
    while True:
        await asyncio.sleep(30)
        now_wall = time.time()
        if now_wall - wall > gap:
            slept = (now_wall - wall) / 60
            log.info("Похоже, ноут спал ~%.0f мин — догоняю пропущенные посты", slept)
            try:
                await on_wake(slept)
            except Exception as e:
                log.warning("Догон после сна: %r", e)
        wall, last = now_wall, time.monotonic()
