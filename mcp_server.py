"""MCP-сервер радара: даёт ИИ-ассистенту (Claude Code) читать вакансии, писать черновики,
ставить статусы, вести трекер и открывать чат с рекрутером. Сообщения сам НЕ отправляет.

Подключение:  claude mcp add job-radar -- D:\\Projects\\job-radar\\.venv\\Scripts\\python.exe D:\\Projects\\job-radar\\mcp_server.py
"""
import csv
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from app import db, insights
from app.settings import STATUSES

CV_ROOT = Path(os.getenv("CV_ROOT", r"D:\Projects\Hueta"))
TRACKER = CV_ROOT / "Рассылка" / "трекер.csv"
TRACKER_COLS = ["Компания", "Роль", "Ссылка", "Вилка", "Канал", "Дата отклика", "Статус", "Следующий шаг", "Заметки"]

mcp = MCPServer("job-radar")


def _salary(v) -> str:
    if v["salary_from"] or v["salary_to"]:
        a, b = v["salary_from"] or "", v["salary_to"] or ""
        return f"{a}-{b} {v['currency'] or ''}".strip(" -")
    return v["salary_raw"] or "не указана"


def _contacts(vid: int) -> list[str]:
    return [r["value"] for r in db.q("SELECT value FROM contacts WHERE vacancy_id=? ORDER BY id", (vid,))]


@mcp.tool()
def list_vacancies(status: str = "Новая", min_relevance: int = 70, days: int = 3,
                   track: str = "", limit: int = 30) -> list[dict]:
    """Вакансии из радара, свежие сверху. status: одно из Новая/Интересно/Откликнулась/Интервью/Оффер/Отказ/Скрыта
    или "" для всех. track: "main", "backup" или "" (оба). days: сколько дней назад впервые увидели."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    sql = "SELECT * FROM vacancies WHERE relevance>=? AND first_seen>=?"
    args: list = [min_relevance, since]
    if status:
        sql += " AND status=?"
        args.append(status)
    if track == "backup":
        sql += " AND backup=1"
    elif track == "main":
        sql += " AND COALESCE(backup,0)=0"
    sql += " ORDER BY first_seen DESC LIMIT ?"
    args.append(limit)
    return [{"id": v["id"], "title": v["title"], "company": v["company"], "salary": _salary(v),
             "format": v["format"], "type": v["job_type"], "relevance": v["relevance"],
             "why": v["relevance_reason"], "status": v["status"], "seen": v["first_seen"][:16],
             "contacts": _contacts(v["id"]), "has_draft": bool(v["draft"])}
            for v in db.q(sql, args)]


@mcp.tool()
def get_vacancy(vacancy_id: int) -> dict:
    """Полная карточка: поля, исходный текст поста, ссылка на пост, канал, автор, контакты, черновик, заметки."""
    v = db.one("SELECT * FROM vacancies WHERE id=?", (vacancy_id,))
    if not v:
        return {"error": "нет такой вакансии"}
    src = db.one("""SELECT r.text, r.url, r.author, c.title AS channel FROM vacancy_sources s
                    JOIN raw_posts r ON r.id=s.raw_post_id JOIN channels c ON c.id=r.channel_id
                    WHERE s.vacancy_id=? ORDER BY r.date LIMIT 1""", (vacancy_id,))
    out = {k: v[k] for k in v.keys() if k != "hash"}
    out.update(salary=_salary(v), contacts=_contacts(vacancy_id),
               post_text=src["text"] if src else "", post_url=src["url"] if src else "",
               channel=src["channel"] if src else "", post_author=src["author"] if src else "",
               flags=insights.flags_of(v), recruiter_history=insights.recruiter_history(vacancy_id))
    return out


@mcp.tool()
def set_cv(vacancy_id: int, cv_path: str) -> str:
    """Отметить, какое CV отправлено по вакансии (полный путь к PDF из list_cvs)."""
    p = insights.allowed_cv(cv_path)
    if not p:
        return "такого CV нет (нужен PDF из папки CV)"
    db.x("UPDATE vacancies SET cv_path=? WHERE id=?", (str(p), vacancy_id))
    return "ok"


@mcp.tool()
def set_recruiter(contact: str, label: str, note: str = "") -> str:
    """Метка поведения рекрутера: fast (быстро отвечает), ok (отвечает), slow (медленно), reads (читает и молчит),
    unread (не читает), never (не писать). note — короткий факт с датой."""
    return "ok" if insights.set_recruiter(contact, label, note) else f"label одно из: {', '.join(insights.RECRUITER_LABELS)}"


@mcp.tool()
def save_prep(vacancy_id: int, text: str) -> str:
    """Сохранить шпаргалку к собесу в карточку (видна на дашборде)."""
    db.x("UPDATE vacancies SET prep=? WHERE id=?", (text.strip(), vacancy_id))
    return "ok"


@mcp.tool()
def save_draft(vacancy_id: int, text: str) -> str:
    """Сохранить готовый текст отклика в карточку (виден на дашборде)."""
    db.x("UPDATE vacancies SET draft=? WHERE id=?", (text.strip(), vacancy_id))
    return "ok"


@mcp.tool()
def set_status(vacancy_id: int, status: str, note: str = "") -> str:
    """Поменять статус вакансии в радаре; note дописывается в заметки с датой."""
    if status not in STATUSES:
        return f"статус должен быть одним из: {', '.join(STATUSES)}"
    v = db.one("SELECT notes FROM vacancies WHERE id=?", (vacancy_id,))
    if not v:
        return "нет такой вакансии"
    notes = v["notes"] or ""
    if note:
        notes = (notes + "\n" if notes else "") + f"{date.today():%d.%m} {note}"
    db.x("UPDATE vacancies SET status=?, notes=? WHERE id=?", (status, notes, vacancy_id))
    return "ok"


@mcp.tool()
def set_contacts(vacancy_id: int, contacts: list[str]) -> str:
    """Заменить контакты вакансии (если парсер ошибся). Формат: "@user", "mail@x.com", "https://..."."""
    db.x("DELETE FROM contacts WHERE vacancy_id=?", (vacancy_id,))
    for c in contacts:
        c = c.strip()
        t = "email" if re.fullmatch(r"[^@\s]+@[^@\s]+\.\w+", c) else "telegram" if c.startswith("@") else "link"
        db.x("INSERT OR IGNORE INTO contacts(vacancy_id, type, value) VALUES (?,?,?)", (vacancy_id, t, c))
    return "ok"


@mcp.tool()
def list_cvs() -> list[str]:
    """Все готовые CV (PDF) с полными путями."""
    return insights.list_cvs()


@mcp.tool()
def tracker_read() -> list[dict]:
    """Строки трекера откликов (Рассылка/трекер.csv)."""
    with open(TRACKER, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f, delimiter=";") if any((r.get(k) or "").strip() for k in TRACKER_COLS)]


@mcp.tool()
def tracker_upsert(company: str, role: str, status: str, link: str = "", salary: str = "", channel: str = "",
                   applied: str = "", next_step: str = "", notes: str = "") -> str:
    """Добавить или обновить строку трекера (ключ: компания + роль). applied: дата ДД.ММ.ГГГГ,
    "today" = сегодня. Пустые поля при обновлении не затирают старые значения."""
    if applied == "today":
        applied = f"{date.today():%d.%m.%Y}"
    new = dict(zip(TRACKER_COLS, [company, role, link, salary, channel, applied, status, next_step, notes]))
    with open(TRACKER, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    for r in rows:
        if r["Компания"].strip().lower() == company.strip().lower() and r["Роль"].strip().lower() == role.strip().lower():
            r.update({k: v for k, v in new.items() if v})
            break
    else:
        rows.append(new)
    with open(TRACKER, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, TRACKER_COLS, delimiter=";")
        w.writeheader()
        w.writerows(r for r in rows if any((r.get(k) or "").strip() for k in TRACKER_COLS))
    return "ok"


@mcp.tool()
def open_chat(username: str) -> str:
    """Открыть на ноуте Telegram с этим человеком (ничего не отправляет)."""
    u = username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", u):
        return "некорректный username"
    os.startfile(f"tg://resolve?domain={u}")
    return f"открыт чат @{u}"


if __name__ == "__main__":
    mcp.run()
