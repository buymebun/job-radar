"""Дашборд вакансий (FastAPI + Jinja + HTMX). Слушает только localhost."""
import asyncio
import base64
import re
import csv
import secrets
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import collector, db, insights, llm, market, reply
from .extract import CATEGORIES, FORMATS, JOB_TYPES, VERTICALS
from .notify import _salary
from .settings import APP_TITLE, DASH_PASSWORD, DASH_USER, ROOT, STATUSES

app = FastAPI(title=APP_TITLE)


@app.middleware("http")
async def lan_auth(request: Request, call_next):
    """С самого ноута — без пароля. С других устройств в сети — Basic-auth (DASH_USER / DASH_PASSWORD)."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        if not DASH_PASSWORD:
            return Response("Доступ из сети закрыт: задай DASH_PASSWORD в .env", status_code=403)
        auth = request.headers.get("authorization", "")
        ok = False
        if auth.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, DASH_USER) and secrets.compare_digest(pwd, DASH_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Нужен вход", status_code=401, headers={"WWW-Authenticate": 'Basic realm="HRmagistr"'})
    return await call_next(request)
tpl = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
tpl.env.globals.update(salary=_salary, STATUSES=STATUSES, flags_of=insights.flags_of, APP_TITLE=APP_TITLE,
                       norm_contact=insights._norm, RECRUITER_LABELS=insights.RECRUITER_LABELS)

NON_TECH = ("Медиабаинг", "Продажи")
PERIODS = {"1": "сутки", "3": "3 дня", "7": "неделя", "30": "месяц", "all": "всё время"}
SORTS = {"rel": "v.relevance DESC, v.last_seen DESC",
         "date": "v.last_seen DESC",
         "salary": "COALESCE(v.salary_to, v.salary_from, 0) DESC, v.relevance DESC"}


def _filters(p: dict) -> tuple[str, list]:
    where, args = ["1=1"], []
    if p.get("q"):
        where.append("(v.title LIKE ? OR v.company LIKE ? OR v.summary LIKE ? OR v.stack LIKE ?)")
        args += [f"%{p['q']}%"] * 4
    for field in ("category", "format", "vertical", "job_type"):
        if p.get(field):
            where.append(f"v.{field}=?")
            args.append(p[field])
    if p.get("status"):
        where.append("v.status=?")
        args.append(p["status"])
    else:
        where.append("v.status!='Скрыта'")
    if p.get("emp") == "fit":                     # подходит из Казахстана: без привязки к РФ и ЕС
        where.append("COALESCE(v.rf_only,0)=0 AND COALESCE(v.eu_only,0)=0")
    elif p.get("emp") == "eu":
        where.append("v.eu_only=1")
    elif p.get("emp") == "rf":
        where.append("v.rf_only=1")
    elif p.get("emp") == "none":
        where.append("v.employment IS NULL")
    elif p.get("emp"):
        where.append("v.employment LIKE ?")
        args.append(f"%{p['emp']}%")
    if p.get("min_rel"):
        where.append("v.relevance>=?")
        args.append(int(p["min_rel"]))
    if p.get("salary"):
        where.append("COALESCE(v.salary_to, v.salary_from, 0)>=?")
        args.append(int(p["salary"]))
    if p.get("track") == "backup":
        where.append("v.backup=1")
    elif p.get("track") == "main":
        where.append("v.relevance>=70")
    if p.get("hide_nontech", "1") == "1" and not p.get("category") and p.get("track") != "backup":
        where.append(f"v.category NOT IN ({','.join('?' * len(NON_TECH))})")
        args += list(NON_TECH)
    period = p.get("period", "7")
    if period != "all":
        since = (datetime.now(timezone.utc) - timedelta(days=int(period))).isoformat(timespec="seconds")
        where.append("v.last_seen>=?")
        args.append(since)
    return " AND ".join(where), args


def _rows(p: dict, limit: int = 500):
    where, args = _filters(p)
    order = SORTS.get(p.get("sort", "rel"), SORTS["rel"])
    rows = db.q(f"""
        SELECT v.*,
          (SELECT group_concat(type || '|' || value, '\n') FROM contacts c WHERE c.vacancy_id=v.id) AS contacts,
          (SELECT count(*) FROM vacancy_sources s WHERE s.vacancy_id=v.id) AS n_sources,
          (SELECT ch.title FROM vacancy_sources s JOIN raw_posts r ON r.id=s.raw_post_id
             JOIN channels ch ON ch.id=r.channel_id WHERE s.vacancy_id=v.id ORDER BY r.date LIMIT 1) AS source_title,
          (SELECT r.url FROM vacancy_sources s JOIN raw_posts r ON r.id=s.raw_post_id
             WHERE s.vacancy_id=v.id ORDER BY r.date LIMIT 1) AS source_url
        FROM vacancies v WHERE {where} ORDER BY {order} LIMIT ?""", (*args, limit))
    return [_row(r) for r in rows]


def _row(r) -> dict:
    d = dict(r)
    d["stack"] = json.loads(d.get("stack") or "[]")
    d["contacts"] = [dict(zip(("type", "value"), c.split("|", 1))) for c in (d.get("contacts") or "").split("\n") if c]
    return d


def _stats() -> dict:
    day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    s = {"new_day": db.one("SELECT count(*) n FROM vacancies WHERE first_seen>=?", (day,))["n"],
         "relevant": db.one("SELECT count(*) n FROM vacancies WHERE relevance>=70 AND status!='Скрыта'")["n"]}
    for st in ("Откликнулась", "Интервью", "Оффер"):
        s[st] = db.one("SELECT count(*) n FROM vacancies WHERE status=?", (st,))["n"]
    return s


def contact_href(c: dict) -> str:
    v = c["value"]
    if c["type"] == "telegram":
        return "https://t.me/" + v.lstrip("@")
    if c["type"] == "email":
        return "mailto:" + v
    return v


tpl.env.globals.update(contact_href=contact_href)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    p = dict(request.query_params)
    p.setdefault("period", "7")
    p.setdefault("sort", "rel")
    p.setdefault("hide_nontech", "1")
    return tpl.TemplateResponse(request, "index.html", {
        "rows": _rows(p), "p": p, "stats": _stats(), "rmap": insights.recruiter_map(),
        "CATEGORIES": CATEGORIES, "FORMATS": FORMATS, "VERTICALS": VERTICALS, "PERIODS": PERIODS,
        "JOB_TYPES": JOB_TYPES,
    })


@app.get("/state", response_class=HTMLResponse)
def state(request: Request):
    return tpl.TemplateResponse(request, "_state.html", {
        "s": collector.state, "llms": llm.active_providers(), "usage": llm.usage_summary(),
        "raw": db.one("SELECT count(*) n FROM raw_posts")["n"],
        "vac": db.one("SELECT count(*) n FROM vacancies")["n"],
    })


@app.get("/v/{vid}", response_class=HTMLResponse)
def card(request: Request, vid: int):
    row = db.one("SELECT v.*, NULL contacts FROM vacancies v WHERE id=?", (vid,))
    if row is None:
        return HTMLResponse("<p>Вакансия не найдена</p>", status_code=404)
    v = _row(row)
    sources = db.q("""SELECT r.url, r.date, r.text, ch.title FROM vacancy_sources s
                      JOIN raw_posts r ON r.id=s.raw_post_id JOIN channels ch ON ch.id=r.channel_id
                      WHERE s.vacancy_id=? ORDER BY r.date""", (vid,))
    v["contacts"] = [dict(c) for c in db.q("SELECT type, value FROM contacts WHERE vacancy_id=?", (vid,))]
    return tpl.TemplateResponse(request, "_card.html", {
        "v": v, "sources": sources, "flags": insights.flags_of(row), "history": insights.recruiter_history(vid),
        "cvs": insights.list_cvs(), "rtags": {t["contact"]: t for t in insights.recruiter_tags(vid)}})


_bg: set = set()


async def _prep_bg(vid: int):
    try:
        await insights.make_prep(vid)
    except Exception as ex:
        db.x("UPDATE vacancies SET prep=? WHERE id=?", (f"Не получилось подготовить: {ex}", vid))


@app.post("/v/{vid}/status", response_class=HTMLResponse)
async def set_status(request: Request, vid: int, status: str = Form(...)):
    if status in STATUSES:
        db.x("UPDATE vacancies SET status=? WHERE id=?", (status, vid))
        row = db.one("SELECT prep FROM vacancies WHERE id=?", (vid,))
        if status == "Интервью" and not (row and row["prep"]):
            t = asyncio.create_task(_prep_bg(vid))   # шпаргалка к собесу готовится в фоне
            _bg.add(t)
            t.add_done_callback(_bg.discard)
    return tpl.TemplateResponse(request, "_status.html", {"v": {"id": vid, "status": status}})


@app.post("/v/{vid}/draft", response_class=HTMLResponse)
async def make_draft(request: Request, vid: int):
    try:
        text = await reply.draft(vid)
    except Exception as ex:
        return HTMLResponse(f'<p class="muted">Не получилось: {ex}</p>')
    contacts = [dict(c) for c in db.q("SELECT type, value FROM contacts WHERE vacancy_id=?", (vid,))]
    return tpl.TemplateResponse(request, "_draft.html", {"vid": vid, "text": text, "contacts": contacts})


@app.post("/v/{vid}/prep", response_class=HTMLResponse)
async def make_prep(request: Request, vid: int):
    try:
        await insights.make_prep(vid)
    except Exception as ex:
        return HTMLResponse(f'<p class="muted">Не получилось: {ex}</p>')
    v = db.one("SELECT id, prep FROM vacancies WHERE id=?", (vid,))
    return tpl.TemplateResponse(request, "_prep.html", {"v": dict(v)})


def _cv_partial(request: Request, vid: int):
    v = db.one("SELECT id, cv_path FROM vacancies WHERE id=?", (vid,))
    return tpl.TemplateResponse(request, "_cv.html", {"v": dict(v), "cvs": insights.list_cvs()})


@app.post("/v/{vid}/cv", response_class=HTMLResponse)
def set_cv(request: Request, vid: int, cv_path: str = Form("")):
    p = insights.allowed_cv(cv_path) if cv_path else None
    db.x("UPDATE vacancies SET cv_path=? WHERE id=?", (str(p) if p else None, vid))
    return _cv_partial(request, vid)


@app.post("/v/{vid}/cv/upload", response_class=HTMLResponse)
async def upload_cv(request: Request, vid: int, file: UploadFile = File(...)):
    name = re.sub(r"[^\w.\-]+", "_", file.filename or "cv.pdf")
    if not name.lower().endswith(".pdf"):
        return HTMLResponse('<p class="muted">Нужен PDF</p>')
    insights.UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = insights.UPLOADS / name
    dest.write_bytes(await file.read())
    db.x("UPDATE vacancies SET cv_path=? WHERE id=?", (str(dest.resolve()), vid))
    return _cv_partial(request, vid)


@app.get("/v/{vid}/cv")
def open_cv(vid: int):
    v = db.one("SELECT cv_path FROM vacancies WHERE id=?", (vid,))
    p = insights.allowed_cv(v["cv_path"]) if v and v["cv_path"] else None
    if not p:
        return Response("CV не выбрано", status_code=404)
    return FileResponse(p, media_type="application/pdf", content_disposition_type="inline", filename=p.name)


@app.post("/recruiter", response_class=HTMLResponse)
def set_recruiter(contact: str = Form(...), label: str = Form(""), note: str = Form("")):
    if label:
        insights.set_recruiter(contact, label, note)
    else:
        db.x("DELETE FROM recruiters WHERE contact=?", (insights._norm(contact),))
    return HTMLResponse('<span class="saved">✓</span>')


@app.post("/v/{vid}/notes", response_class=HTMLResponse)
def set_notes(vid: int, notes: str = Form("")):
    db.x("UPDATE vacancies SET notes=? WHERE id=?", (notes, vid))
    return HTMLResponse('<span class="saved">сохранено ✓</span>')


@app.get("/market", response_class=HTMLResponse)
def market_page(request: Request, days: int = 30):
    days = days if days in (7, 14, 30, 90) else 30
    return tpl.TemplateResponse(request, "market.html", {"m": market.stats(days), "days": days})


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    rows = db.q("""
        SELECT ch.*,
          (SELECT count(*) FROM raw_posts r WHERE r.channel_id=ch.id AND r.stage IN ('vacancy','duplicate')) AS n_vac,
          (SELECT count(*) FROM raw_posts r JOIN vacancy_sources s ON s.raw_post_id=r.id
             JOIN vacancies v ON v.id=s.vacancy_id WHERE r.channel_id=ch.id AND v.relevance>=70) AS n_rel
        FROM channels ch ORDER BY ch.enabled DESC, n_rel DESC, ch.posts DESC, ch.title""")
    return tpl.TemplateResponse(request, "sources.html", {"rows": rows})


@app.post("/sources/{cid}/toggle", response_class=HTMLResponse)
def toggle_source(cid: int):
    ch = db.one("SELECT enabled FROM channels WHERE id=?", (cid,))
    new = 0 if ch["enabled"] else 1
    db.x("UPDATE channels SET enabled=? WHERE id=?", (new, cid))
    if new:
        collector.schedule_backfill(cid)
    label = "вкл" if new else "выкл"
    return HTMLResponse(f'<button class="toggle {label}" hx-post="/sources/{cid}/toggle" hx-swap="outerHTML">{label}</button>')


@app.get("/export.csv")
def export(request: Request):
    p = dict(request.query_params)
    buf = io.StringIO()
    buf.write("﻿")  # чтобы Excel открыл кириллицу
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Релевантность", "Должность", "Тип", "Компания", "Зарплата", "Формат", "Вертикаль", "Категория",
                "Стек", "Контакты", "Источник", "Ссылка", "Дата", "Статус", "Заметки", "Описание"])
    for r in _rows(p, limit=100000):
        w.writerow([r["relevance"], r["title"], r["job_type"] or "", r["company"], _salary(r), r["format"],
                    r["vertical"], r["category"],
                    ", ".join(r["stack"]), ", ".join(c["value"] for c in r["contacts"]), r["source_title"],
                    r["source_url"], (r["last_seen"] or "")[:10], r["status"], r["notes"], r["summary"]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=vacancies.csv"})


@app.get("/resumes", response_class=HTMLResponse)
def resumes_page(request: Request):
    from . import resumes as rs
    p = dict(request.query_params)
    p.setdefault("period", "30")
    where, args = ["1=1"], []
    if p["period"] != "all":
        where.append("rs.date>=?")
        args.append((datetime.now(timezone.utc) - timedelta(days=int(p["period"]))).isoformat(timespec="seconds"))
    for f in ("role", "level"):
        if p.get(f):
            where.append(f"rs.{f}=?")
            args.append(p[f])
    if p.get("q"):
        where.append("(rs.title LIKE ? OR rs.stack LIKE ? OR rs.summary LIKE ? OR r.text LIKE ?)")
        args += [f"%{p['q']}%"] * 4
    if p.get("min_usd"):
        where.append("rs.salary_usd>=?")
        args.append(float(p["min_usd"]))
    if p.get("with_salary"):
        where.append("rs.salary_usd IS NOT NULL")
    rows = []
    for r in db.q(f"""SELECT rs.*, r.url, ch.title channel FROM resumes rs JOIN raw_posts r ON r.id=rs.raw_post_id
                      JOIN channels ch ON ch.id=rs.channel_id WHERE {' AND '.join(where)}
                      ORDER BY rs.date DESC LIMIT 300""", args):
        d = dict(r)
        d["stack"] = json.loads(d["stack"] or "[]")
        d["date"] = (d["date"] or "")[:10]
        rows.append(d)
    since = args[0] if p["period"] != "all" else "0"
    vac = [m for m in (market._monthly_usd(v) for v in db.q("SELECT * FROM vacancies WHERE first_seen>=?", (since,))) if m]
    s = {"n": len(rows), "with_salary": sum(1 for r in rows if r["salary_usd"]),
         "median": rs._median(r["salary_usd"] for r in rows), "vac_median": rs._median(vac),
         "pending": db.one("SELECT count(*) n FROM resumes WHERE COALESCE(llm_done,0)=0")["n"]}
    return tpl.TemplateResponse(request, "resumes.html", {
        "rows": rows, "p": p, "s": s, "PERIODS": PERIODS,
        "ROLES": rs.ROLE_NAMES, "LEVELS": rs.LEVEL_NAMES})
