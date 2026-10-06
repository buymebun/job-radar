"""Уведомления обычным ботом (Bot API) на личный аккаунт: топовые вакансии, дайджест, сбои."""
import html
import logging
from datetime import datetime, timedelta, timezone

from . import db, llm, net
from .settings import CONFIG, NOTIFY_BOT_TOKEN, NOTIFY_CHAT_ID, PUBLIC_URL

log = logging.getLogger("notify")
e = html.escape
DASH = PUBLIC_URL


def _salary(v) -> str:
    if v["salary_from"] or v["salary_to"]:
        a, b = v["salary_from"], v["salary_to"]
        rng = f"{a}–{b}" if a and b else (f"от {a}" if a else f"до {b}")
        return f"{rng} {v['currency'] or ''}".strip()
    return v["salary_raw"] or "не указана"


async def send_text(text: str):
    if not (NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID):
        return
    try:
        async with net.client(20) as c:
            r = await c.post(f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage",
                             json={"chat_id": NOTIFY_CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
                                   "disable_web_page_preview": True})
            r.raise_for_status()
    except Exception as ex:
        log.warning("Уведомление не отправлено: %r", ex)


async def send_vacancy(vid: int, tag: str = ""):
    v = db.one("SELECT * FROM vacancies WHERE id=?", (vid,))
    contacts = [c["value"] for c in db.q("SELECT value FROM contacts WHERE vacancy_id=?", (vid,))]
    src = db.one("""SELECT r.url FROM vacancy_sources s JOIN raw_posts r ON r.id=s.raw_post_id
                    WHERE s.vacancy_id=? ORDER BY r.date LIMIT 1""", (vid,))
    kind = " · 🧩 проект" if v["job_type"] == "Проект / фриланс" else ""
    from . import insights
    warn = ""
    if flags := insights.flags_of(v):
        warn += f"⚠️ <b>Подозрительно:</b> {e('; '.join(flags))}\n"
    for rt in insights.recruiter_tags(vid):
        note = f" ({e(rt['note'])})" if rt["note"] else ""
        warn += f"🏷 {e(rt['contact'])}: {e(rt['text'])}{note}\n"
    for h in insights.recruiter_history(vid)[:3]:
        warn += f"👤 {e(h['contact'])} уже был: {e(h['what'])} ({e(h['status'])}{', ' + e(h['date']) if h['date'] else ''})\n"
    await send_text(
        (f"{tag}\n" if tag else "") + (f"{warn}\n" if warn else "") +
        f"🎯 <b>{e(v['title'])}</b>: {v['relevance']}%{kind}\n"
        f"{e(v['company'] or 'компания не указана')} · {e(v['format'])} · {e(v['vertical'])}\n"
        f"💰 {e(_salary(v))}\n\n"
        f"{e(v['summary'] or '')}\n\n"
        f"<i>{e(v['relevance_reason'] or '')}</i>\n\n"
        f"📩 {e(', '.join(contacts) or 'контактов нет')}\n"
        f"🔗 {e(src['url']) if src else ''}\n"
        f"📋 {DASH}/?open={vid}"
    )


async def send_digest(extra: str = ""):
    """Сводка за сутки: лучшие новые вакансии + статистика."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    min_rel = CONFIG.get("autopilot", {}).get("digest_min_relevance", 60)
    top = db.q("""SELECT * FROM vacancies WHERE first_seen>=? AND relevance>=? AND status='Новая'
                  ORDER BY relevance DESC LIMIT 10""", (since, min_rel))
    posts = db.one("SELECT count(*) n FROM raw_posts WHERE date>=?", (since,))["n"]
    vacs = db.one("SELECT count(*) n FROM vacancies WHERE first_seen>=?", (since,))["n"]
    lines = [f"📊 <b>HRmagistr за сутки</b>: постов {posts}, вакансий {vacs}, подходящих (≥{min_rel}) {len(top)}"]
    for v in top:
        kind = " 🧩" if v["job_type"] == "Проект / фриланс" else ""
        lines.append(f"• <b>{v['relevance']}</b> {e(v['title'])}{kind}: {e(_salary(v))} · "
                     f"<a href=\"{DASH}/?open={v['id']}\">открыть</a>")
    if not top:
        lines.append("Новых подходящих нет: проверь источники и profile.md.")
    backup = db.q("""SELECT * FROM vacancies WHERE first_seen>=? AND backup=1 AND status='Новая'
                     ORDER BY first_seen DESC LIMIT 7""", (since,))
    if backup:
        lines.append("\n🛟 <b>Запасной трек</b> (техспец / интегратор):")
        for v in backup:
            lines.append(f"• {e(v['title'])}: {e(_salary(v))} · <a href=\"{DASH}/?open={v['id']}\">открыть</a>")
    lines.append(f"\nLLM сегодня: {e(llm.usage_summary())}")
    if extra:
        lines.append(extra)
    lines.append(f"📋 {DASH}")
    await send_text("\n".join(lines))
