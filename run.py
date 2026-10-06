"""Запуск: сборщик (Telethon) + дашборд (FastAPI) + автопилот в одном процессе.
Первый запуск спросит номер аккаунта-ПАРСЕРА, код из Telegram и пароль 2FA (запускать вручную в терминале).
Дальше можно гонять автономно: python run_forever.py (или автозапуск — tools/install_autostart.ps1)."""
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

import uvicorn
from telethon import TelegramClient

from app import autopilot, collector, net, notify
from app.settings import APP_TITLE, DATA, PUBLIC_URL, TG_API_HASH, TG_API_ID, TG_SESSION, WEB_HOST, WEB_PORT
from app.web import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%d.%m %H:%M:%S",
                    handlers=[logging.StreamHandler(),
                              RotatingFileHandler(DATA / "radar.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)


async def main():
    if not (TG_API_ID and TG_API_HASH):
        sys.exit("Заполни TG_API_ID и TG_API_HASH в .env (см. .env.example)")

    # VPN / kill-switch: прокси задан, но не работает — не стартуем вообще
    try:
        ip = await net.check()
    except RuntimeError as e:
        logging.error("Сеть: %s", e)
        sys.exit(2)
    logging.info("Выход в интернет: %s%s", ip, " (через прокси)" if net.PROXY_URL else " (напрямую, PROXY_URL не задан)")

    client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH, proxy=net.telethon_proxy(),
                            device_model="Job Radar", system_version="Windows", app_version="1.0",
                            connection_retries=None, retry_delay=10, auto_reconnect=True)
    if not sys.stdin.isatty():
        # автономный режим: вводить код некому — нужна уже готовая сессия
        await client.connect()
        if not await client.is_user_authorized():
            sys.exit("Нет авторизованной сессии. Один раз запусти `python run.py` в терминале и войди.")
    await client.start()
    me = await client.get_me()
    logging.info("Вошли как %s (@%s)", me.first_name, me.username)

    server = uvicorn.Server(uvicorn.Config(app, host=WEB_HOST, port=WEB_PORT, log_level="warning"))
    logging.info("Дашборд: http://127.0.0.1:%s (на ноуте), %s (в домашней сети)", WEB_PORT, PUBLIC_URL)
    await notify.send_text(f"🟢 {APP_TITLE} запущен · выход {ip}{' через VPN' if net.PROXY_URL else ''} · "
                           f"дашборд {PUBLIC_URL}")

    collector._spawn(autopilot.run(client))
    await asyncio.gather(
        server.serve(),
        collector.run(client),
        client.run_until_disconnected(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
