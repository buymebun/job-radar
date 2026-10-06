"""Разовое напоминание через бота радара (тот же бот и прокси).
    python tools/remind.py "Заголовок" путь\\к\\файлу_с_текстом.md [ссылка]
Удобно запускать из Планировщика заданий в нужное время."""
import asyncio
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import notify  # noqa: E402


async def main():
    title = sys.argv[1]
    body = Path(sys.argv[2]).read_text(encoding="utf-8").strip() if len(sys.argv) > 2 else ""
    link = sys.argv[3] if len(sys.argv) > 3 else ""
    text = f"⏰ <b>{html.escape(title)}</b>\n\n"
    if body:
        text += f"<pre>{html.escape(body)}</pre>\n"
    if link:
        text += f"\n{html.escape(link)}"
    await notify.send_text(text)


asyncio.run(main())
