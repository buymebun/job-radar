"""Подсказки к вакансии: признаки мошенничества, история общения с рекрутером, подготовка к интервью."""
import csv
import json
import logging
import re
from pathlib import Path

from . import db, llm
from .settings import CONFIG, CV_ROOT, DATA, profile_text

log = logging.getLogger(__name__)

# ---------- ⚠️ подозрительно ----------
# метка -> регулярки (без учёта регистра). Дополняется из config.yaml: scam_markers: {метка: [regex, ...]}
SCAM_MARKERS: dict[str, list[str]] = {
    "просят запустить код / репозиторий": [
        r"git\s+clone", r"npm\s+(i|install|run)\b", r"скача\w*\s+(наш\s+)?(репозитор|проект|архив)",
        r"запусти\w*\s+(наш\s+)?(проект|скрипт|репозитор)", r"run\s+(our\s+)?(project|repo)\s+locally",
        r"clone\s+(our\s+|the\s+)?repo"],
    "кошелёк / seed-фраза": [r"seed[\s-]?фраз", r"seed\s+phrase", r"секретн\w+\s+фраз", r"private\s+key", r"приватн\w+\s+ключ",
                             r"подключите\s+(свой\s+|ваш\s+)?кош[её]л[её]к", r"connect\s+your\s+(own\s+)?wallet"],
    "просят деньги": [r"оплатите", r"(нужно|необходимо|надо)\s+(оплатить|внести|перевести)", r"(?<!бес)платн\w+\s+(обучени|стажировк|курс)",
                      r"стоимость\s+обучения", r"залог", r"страхов\w+\s+взнос", r"вступительн\w+\s+взнос",
                      r"предоплат\w*\s+за\s+(обучени|место|доступ)", r"внес\w*\s+депозит"],
    "вход через чужой аккаунт / код": [r"войд\w*\s+через\s+(icloud|google|apple)", r"код\s+из\s+смс",
                                       r"установите\s+(программ|приложени|anydesk|teamviewer)"],
    "слишком хорошо: без опыта и большие деньги": [
        r"без\s+опыта[^.\n]{0,60}(от|до)\s*\d[\d\s]*(\$|usd|долл)",
        r"(зараб|доход|получа)\w*[^.\n]{0,30}\d{3,}\s*(\$|usd)?\s*(в|/)\s*(день|сутки)",
        r"пассивн\w+\s+доход"],
}
SCAM_MARKERS.update(CONFIG.get("scam_markers") or {})
_SCAM = {label: [re.compile(p, re.I) for p in pats] for label, pats in SCAM_MARKERS.items()}


# строки-предупреждения каналов («если просят запустить код — это мошенники») не считаем
_DISCLAIMER = re.compile(r"мошенни|scam|fraud|никогда\s+не\s+(переводите|платите)|будьте\s+осторожн", re.I)


def scam_flags(text: str) -> list[str]:
    text = "\n".join(line for line in (text or "").splitlines() if not _DISCLAIMER.search(line))
    return [label for label, pats in _SCAM.items() if any(p.search(text) for p in pats)]


def _post_text(vid: int) -> str:
    rows = db.q("""SELECT r.text FROM vacancy_sources s JOIN raw_posts r ON r.id=s.raw_post_id
                   WHERE s.vacancy_id=?""", (vid,))
    return "\n".join(r["text"] or "" for r in rows)


def mark_flags(vid: int) -> list[str]:
    text = _post_text(vid)
    flags = scam_flags(text)
    emp, rf_only = employment(text)
    db.x("UPDATE vacancies SET flags=?, employment=?, rf_only=?, eu_only=? WHERE id=?",
         (json.dumps(flags, ensure_ascii=False) if flags else None, emp, int(rf_only), int(eu_only(text)), vid))
    return flags


# ---------- оформление ----------
EMPLOYMENT = [
    ("ТК / штат", r"(?<!\w)тк\s*(рф|рк)|по\s+тк(?!\w)|трудов\w*\s+(договор|кодекс)|в\s+штат(?!\w)|официальн\w+\s+(трудоустройств|оформлени)"),
    ("ГПХ", r"(?<!\w)гпх(?!\w)|гражданско-правов"),
    ("ИП / самозанятый", r"(?<!\w)ип(?!\w)|самозанят|индивидуальн\w+\s+предпринимател|sole\s+proprietor"),
    ("Контракт / B2B", r"(?<!\w)b2b(?!\w)[^.\n]{0,20}(контракт|contract|договор|оформлени)|(?<!\w)b2b\s*$|contractor|контрактор|"
                       r"договор\w*\s+(подряда|оказания\s+услуг)"),
]
_EMP = [(n, re.compile(p, re.I | re.M)) for n, p in EMPLOYMENT]
_RF_ONLY = re.compile(
    r"(только|исключительно)\s+(для\s+)?(из\s+|в\s+)?(рф|росси)|резидент\w*\s+рф|налогов\w+\s+резидент\w*\s+рф|"
    r"гражданств\w*\s+рф|находит\w*\s+(в|на\s+территории)\s+(рф|росси)|локация\s*:\s*(рф|россия)(?!\w)|"
    r"удал[её]нн?о\s+по\s+рф|удал[её]нка\s+по\s+рф|в\s+пределах\s+рф|территори\w+\s+рф|(?<!\w)тк\s*рф", re.I)


_EU_ONLY = re.compile(
    r"(eu|e\.u\.|europe|european union|uk|eea|schengen)[\w\s,/-]{0,25}(resident|residence|based|citizen|work permit|right to work)|"
    r"(resident|residence|based|located|right to work|work permit|citizen)\w*\s+(in|of|within)\s+(the\s+)?(eu|europe|european union|eea|uk|schengen)\b|"
    r"(only|must be)\s+(in|from|based in)\s+(the\s+)?(eu|europe|eea|uk)\b|"
    r"(проживани\w*|резидент\w*|находит\w*|гражданств\w*|разрешени\w+\s+на\s+работу)\s+(в|на\s+территории)\s+"
    r"[^.\n]{0,50}?(?<!\w)(ес|евросоюз\w*|европ\w*|шенген\w*)(?!\w)", re.I)


def eu_only(text: str) -> bool:
    return bool(_EU_ONLY.search(text or ""))


def employment(text: str) -> tuple[str | None, bool]:
    """(«ТК РФ / ГПХ…» или None, нужно ли быть в РФ)."""
    names = [n for n, p in _EMP if p.search(text or "")]
    return (" / ".join(names) or None), bool(_RF_ONLY.search(text or ""))


def recompute_flags() -> int:
    n = 0
    for r in db.q("SELECT id FROM vacancies"):
        n += bool(mark_flags(r["id"]))
    return n


def flags_of(v) -> list[str]:
    try:
        return json.loads(v["flags"] or "[]")
    except (ValueError, TypeError, KeyError, IndexError):
        return []


# ---------- память о рекрутерах ----------
TRACKER = CV_ROOT / "Рассылка" / "трекер.csv"
_SEEN_STATUSES = ("Откликнулась", "Интервью", "Оффер", "Отказ")


def _norm(c: str) -> str:
    c = c.strip().lower().rstrip("/")
    c = re.sub(r"^https?://", "", c)
    c = re.sub(r"^(t\.me|telegram\.me)/", "@", c)
    return c


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"\w+", s.lower()) if len(w) > 3}


def recruiter_history(vid: int) -> list[dict]:
    """Где ещё встречались контакты этой вакансии: другие вакансии со статусом и строки трекера."""
    mine = [r["value"] for r in db.q("SELECT value FROM contacts WHERE vacancy_id=?", (vid,))]
    keys = {_norm(c) for c in mine if c}
    if not keys:
        return []
    out = []
    for r in db.q(f"""SELECT DISTINCT v.id, v.title, v.status, v.last_seen, c.value FROM contacts c
                      JOIN vacancies v ON v.id=c.vacancy_id
                      WHERE c.vacancy_id!=? AND v.status IN ({','.join('?' * len(_SEEN_STATUSES))})""",
                  (vid, *_SEEN_STATUSES)):
        if _norm(r["value"]) in keys:
            out.append({"contact": r["value"], "what": r["title"], "status": r["status"], "vid": r["id"],
                        "date": (r["last_seen"] or "")[:10]})
    title = db.one("SELECT title FROM vacancies WHERE id=?", (vid,))
    own_words = _words(title["title"] if title else "")
    try:
        with open(TRACKER, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                blob = " ".join((row.get(k) or "") for k in ("Ссылка", "Канал", "Заметки")).lower()
                hit = next((k for k in keys if k and k in blob), None)
                if len(own_words & _words(row.get("Роль") or "")) >= 2:
                    continue                           # это строка трекера про эту же вакансию
                if hit and not any(o["what"] == row.get("Роль") for o in out):
                    out.append({"contact": hit, "what": f"{row.get('Компания', '')}: {row.get('Роль', '')}",
                                "status": row.get("Статус", ""), "vid": None, "date": row.get("Дата отклика", "")})
    except OSError:
        pass
    return out


# ---------- CV, которое отправила ----------
UPLOADS = DATA / "cv_uploads"


def list_cvs() -> list[str]:
    paths = list(CV_ROOT.rglob("*CV*.pdf")) if CV_ROOT.exists() else []
    paths += list(UPLOADS.glob("*.pdf")) if UPLOADS.exists() else []
    return sorted(str(p) for p in paths)


def allowed_cv(path: str) -> Path | None:
    """Путь допустим, только если это PDF из папки CV или загруженных."""
    try:
        p = Path(path).resolve()
    except OSError:
        return None
    ok = p.suffix.lower() == ".pdf" and p.exists() and any(
        p.is_relative_to(root.resolve()) for root in (CV_ROOT, UPLOADS) if root.exists())
    return p if ok else None


def cv_text(path: str, limit: int = 6000) -> str:
    p = allowed_cv(path or "")
    if not p:
        return ""
    try:
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)[:limit]
    except Exception as e:
        log.warning("CV %s не прочитался: %r", p, e)
        return ""


# ---------- подготовка к интервью ----------
PREP_SYSTEM = """Ты помогаешь кандидату (женщина, пиши ей на «ты») подготовиться к интервью. Отвечай ТОЛЬКО JSON: {{"text": "..."}}
Текст в простом markdown, по-русски, без воды, без длинных тире. Разделы:
1. Коротко о компании и роли (только из текста вакансии; чего нет, так и скажи «в вакансии не указано»).
2. Что они проверят: 5–8 вероятных вопросов по их стеку и задачам.
3. Где у тебя пробелы относительно требований и как честно о них сказать одной фразой.
4. Что подсветить из опыта: 3–4 факта из профиля и CV, которые прямо закрывают требования.
5. Что спросить у них: 4–5 вопросов (деньги, формат, команда, процессы, испытательный).
6. Если в CV указано то, чего нет в профиле, предупреди: «в CV написано X, будь готова рассказать».

Профиль кандидата:
---
{profile}
---
CV, которое она отправила:
---
{cv}
---"""


async def make_prep(vid: int) -> str:
    v = db.one("SELECT * FROM vacancies WHERE id=?", (vid,))
    user = (f"Вакансия: {v['title']} · {v['company'] or 'компания не указана'} · {v['format']} · "
            f"зарплата: {v['salary_raw'] or 'не указана'}\nСтек: {', '.join(json.loads(v['stack'] or '[]'))}\n"
            f"Заметки кандидата: {v['notes'] or '-'}\n\nТекст вакансии:\n{_post_text(vid)[:4000]}")
    system = PREP_SYSTEM.format(profile=profile_text(), cv=cv_text(v["cv_path"]) or "не указано")
    data, _model = await llm.complete_json(system, user)
    text = re.sub(r"\s*[—–]\s*", ", ", str(data.get("text") or "")).strip()
    db.x("UPDATE vacancies SET prep=? WHERE id=?", (text, vid))
    return text


# ---------- метки поведения рекрутеров ----------
RECRUITER_LABELS = {
    "fast": "⚡ отвечает быстро",
    "ok": "✅ отвечает",
    "slow": "🐢 отвечает медленно",
    "reads": "👀 читает и молчит",
    "unread": "🔇 не читает",
    "never": "🚫 не писать",
}
db.x("""CREATE TABLE IF NOT EXISTS recruiters (
    contact TEXT PRIMARY KEY,
    label TEXT, note TEXT, updated TEXT
)""")


def set_recruiter(contact: str, label: str, note: str = "") -> bool:
    key = _norm(contact)
    if not key or label not in RECRUITER_LABELS:
        return False
    db.x("""INSERT INTO recruiters(contact, label, note, updated) VALUES (?,?,?,?)
            ON CONFLICT(contact) DO UPDATE SET label=excluded.label,
              note=CASE WHEN excluded.note!='' THEN excluded.note ELSE recruiters.note END, updated=excluded.updated""",
         (key, label, note, db.now()))
    return True


def recruiter_tags(vid: int) -> list[dict]:
    """Метки для контактов вакансии: [{contact, label, text, note}]."""
    out = []
    for r in db.q("SELECT value FROM contacts WHERE vacancy_id=?", (vid,)):
        t = db.one("SELECT * FROM recruiters WHERE contact=?", (_norm(r["value"]),))
        if t:
            out.append({"contact": r["value"], "label": t["label"], "text": RECRUITER_LABELS.get(t["label"], t["label"]),
                        "note": t["note"] or ""})
    return out


def recruiter_map() -> dict[str, str]:
    """contact(нормализованный) -> текст метки, для списка вакансий."""
    return {r["contact"]: RECRUITER_LABELS.get(r["label"], r["label"]) for r in db.q("SELECT contact, label FROM recruiters")}
