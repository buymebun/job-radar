"""Резюме соискателей — «другая сторона» рынка. Без LLM: роль, уровень, ожидания и стек достаём правилами."""
import json
import re

from . import db
from .settings import CONFIG
from .textutil import is_resume

db.x("""CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY,
    raw_post_id INTEGER UNIQUE,
    hash TEXT UNIQUE,
    date TEXT, channel_id INTEGER,
    title TEXT, role TEXT, level TEXT, format TEXT,
    salary INTEGER, currency TEXT, salary_usd REAL,
    stack TEXT, tags TEXT
)""")

MCFG = CONFIG.get("market") or {}
RATES = {"USD": 1.0, "USDT": 1.0, "EUR": MCFG.get("usd_per_eur", 1.08), "RUB": 1 / MCFG.get("rub_per_usd", 82),
         "KZT": 1 / MCFG.get("kzt_per_usd", 500), "UAH": 1 / MCFG.get("uah_per_usd", 41)}

# роль: первая подходящая группа (порядок важен: «devops» раньше «python»)
ROLES = [
    ("CTO / Head", r"\bcto\b|head of|руководител\w* (разработ|отдел)|технический директор"),
    ("Team / Tech Lead", r"team ?lead|tech ?lead|тимлид|техлид"),
    ("DevOps / SRE", r"devops|девопс|\bsre\b|platform engineer|системн\w* администратор|sysadmin"),
    ("Data / ML", r"data (engineer|scien)|\bml\b|machine learning|аналитик данных|data analyst"),
    ("QA", r"\bqa\b|тестировщик|aqa"),
    ("Frontend", r"frontend|фронтенд|react|vue"),
    ("Fullstack", r"full ?stack|фулл?стек"),
    ("Backend", r"backend|бэкенд|бекенд|python|golang|\bgo\b|java\b|php|node|c#|\.net"),
    ("PM / Product", r"project manager|product manager|\bpm\b|projectmanager|продакт|проджект"),
    ("Дизайн", r"design|дизайн"),
    ("Арбитраж / маркетинг", r"media ?buy|медиабай|арбитраж|affiliate|smm|маркет"),
]
LEVELS = [("Lead+", r"\blead\b|лид|head|cto|principal|architect|архитектор"), ("Senior", r"senior|сеньор|синьор"),
          ("Middle", r"middle|мидл"), ("Junior", r"junior|джун|intern|стаж")]

_TAG = re.compile(r"#([\w\-]+)", re.U)
_TITLE = re.compile(r"(?:позиция|должность|роль|position|ищу работу)\s*[:\-—]\s*(.+)", re.I)
# «от 400 000 ₽», «300к руб», «$4000», «4 000 USD», «200k»
_MONEY = re.compile(r"(?:(\$|€)\s?)?(\d[\d\s.,]{1,9})\s?(k|к|тыс\w*)?\s?(\$|€|₽|руб\w*|rub|usd|usdt|eur|евро|тенге|₸|kzt|грн|uah)?",
                    re.I)
_SAL_LINE = re.compile(r"(зарплат|зп|з/п|ожидани|salary|оклад|доход|рассматриваю от|от \d)", re.I)
_CUR = {"$": "USD", "usd": "USD", "usdt": "USDT", "€": "EUR", "eur": "EUR", "евро": "EUR", "₽": "RUB", "rub": "RUB",
        "тенге": "KZT", "₸": "KZT", "kzt": "KZT", "грн": "UAH", "uah": "UAH"}


def _salary(text: str) -> tuple[int | None, str | None]:
    for line in text.splitlines():
        if not _SAL_LINE.search(line):
            continue
        for m in _MONEY.finditer(line):
            pre, num, mult, cur = m.groups()
            cur = (pre or cur or "").lower()
            cur = "RUB" if cur.startswith("руб") else _CUR.get(cur)
            try:
                n = float(re.sub(r"[\s,]", "", num).rstrip("."))
            except ValueError:
                continue
            if mult:
                n *= 1000
            if not cur:                       # без валюты: большие суммы считаем рублями, малые долларами
                cur = "RUB" if n >= 20000 else ("USD" if n >= 300 else None)
            if cur and n >= 100:
                return int(n), cur
    return None, None


# общие слова из стека вакансий, которые ничего не говорят о навыках
STOP = {"cv", "production", "automation", "cloud", "api", "web", "telegram", "tg", "devops", "sre", "аналитика",
        "infrastructure", "ии", "нейросети", "ads", "https", "b2b saas", "базы данных", "микросервисы", "it", "remote",
        "backend", "frontend", "fullstack", "ai", "crypto", "usdt", "network", "windows", "git", "sql", "vk"}


def _vocab() -> dict[str, str]:
    """Словарь технологий берём из стека вакансий — тот же язык, что у спроса."""
    names: dict[str, str] = {}
    for r in db.q("SELECT stack FROM vacancies WHERE stack IS NOT NULL AND stack!='[]'"):
        for s in json.loads(r["stack"] or "[]"):
            k = s.strip().lower()
            if 1 < len(k) <= 30 and k not in STOP:
                names.setdefault(k, s.strip())
    return names


_vocab_cache: dict[str, re.Pattern] = {}


def _stack(text: str, vocab: dict[str, str]) -> list[str]:
    t = text.lower()
    out = []
    for k, name in vocab.items():
        p = _vocab_cache.get(k)
        if p is None:
            p = _vocab_cache[k] = re.compile(r"(?<![\w])" + re.escape(k) + r"(?![\w])")
        if p.search(t):
            out.append(name)
    return out[:25]


def parse(text: str, vocab: dict[str, str]) -> dict:
    low = text.lower()
    tags = [t.lower() for t in _TAG.findall(text)]
    m = _TITLE.search(text)
    title = m.group(1).strip()[:120] if m else ""
    head = " ".join(tags) + " " + (title or low[:400]).lower()
    role = next((name for name, p in ROLES if re.search(p, head)), "Другое")
    level = next((name for name, p in LEVELS if re.search(p, head)), "Не указан")
    fmt = "Удалёнка" if re.search(r"удал[её]н|remote", low) else "Гибрид" if "гибрид" in low else \
          "Офис" if re.search(r"\bофис|релокац", low) else "Не указан"
    sal, cur = _salary(text)
    usd = round(sal * RATES[cur], 0) if sal and cur in RATES else None
    if usd and usd > 30000:                    # годовые
        usd = round(usd / 12)
    return {"title": title, "role": role, "level": level, "format": fmt, "salary": sal, "currency": cur,
            "salary_usd": usd, "stack": _stack(text, vocab), "tags": tags[:20]}


def add(raw_id: int, vocab: dict[str, str] | None = None) -> bool:
    r = db.one("SELECT id, text, date, channel_id, hash FROM raw_posts WHERE id=?", (raw_id,))
    if not r or not r["text"]:
        return False
    p = parse(r["text"], vocab if vocab is not None else _vocab())
    db.x("""INSERT OR IGNORE INTO resumes(raw_post_id, hash, date, channel_id, title, role, level, format,
            salary, currency, salary_usd, stack, tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
         (r["id"], r["hash"], r["date"], r["channel_id"], p["title"], p["role"], p["level"], p["format"],
          p["salary"], p["currency"], p["salary_usd"], json.dumps(p["stack"], ensure_ascii=False),
          json.dumps(p["tags"], ensure_ascii=False)))
    # повтор того же резюме из другого канала не вставится (UNIQUE hash) — считаем только новые
    return db.one("SELECT 1 FROM resumes WHERE raw_post_id=?", (raw_id,)) is not None


def rescan() -> int:
    """Пройтись по всем сохранённым постам и собрать резюме (после обновления правил)."""
    vocab = _vocab()
    n = 0
    for r in db.q("""SELECT id, text FROM raw_posts WHERE text IS NOT NULL
                     AND id NOT IN (SELECT raw_post_id FROM resumes)
                     AND hash NOT IN (SELECT hash FROM resumes WHERE hash IS NOT NULL)"""):
        if is_resume(r["text"]):
            n += add(r["id"], vocab)
    return n


def _median(xs):
    xs = sorted(x for x in xs if x)
    return round(xs[len(xs) // 2]) if xs else None


def supply(since: str, vac_rows: list) -> dict:
    """Срез резюме за период + сравнение со спросом (вакансиями того же периода)."""
    from collections import Counter
    rows = db.q("""SELECT rs.*, r.url FROM resumes rs JOIN raw_posts r ON r.id=rs.raw_post_id
                   WHERE rs.date>=? ORDER BY rs.date DESC""", (since,))
    demand, supply_c, names = Counter(), Counter(), {}
    for v in vac_rows:
        for t in json.loads(v["stack"] or "[]"):
            k = t.strip().lower()
            if k not in STOP:
                demand[k] += 1
                names.setdefault(k, t.strip())
    for r in rows:
        for t in json.loads(r["stack"] or "[]"):
            supply_c[t.strip().lower()] += 1
    top = [k for k, _ in demand.most_common(15)]
    mine_roles = ("Backend", "Team / Tech Lead", "CTO / Head", "Fullstack")
    mine = [r for r in rows if r["role"] in mine_roles and r["level"] in ("Senior", "Lead+")]
    return {
        "n": len(rows),
        "with_salary": sum(1 for r in rows if r["salary_usd"]),
        "median_usd": _median(r["salary_usd"] for r in rows),
        "roles": Counter(r["role"] for r in rows).most_common(),
        "levels": Counter(r["level"] for r in rows).most_common(),
        "vs": {"labels": [names[k] for k in top], "demand": [demand[k] for k in top],
               "supply": [supply_c[k] for k in top]},
        "mine_n": len(mine), "mine_median": _median(r["salary_usd"] for r in mine),
        "recent": [{"date": (r["date"] or "")[:10], "role": r["role"], "level": r["level"],
                    "title": r["title"], "salary": f"{r['salary']:,} {r['currency']}".replace(",", " ") if r["salary"] else "",
                    "usd": r["salary_usd"], "url": r["url"]} for r in rows[:25]],
    }


# ---------- дообогащение локальной моделью (в фоне, квоту Gemini не тратит) ----------
for _c, _d in (("llm_done", "INTEGER DEFAULT 0"), ("years", "REAL"), ("summary", "TEXT")):
    if _c not in {r[1] for r in db.q("PRAGMA table_info(resumes)")}:
        db.x(f"ALTER TABLE resumes ADD COLUMN {_c} {_d}")

ROLE_NAMES = [n for n, _ in ROLES] + ["Другое"]
LEVEL_NAMES = [n for n, _ in LEVELS] + ["Не указан"]
ENRICH_SYSTEM = f"""Это резюме соискателя из Telegram. Верни ТОЛЬКО JSON:
{{"title": "желаемая должность коротко", "role": одно из {ROLE_NAMES}, "level": одно из {LEVEL_NAMES},
"years": число лет опыта или null, "salary": число или null, "currency": "USD|EUR|RUB|KZT|UAH|USDT" или null,
"period": "month|year|hour" или null, "format": "Удалёнка|Гибрид|Офис|Не указан",
"summary": "одна строка: кто это и чем силён"}}
Зарплату бери только из ожиданий кандидата. Ничего не выдумывай: нет данных, ставь null."""


async def enrich_one(r) -> None:
    from . import llm
    data, _m = await llm.complete_local(ENRICH_SYSTEM, (r["text"] or "")[:2500])
    upd = {}
    if data.get("role") in ROLE_NAMES and data["role"] != "Другое":
        upd["role"] = data["role"]
    if data.get("level") in LEVEL_NAMES and data["level"] != "Не указан":
        upd["level"] = data["level"]
    if data.get("format") in ("Удалёнка", "Гибрид", "Офис"):
        upd["format"] = data["format"]
    if (data.get("title") or "").strip() and not r["title"]:
        upd["title"] = str(data["title"]).strip()[:120]
    try:
        upd["years"] = float(data["years"]) if data.get("years") not in (None, "", "null") else None
    except (TypeError, ValueError):
        pass
    upd["summary"] = str(data.get("summary") or "").strip()[:300]
    # зарплату модели берём, только если правила её не нашли
    if not r["salary"] and data.get("salary") and str(data.get("currency") or "").upper() in RATES:
        try:
            sal, cur = float(data["salary"]), str(data["currency"]).upper()
            per = {"year": 1 / 12, "hour": 160}.get(str(data.get("period") or "month"), 1)
            upd.update(salary=int(sal), currency=cur, salary_usd=round(sal * RATES[cur] * per))
        except (TypeError, ValueError):
            pass
    upd["llm_done"] = 1
    db.x(f"UPDATE resumes SET {', '.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), r["id"]))


async def enrich_loop(batch: int = 10, pause_sec: int = 300):
    import asyncio
    import logging
    log = logging.getLogger(__name__)
    while True:
        from .collector import vacancies_idle
        if not vacancies_idle():                 # вакансии важнее: ждём, пока их очередь опустеет
            await asyncio.sleep(30)
            continue
        rows = db.q("""SELECT rs.*, r.text FROM resumes rs JOIN raw_posts r ON r.id=rs.raw_post_id
                       WHERE COALESCE(rs.llm_done,0)=0 ORDER BY rs.date DESC LIMIT ?""", (batch,))
        done = 0
        for r in rows:
            if not vacancies_idle():             # пришли вакансии посреди пачки — уступаем
                break
            try:
                await enrich_one(r)
                done += 1
            except Exception as e:
                log.info("Резюме %s: локальная модель недоступна (%r), попробую позже", r["id"], e)
                break
        if done:
            log.info("Резюме: локальная модель разобрала %d", done)
        await asyncio.sleep(5 if done else pause_sec)
