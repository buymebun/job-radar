"""Аналитика рынка по собранным вакансиям: динамика по дням, категории, стек, вилки, прогноз на завтра."""
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, resumes
from .settings import CONFIG

TZ = ZoneInfo((CONFIG.get("autopilot") or {}).get("local_tz", "Asia/Almaty"))
TECH = ("Разработка", "DevOps", "Интеграции", "Техспец", "Аналитика")
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _local_date(iso: str):
    return datetime.fromisoformat(iso).astimezone(TZ).date()


def _monthly_usd(v) -> float | None:
    """Вилка в месяц (USD/USDT/EUR как есть). Годовые суммы (>30k) делим на 12."""
    val = v["salary_from"] or v["salary_to"]
    if not val or (v["currency"] or "").upper() not in ("USD", "USDT", "EUR", "$"):
        return None
    return val / 12 if val > 30000 else float(val)


def stats(days: int = 30) -> dict:
    today = datetime.now(TZ).date()
    start = today - timedelta(days=days - 1)
    # не показываем дни, когда радар ещё не собирал (иначе нули выглядят как «вакансий не было»)
    # радар видит историю только на collect.backfill_days назад от первого запуска
    row = db.one("SELECT at FROM join_log WHERE target='__first_run__'")
    if not row:
        db.x("INSERT INTO join_log(target,result,at) VALUES('__first_run__','ts',?)", (db.now(),))
        row = db.one("SELECT at FROM join_log WHERE target='__first_run__'")
    horizon = _local_date(row["at"]) - timedelta(days=CONFIG["collect"]["backfill_days"])
    start = max(start, horizon)
    days = (today - start).days + 1
    since = datetime.combine(start, datetime.min.time(), TZ).astimezone(timezone.utc).isoformat()
    rows = db.q("SELECT * FROM vacancies WHERE first_seen>=?", (since,))

    # --- по дням
    dates = [start + timedelta(days=i) for i in range(days)]
    all_d, tech_d, rel_d = Counter(), Counter(), Counter()
    for v in rows:
        d = _local_date(v["first_seen"])
        all_d[d] += 1
        if v["category"] in TECH:
            tech_d[d] += 1
        if v["relevance"] >= 70:
            rel_d[d] += 1
    daily = {"labels": [d.strftime("%d.%m") for d in dates],
             "weekday": [WEEKDAYS[d.weekday()] for d in dates],
             "all": [all_d[d] for d in dates], "tech": [tech_d[d] for d in dates], "rel": [rel_d[d] for d in dates]}

    # --- по дням недели (среднее технических) и прогноз на завтра
    by_wd = defaultdict(list)
    for d in dates[:-1]:                       # сегодняшний день ещё не закончился
        by_wd[d.weekday()].append(tech_d[d])
    wd_avg = [round(statistics.mean(by_wd[i]), 1) if by_wd[i] else 0 for i in range(7)]
    tomorrow = today + timedelta(days=1)
    last7 = [tech_d[today - timedelta(days=i)] for i in range(1, 8)]

    # --- срезы по техническим
    tech = [v for v in rows if v["category"] in TECH]
    stack = Counter()
    for v in tech:
        for s in json.loads(v["stack"] or "[]"):
            stack[s.strip().lower()] += 1
    names = {}
    for v in tech:                              # красивое написание по первому встреченному
        for s in json.loads(v["stack"] or "[]"):
            names.setdefault(s.strip().lower(), s.strip())
    salaries = [m for m in (_monthly_usd(v) for v in tech) if m]

    src = db.q("""SELECT ch.title, count(DISTINCT v.id) n FROM vacancies v
                  JOIN vacancy_sources s ON s.vacancy_id=v.id JOIN raw_posts r ON r.id=s.raw_post_id
                  JOIN channels ch ON ch.id=r.channel_id
                  WHERE v.first_seen>=? AND v.category IN (%s) GROUP BY ch.title ORDER BY n DESC LIMIT 10"""
               % ",".join("?" * len(TECH)), (since, *TECH))

    return {
        "days": days,
        "tiles": {
            "today_tech": tech_d[today], "avg7_tech": round(statistics.mean(last7), 1),
            "today_rel": rel_d[today], "today_all": all_d[today],
            "tomorrow_wd": WEEKDAYS[tomorrow.weekday()], "tomorrow_tech": wd_avg[tomorrow.weekday()],
            "tech_share": round(100 * len(tech) / len(rows)) if rows else 0,
            "salary_median": round(statistics.median(salaries)) if salaries else None,
            "salary_n": len(salaries), "tech_total": len(tech),
        },
        "daily": daily,
        "weekday": {"labels": WEEKDAYS, "values": wd_avg},
        "categories": Counter(v["category"] for v in rows).most_common(),
        "verticals": Counter(v["vertical"] for v in tech).most_common(8),
        "formats": Counter(v["format"] for v in tech).most_common(),
        "job_types": Counter(v["job_type"] or "Не указано" for v in tech).most_common(),
        "stack": [(names[k], n) for k, n in stack.most_common(15)],
        "sources": [(r["title"], r["n"]) for r in src],
        "salary_buckets": _buckets(salaries),
        "resumes": resumes.supply(since, tech),
    }


def _buckets(vals: list[float]):
    edges = [0, 1000, 2000, 3000, 4000, 6000, 8000, 10**9]
    labels = ["до $1k", "$1-2k", "$2-3k", "$3-4k", "$4-6k", "$6-8k", "$8k+"]
    out = [0] * len(labels)
    for v in vals:
        for i in range(len(labels)):
            if edges[i] <= v < edges[i + 1]:
                out[i] += 1
                break
    return {"labels": labels, "values": out}
