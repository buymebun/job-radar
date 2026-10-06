"""Сбор постов аккаунтом-парсером: синхронизация источников, бэкфилл, онлайн, очередь LLM-разбора.
Только чтение: здесь нет ни отправки сообщений, ни вступления в чаты.
Публичные каналы из sources.public читаются по @username без вступления (новые посты — опросом)."""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

from . import db, insights, notify, resumes, tracks
from .extract import analyze
from .settings import CONFIG
from .textutil import dedup_hash, is_resume, prefilter, prefilter_chat

log = logging.getLogger("collector")
queue: asyncio.Queue[int] = asyncio.Queue()
_tasks: set[asyncio.Task] = set()  # держим ссылки, иначе GC может убить задачу
state = {"backfill": "не запускался", "queue": 0, "processed": 0, "errors": 0, "busy": False}


def _spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def post_url(ch, msg_id: int) -> str:
    if ch["username"]:
        return f"https://t.me/{ch['username']}/{msg_id}"
    tg_id = str(ch["tg_id"]).removeprefix("-100")
    return f"https://t.me/c/{tg_id}/{msg_id}"


def _buttons(msg) -> list[str]:
    out = []
    for row in (msg.buttons or []):
        for b in row:
            url = getattr(b, "url", None)
            if url:
                out.append(f"{b.text}: {url}" if b.text else url)
    return out


async def sync_sources(client: TelegramClient):
    cfg = CONFIG["sources"]
    auto = re.compile(cfg["auto_match"], re.I)
    include = {s.lower().lstrip("@") for s in cfg.get("include") or []}
    exclude = {s.lower().lstrip("@") for s in cfg.get("exclude") or []}
    n = 0
    async for d in client.iter_dialogs():
        ent = d.entity
        if not isinstance(ent, (Channel, Chat)):
            continue
        title = d.name or ""
        username = getattr(ent, "username", None)
        keys = {title.lower(), (username or "").lower()}
        matched = bool(auto.search(title)) or bool(keys & include)
        # если источники задаются папкой Telegram — включает только она (см. app/folder.py)
        enabled = matched and not (keys & exclude) and not cfg.get("folder")
        db.upsert_channel(d.id, title, username, auto_found=matched, enabled_default=enabled, is_chat=_is_chat(ent))
        _member_ids.add(d.id)
        n += 1
    log.info("Источники: найдено %d групп/каналов", n)
    await sync_public(client)


_member_ids: set[int] = set()   # где аккаунт состоит — новые посты приходят событиями


def _is_chat(ent) -> bool:
    """Группа/чат (обсуждения, запросы в свободной форме), а не канал с постами."""
    return isinstance(ent, Chat) or bool(getattr(ent, "megagroup", False) or getattr(ent, "gigagroup", False))


def _username(ref: str) -> str:
    ref = ref.strip()
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]{4,})", ref)
    return (m.group(1) if m else ref).lstrip("@")


async def sync_public(client: TelegramClient):
    """Публичные каналы и чаты из config (sources.public) — читаем по @username БЕЗ вступления."""
    refs = CONFIG["sources"].get("public") or []
    ok = 0
    for ref in refs:
        uname = _username(ref)
        try:
            ent = await client.get_entity(uname)
        except FloodWaitError as e:
            log.warning("FloodWait %ss на поиске @%s — остальные в следующий запуск", e.seconds, uname)
            break
        except Exception as e:
            log.warning("Канал @%s не найден/закрыт: %s", uname, e)
            continue
        if not isinstance(ent, Channel):
            continue
        from telethon import utils
        db.upsert_channel(utils.get_peer_id(ent), ent.title, uname, auto_found=False, enabled_default=True,
                          is_chat=_is_chat(ent))
        ok += 1
        await asyncio.sleep(1.5)   # поиск по юзернейму лимитирован — не спешим
    if refs:
        log.info("Публичные каналы без вступления: %d из %d", ok, len(refs))


async def poll_public():
    """Каналы, где аккаунт не состоит, событий не шлют — забираем новые посты опросом."""
    every = int(CONFIG["sources"].get("poll_min", 10)) * 60
    while True:
        await asyncio.sleep(every)
        for ch in db.enabled_channels():
            if ch["tg_id"] in _member_ids:
                continue
            try:
                async for msg in _client.iter_messages(ch["username"] or ch["tg_id"], min_id=ch["last_msg_id"] or 0,
                                                       limit=200):
                    await ingest(msg, ch)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                log.warning("Опрос %s: %s", ch["title"], e)
            await asyncio.sleep(2)


async def _author(msg) -> str | None:
    s = getattr(msg, "sender", None)
    if s is None:
        try:
            s = await msg.get_sender()
        except Exception:
            return None
    u = getattr(s, "username", None)
    return f"@{u}" if u else None


async def ingest(msg, ch) -> None:
    text = (msg.message or "").strip()
    if not text:
        return
    is_chat = bool(ch["is_chat"])
    min_len = CONFIG["collect"]["chat_min_text_len" if is_chat else "min_text_len"]
    # в чатах много болтовни: не пишем в базу всё подряд, только то, что похоже на запрос
    resume = is_resume(text)                      # резюме соискателей собираем отдельно (рынок с другой стороны)
    if is_chat and not resume and (len(text) < min_len or not prefilter_chat(text)[0]):
        return
    date = msg.date.astimezone(timezone.utc).isoformat(timespec="seconds")
    buttons = _buttons(msg)
    h = dedup_hash(text)
    author = await _author(msg) if is_chat else None
    rid = db.insert_raw(ch["id"], msg.id, date, text, buttons, post_url(ch, msg.id), h, author)
    if rid is None:
        return  # уже есть
    if len(text) < min_len:
        db.set_stage(rid, "short")
        return
    if resume:
        db.set_stage(rid, "resume")
        resumes.add(rid)
        return
    existing = db.vacancy_by_hash(h)
    if existing:
        db.add_source(existing["id"], rid, date)
        db.set_stage(rid, "duplicate")
        return
    ok, _reason = (True, "chat") if is_chat else prefilter(text)   # чаты уже отфильтрованы выше
    if not ok:
        db.set_stage(rid, "prefilter_fail")
        return
    await queue.put(rid)


_client: TelegramClient | None = None


async def backfill_one(ch) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=CONFIG["collect"]["backfill_days"])
    try:
        async for msg in _client.iter_messages(ch["username"] or ch["tg_id"], limit=CONFIG["collect"]["per_channel_limit"],
                                               min_id=ch["last_msg_id"] or 0):
            if msg.date < since:
                break
            await ingest(msg, ch)
    except FloodWaitError as e:
        log.warning("FloodWait %ss на %s", e.seconds, ch["title"])
        await asyncio.sleep(e.seconds + 5)
    except Exception as e:
        log.warning("Бэкфилл %s: %s", ch["title"], e)


async def backfill():
    chans = db.enabled_channels()
    for i, ch in enumerate(chans, 1):
        state["backfill"] = f"{i}/{len(chans)}: {ch['title']}"
        await backfill_one(ch)
        await asyncio.sleep(2)  # бережно к лимитам
    state["backfill"] = "готово"
    log.info("Бэкфилл завершён")


def schedule_backfill(channel_id: int):
    """Вызывается из дашборда, когда источник включили вручную."""
    ch = db.one("SELECT * FROM channels WHERE id=?", (channel_id,))
    if _client and ch:
        _spawn(backfill_one(ch))


async def worker():
    while True:
        rid = await queue.get()
        state["queue"] = queue.qsize()
        state["busy"] = True
        row = db.one("SELECT r.*, c.username AS ch_username, c.is_chat FROM raw_posts r "
                     "JOIN channels c ON c.id=r.channel_id WHERE r.id=?", (rid,))
        try:
            # дубликат мог появиться, пока пост ждал в очереди
            existing = db.vacancy_by_hash(row["hash"])
            if existing:
                db.add_source(existing["id"], rid, row["date"])
                db.set_stage(rid, "duplicate")
                continue
            card, contacts, model = await analyze(row["text"], json.loads(row["buttons"] or "[]"), row["ch_username"],
                                                  is_chat=bool(row["is_chat"]), author=row["author"])
            if card is None:
                db.set_stage(rid, "not_vacancy")
            else:
                vid = db.insert_vacancy(row["hash"], card, row["date"], model)
                db.add_source(vid, rid, row["date"])
                db.add_contacts(vid, contacts)
                insights.mark_flags(vid)
                db.set_stage(rid, "vacancy")
                backup = tracks.mark(vid)
                if card["relevance"] >= CONFIG["notify"]["min_relevance"]:
                    await notify.send_vacancy(vid)
                elif backup and tracks.CFG.get("notify", True):
                    await notify.send_vacancy(vid, tag="🛟 <b>Запасной трек</b>")
            state["processed"] += 1
        except Exception as e:
            state["errors"] += 1
            log.warning("Разбор поста %s: %s", rid, e)
            db.set_stage(rid, "llm_error")
            await asyncio.sleep(10)
        finally:
            state["queue"] = queue.qsize()
            state["busy"] = not queue.empty()


def vacancies_idle() -> bool:
    """Очередь вакансий пуста и ничего не разбирается: можно занимать модель фоновыми задачами."""
    return queue.empty() and not state["busy"]


async def retry_errors():
    """Раз в 15 минут возвращает в очередь посты, где LLM упал (лимиты, сеть)."""
    while True:
        await asyncio.sleep(900)
        for r in db.q("SELECT id FROM raw_posts WHERE stage='llm_error' LIMIT 200"):
            db.set_stage(r["id"], "new")
            await queue.put(r["id"])


def requeue_pending():
    """После перезапуска: посты, прошедшие префильтр, но не разобранные."""
    rows = db.q("""SELECT r.id, r.text, c.is_chat FROM raw_posts r JOIN channels c ON c.id=r.channel_id
                   WHERE r.stage IN ('new','llm_error')""")
    for r in rows:
        ok = prefilter_chat(r["text"])[0] if r["is_chat"] else             (len(r["text"]) >= CONFIG["collect"]["min_text_len"] and prefilter(r["text"])[0])
        if ok:
            queue.put_nowait(r["id"])


async def run(client: TelegramClient):
    global _client
    _client = client
    log.info("Запасной трек: %d вакансий после пересчёта", tracks.recompute_all())
    log.info("Подозрительных вакансий: %d", insights.recompute_flags())
    log.info("Резюме соискателей в базе: +%d", resumes.rescan())
    await sync_sources(client)
    if CONFIG["sources"].get("folder"):
        from . import folder
        added, _ = await folder.sync(client, announce=False)
        log.info("Папка «%s»: источников %d", folder.NAME,
                 db.one("SELECT count(*) n FROM channels WHERE in_folder=1")["n"])
        _spawn(folder.watch(client))
    requeue_pending()

    @client.on(events.NewMessage())
    async def on_new(event):
        ch = db.channel_by_tg(event.chat_id)
        if ch and ch["enabled"]:
            await ingest(event.message, ch)

    _spawn(worker())
    _spawn(retry_errors())
    _spawn(poll_public())

    from . import power

    async def on_wake(slept_min: float):
        before = db.one("SELECT count(*) n FROM raw_posts")["n"]
        await backfill()                      # с last_msg_id каждого источника — только пропущенное
        got = db.one("SELECT count(*) n FROM raw_posts")["n"] - before
        log.info("После сна (~%.0f мин) догнала постов: %d", slept_min, got)

    _spawn(power.keep_awake())
    _spawn(power.wake_watch(on_wake))
    if (CONFIG.get("market") or {}).get("resume_llm", True):
        _spawn(resumes.enrich_loop())          # резюме дообогащает локальная модель, Gemini не тратим
    await backfill()
