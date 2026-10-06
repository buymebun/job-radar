"""Аккаунт-ПАРСЕР постепенно вступает в каналы из data/channels_from_personal.txt.
Лимит в день и случайные паузы — чтобы новый аккаунт не словил бан.

Строки файла: @username | https://t.me/username | https://t.me/+HASH | # комментарий
Запуск раз в день (или оставить работать — будет ждать до следующего дня):
    python tools/join_queue.py            # вступить в N сегодня
    python tools/join_queue.py --loop     # и так каждый день
Останови run.py перед запуском (одна сессия — один процесс).
"""
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient, errors  # noqa: E402
from telethon.tl.functions.channels import JoinChannelRequest  # noqa: E402
from telethon.tl.functions.messages import ImportChatInviteRequest  # noqa: E402

from app import db  # noqa: E402
from app import net  # noqa: E402
from app.settings import DATA, TG_API_HASH, TG_API_ID, TG_SESSION  # noqa: E402

PER_DAY = 7                 # не больше 5–10 в день для нового аккаунта
PAUSE = (180, 600)          # 3–10 минут между вступлениями
SRC = DATA / "channels_from_personal.txt"


def targets() -> list[str]:
    if not SRC.exists():
        sys.exit(f"Нет {SRC}. Сначала tools/export_channels.py или создай файл вручную.")
    out = []
    for line in SRC.read_text(encoding="utf-8").splitlines():
        t = line.split("#", 1)[0].strip()
        if t:
            out.append(t)
    done = {r["target"] for r in db.q("SELECT target FROM join_log WHERE result IN ('ok','already','requested')")}
    return [t for t in out if t not in done]


async def join(client: TelegramClient, t: str) -> str:
    link = t.replace("https://", "").replace("http://", "")
    if link.startswith(("t.me/+", "t.me/joinchat/")):
        h = link.split("/")[-1].lstrip("+")
        await client(ImportChatInviteRequest(h))
    else:
        await client(JoinChannelRequest(link.removeprefix("t.me/").lstrip("@")))
    return "ok"


async def run_day(client: TelegramClient):
    todo = targets()[:PER_DAY]
    if not todo:
        print("Очередь пуста — всё вступлено.")
        return False
    for i, t in enumerate(todo, 1):
        try:
            result = await join(client, t)
        except errors.UserAlreadyParticipantError:
            result = "already"
        except errors.InviteRequestSentError:
            result = "requested"   # заявка на вступление отправлена, ждёт одобрения админа
        except errors.FloodWaitError as e:
            print(f"FloodWait {e.seconds}s — останавливаюсь на сегодня")
            return True
        except Exception as e:
            result = f"error: {e.__class__.__name__}"
        db.x("INSERT OR REPLACE INTO join_log(target,result,at) VALUES(?,?,?)", (t, result, db.now()))
        print(f"[{i}/{len(todo)}] {t}: {result}")
        if i < len(todo):
            await asyncio.sleep(random.randint(*PAUSE))
    return True


async def main():
    loop = "--loop" in sys.argv
    client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH, proxy=net.telethon_proxy())
    await client.start()
    while await run_day(client) and loop:
        print("Жду сутки до следующей партии…")
        await asyncio.sleep(24 * 3600 + random.randint(0, 3600))
    await client.disconnect()


asyncio.run(main())
