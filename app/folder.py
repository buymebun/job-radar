"""Источники из папки Telegram (например «Vacancies») на аккаунте-парсере.
Добавила канал/чат в папку — радар подхватывает его и выгружает историю; убрала — перестаёт читать."""
import asyncio
import logging

from telethon import utils
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import (Channel, Chat, DialogFilter, DialogFilterChatlist, InputPeerSelf, InputPeerUser,
                               User)

from . import collector, db, notify
from .settings import CONFIG

log = logging.getLogger("folder")
NAME = (CONFIG["sources"].get("folder") or "").strip()


def _title(f) -> str:
    t = getattr(f, "title", "")
    return getattr(t, "text", t) or ""


async def folder_peer_ids(client) -> set[int] | None:
    """peer_id всех каналов/чатов папки; None — папки с таким названием нет."""
    res = await client(GetDialogFiltersRequest())
    filters = getattr(res, "filters", res)
    f = next((x for x in filters if isinstance(x, (DialogFilter, DialogFilterChatlist))
              and _title(x).strip().lower() == NAME.lower()), None)
    if f is None:
        return None
    ids = {_pid(p) for p in list(f.include_peers) + list(f.pinned_peers)} - {None}
    # папка может включать целые типы чатов («все каналы» / «все группы»)
    if isinstance(f, DialogFilter) and (f.broadcasts or f.groups):
        async for d in client.iter_dialogs():
            ent = d.entity
            if f.broadcasts and isinstance(ent, Channel) and ent.broadcast:
                ids.add(d.id)
            if f.groups and (isinstance(ent, Chat) or (isinstance(ent, Channel) and ent.megagroup)):
                ids.add(d.id)
        ids -= {_pid(p) for p in f.exclude_peers}
    return ids


def _pid(peer) -> int | None:
    """Только каналы и группы; «Избранное» (InputPeerSelf), люди и боты — не источники."""
    if isinstance(peer, (InputPeerSelf, InputPeerUser)):
        return None
    try:
        return utils.get_peer_id(peer)
    except TypeError:
        return None


async def sync(client, announce: bool = True) -> tuple[list[str], list[str]]:
    ids = await folder_peer_ids(client)
    if ids is None:
        log.warning("Папка «%s» не найдена на аккаунте-парсере", NAME)
        return [], []
    added, removed = [], []
    for pid in ids:
        try:
            ent = await client.get_entity(pid)
        except Exception as e:
            log.warning("Папка: не открыть %s: %r", pid, e)
            continue
        if isinstance(ent, User):          # «Избранное», личные чаты, боты — не источники
            continue
        is_chat = isinstance(ent, Chat) or bool(getattr(ent, "megagroup", False))
        row = db.channel_by_tg(pid)
        was_on = bool(row and row["enabled"])
        db.upsert_channel(pid, ent.title, getattr(ent, "username", None), auto_found=False,
                          enabled_default=True, is_chat=is_chat)
        db.x("UPDATE channels SET enabled=1, in_folder=1 WHERE tg_id=?", (pid,))
        collector._member_ids.add(pid)
        if not was_on:
            added.append(f"{'💬' if is_chat else '📢'} {ent.title}")
            if announce:   # при старте историю выгружает общий бэкфилл — не дублируем
                collector.schedule_backfill(db.channel_by_tg(pid)["id"])
    for row in db.q("SELECT * FROM channels WHERE in_folder=1"):
        if row["tg_id"] not in ids:
            db.x("UPDATE channels SET enabled=0, in_folder=0 WHERE id=?", (row["id"],))
            removed.append(row["title"])
    if added or removed:
        log.info("Папка «%s»: +%s −%s", NAME, added, removed)
        if announce:
            msg = []
            if added:
                msg.append("📥 <b>Из папки «%s» подключены</b>:\n%s" % (NAME, "\n".join(added[:20])))
            if removed:
                msg.append("📤 Убраны из папки (больше не читаю): " + ", ".join(removed[:20]))
            await notify.send_text("\n\n".join(msg))
    return added, removed


async def watch(client):
    every = int(CONFIG["sources"].get("folder_poll_min", 2)) * 60
    while True:
        await asyncio.sleep(every)
        try:
            await sync(client)
        except Exception as e:
            log.warning("Папка: %r", e)
