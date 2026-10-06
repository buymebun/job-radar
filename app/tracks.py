"""Треки поиска. Основной — релевантность под profile.md (основной поиск).
Запасной — быстрый выход: техспец / интегратор / техподдержка, удалёнка, вилка от backup_track.min_salary.
Запасной считается правилами по уже разобранной карточке — без лишних запросов к LLM."""
from . import db
from .textutil import tech_evidence
from .settings import CONFIG

CFG = CONFIG.get("backup_track", {}) or {}


def monthly_usd(v) -> float | None:
    """Вилка в месяц (USD/USDT/EUR). Годовые суммы (>30k) делим на 12."""
    val = v["salary_from"] or v["salary_to"]
    if not val or (v["currency"] or "").upper() not in ("USD", "USDT", "EUR", "$"):
        return None
    return val / 12 if val > 30000 else float(val)


def is_backup(v) -> bool:
    if not CFG.get("enabled", True):
        return False
    title = (v["title"] or "").lower()
    role_ok = v["category"] in (CFG.get("categories") or []) or \
        any(k in title for k in (CFG.get("title_keywords") or []))
    if not role_ok:
        return False
    # роль должна подтверждаться самим текстом, а не только названием, которое могла придумать модель
    post = db.q("""SELECT r.text FROM vacancy_sources s JOIN raw_posts r ON r.id=s.raw_post_id
                   WHERE s.vacancy_id=?""", (v["id"],))
    if not tech_evidence("\n".join(r["text"] or "" for r in post)):
        return False
    if CFG.get("remote_only", True) and v["format"] == "Офис":
        return False
    m = monthly_usd(v)
    if m is None:
        return CFG.get("allow_unknown_salary", True)
    return m >= CFG.get("min_salary", 2500)


def mark(vid: int) -> bool:
    v = db.one("SELECT * FROM vacancies WHERE id=?", (vid,))
    flag = is_backup(v)
    db.x("UPDATE vacancies SET backup=? WHERE id=?", (int(flag), vid))
    return flag


def recompute_all() -> int:
    """После правки правил в config.yaml — пересчитать все вакансии (быстро, без LLM)."""
    n = 0
    for v in db.q("SELECT * FROM vacancies"):
        flag = int(is_backup(v))
        if flag != (v["backup"] or 0):
            db.x("UPDATE vacancies SET backup=? WHERE id=?", (flag, v["id"]))
        n += flag
    return n
