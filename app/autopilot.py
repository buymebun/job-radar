"""Автопилот: сам находит новые публичные каналы/чаты с вакансиями, отключает пустые,
шлёт дайджест и сигналит о сбоях. Только чтение: никуда не вступает и ничего не пишет в Telegram от аккаунта."""
import asyncio
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telethon import functions, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel

from . import collector, db, notify
from .settings import CONFIG

log = logging.getLogger("autopilot")
CFG = CONFIG.get("autopilot", {}) or {}


def _kv_get(k):
    r = db.one("SELECT at FROM join_log WHERE target=?", (f"__autopilot__{k}",))
    return datetime.fromisoformat(r["at"]) if r and r["at"] else None


def _kv_set(k):
    db.x("INSERT OR REPLACE INTO join_log(target,result,at) VALUES(?,?,?)", (f"__autopilot__{k}", "ts", db.now()))


async def discover(client) -> list[str]:
    """Поиск публичных каналов/чатов по запросам. Добавляет новые без вступления."""
    auto = re.compile(CONFIG["sources"]["auto_match"], re.I)
    known = {r["tg_id"] for r in db.q("SELECT tg_id FROM channels")}
    added, limit = [], CFG.get("max_new_per_day", 10)
    queries = list(CFG.get("search_queries") or [])
    random.shuffle(queries)
    for q in queries:
        if len(added) >= limit:
            break
        try:
            res = await client(functions.contacts.SearchRequest(q=q, limit=30))
        except FloodWaitError as e:
            log.warning("Поиск: FloodWait %ss — остановка до следующего раза", e.seconds)
            break
        except Exception as e:
            log.warning("Поиск «%s»: %r", q, e)
            continue
        for ch in res.chats:
            if not isinstance(ch, Channel) or not ch.username:
                continue
            pid = utils.get_peer_id(ch)
            if pid in known or not auto.search(f"{ch.title} {ch.username}"):
                continue
            is_chat = bool(ch.megagroup or getattr(ch, "gigagroup", False))
            db.upsert_channel(pid, ch.title, ch.username, auto_found=True,
                              enabled_default=CFG.get("auto_enable", True), is_chat=is_chat)
            db.x("UPDATE channels SET discovered=? WHERE tg_id=?", (db.now(), pid))
            known.add(pid)
            added.append(f"{'💬' if is_chat else '📢'} {ch.title} (@{ch.username})")
            if CFG.get("auto_enable", True):
                row = db.channel_by_tg(pid)
                collector.schedule_backfill(row["id"])
            if len(added) >= limit:
                break
        await asyncio.sleep(random.uniform(8, 15))   # поиск лимитирован — медленно и по чуть-чуть
    log.info("Автопоиск: добавлено %d источников", len(added))
    return added


def prune() -> list[str]:
    """Отключает найденные автопилотом источники, где за N дней не было ни одной вакансии."""
    days = CFG.get("prune_after_days", 4)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.q("""SELECT ch.id, ch.title FROM channels ch
                   WHERE ch.enabled=1 AND ch.discovered IS NOT NULL AND ch.discovered<? AND ch.posts>=20
                     AND NOT EXISTS (SELECT 1 FROM raw_posts r WHERE r.channel_id=ch.id
                                     AND r.stage IN ('vacancy','duplicate'))""", (cutoff,))
    for r in rows:
        db.x("UPDATE channels SET enabled=0 WHERE id=?", (r["id"],))
    if rows:
        log.info("Автопилот отключил пустые источники: %s", [r["title"] for r in rows])
    return [r["title"] for r in rows]


async def health_watch():
    """Сигнал, если радар перестал получать посты или LLM стабильно падает."""
    quiet_h = CFG.get("alert_no_posts_hours", 6)
    alerted_quiet = alerted_llm = False
    last_errors = collector.state["errors"]
    while True:
        await asyncio.sleep(600)
        since = (datetime.now(timezone.utc) - timedelta(hours=quiet_h)).isoformat(timespec="seconds")
        has_sources = db.one("SELECT count(*) n FROM channels WHERE enabled=1")["n"] > 0
        fresh = db.one("SELECT count(*) n FROM raw_posts WHERE date>=?", (since,))["n"]
        if has_sources and fresh == 0 and not alerted_quiet:
            await notify.send_text(f"⚠️ HRmagistr: за {quiet_h} ч ни одного нового поста. Проверь VPN/прокси и Telegram-сессию.")
            alerted_quiet = True
        elif fresh:
            alerted_quiet = False
        errs = collector.state["errors"] - last_errors
        last_errors = collector.state["errors"]
        if errs >= 10 and not alerted_llm:
            await notify.send_text(f"⚠️ HRmagistr: {errs} ошибок разбора за 10 мин. Квота Gemini или сеть: "
                                   f"посты сохранены, разберутся позже автоматически.")
            alerted_llm = True
        elif errs == 0:
            alerted_llm = False


async def run(client):
    """Ежедневный цикл: в digest_time (по local_tz) — поиск источников, уборка, дайджест."""
    tz = ZoneInfo(CFG.get("local_tz", "Asia/Almaty"))
    hh, mm = map(int, CFG.get("digest_time", "10:00").split(":"))
    collector._spawn(health_watch())
    # первый поиск — сразу при старте, если давно не искали
    last = _kv_get("discover")
    if CFG.get("discover", True) and (not last or datetime.now(timezone.utc) - last > timedelta(hours=20)):
        await asyncio.sleep(60)   # пусть сначала отработает бэкфилл известных источников
        added = await discover(client)
        _kv_set("discover")
        if added:
            await notify.send_text("🔎 <b>HRmagistr нашёл новые источники</b>:\n" + "\n".join(added[:15]))
    while True:
        now = datetime.now(tz)
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            added = await discover(client) if CFG.get("discover", True) else []
            _kv_set("discover")
            pruned = prune()
            extra = ""
            if added:
                extra += "\n🔎 Новые источники:\n" + "\n".join(added[:10])
            if pruned:
                extra += "\n🧹 Отключены пустые: " + ", ".join(pruned[:10])
            await notify.send_digest(extra)
        except Exception as e:
            log.warning("Автопилот: %r", e)
