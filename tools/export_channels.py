"""ОДНОРАЗОВО, с ЛИЧНОГО аккаунта: выгружает список каналов/чатов с вакансиями в data/channels_from_personal.txt.
Только чтение диалогов. В конце делает log_out — сессия личного аккаунта удаляется и на сервере Telegram.

Запуск:  python tools/export_channels.py
Потом:   перенеси нужные строки в config.yaml -> join_queue (или просто запусти join_queue.py, он читает этот файл)
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient  # noqa: E402
from telethon.tl.types import Channel, Chat  # noqa: E402

from app import net  # noqa: E402
from app.settings import CONFIG, DATA, TG_API_HASH, TG_API_ID  # noqa: E402

OUT = DATA / "channels_from_personal.txt"


async def main():
    auto = re.compile(CONFIG["sources"]["auto_match"], re.I)
    client = TelegramClient(str(DATA / "personal_tmp"), TG_API_ID, TG_API_HASH, proxy=net.telethon_proxy())
    print("Войди ЛИЧНЫМ аккаунтом (сессия будет удалена в конце).")
    await client.start()
    lines = []
    async for d in client.iter_dialogs():
        ent = d.entity
        if not isinstance(ent, (Channel, Chat)) or not auto.search(d.name or ""):
            continue
        username = getattr(ent, "username", None)
        if username:
            lines.append(f"@{username}    # {d.name}")
        else:
            lines.append(f"# ЗАКРЫТЫЙ, нужна инвайт-ссылка: {d.name}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Найдено {len(lines)}. Сохранено в {OUT}")
    print("Для закрытых чатов впиши инвайт-ссылки (https://t.me/+...) вместо строк с #.")
    await client.log_out()
    print("Сессия личного аккаунта удалена.")


asyncio.run(main())
