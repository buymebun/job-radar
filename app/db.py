import json
import sqlite3
import threading
from datetime import datetime, timezone

from .settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    tg_id INTEGER UNIQUE NOT NULL,
    title TEXT,
    username TEXT,
    enabled INTEGER DEFAULT 0,
    auto_found INTEGER DEFAULT 0,
    last_msg_id INTEGER DEFAULT 0,
    posts INTEGER DEFAULT 0,
    is_chat INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS raw_posts (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    date TEXT,
    text TEXT,
    buttons TEXT,
    url TEXT,
    hash TEXT,
    stage TEXT DEFAULT 'new',   -- new | short | prefilter_fail | not_vacancy | duplicate | vacancy | llm_error
    author TEXT,                -- @username автора (для чатов — это и есть контакт)
    UNIQUE(channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_raw_hash ON raw_posts(hash);
CREATE INDEX IF NOT EXISTS ix_raw_stage ON raw_posts(stage);
CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY,
    hash TEXT UNIQUE,
    title TEXT, category TEXT, company TEXT, vertical TEXT, format TEXT, location TEXT,
    salary_from INTEGER, salary_to INTEGER, currency TEXT, salary_raw TEXT,
    experience TEXT, stack TEXT, summary TEXT,
    relevance INTEGER DEFAULT 0, relevance_reason TEXT,
    first_seen TEXT, last_seen TEXT,
    status TEXT DEFAULT 'Новая', notes TEXT DEFAULT '',
    model TEXT,
    job_type TEXT
);
CREATE INDEX IF NOT EXISTS ix_vac_rel ON vacancies(relevance);
CREATE TABLE IF NOT EXISTS vacancy_sources (
    vacancy_id INTEGER NOT NULL,
    raw_post_id INTEGER NOT NULL,
    PRIMARY KEY (vacancy_id, raw_post_id)
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    vacancy_id INTEGER NOT NULL,
    type TEXT, value TEXT,
    UNIQUE(vacancy_id, type, value)
);
CREATE TABLE IF NOT EXISTS join_log (
    target TEXT PRIMARY KEY,
    result TEXT,
    at TEXT
);
"""

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(SCHEMA)
# миграция для баз, созданных до появления колонок
for _t, _c, _d in (("channels", "is_chat", "INTEGER DEFAULT 0"), ("raw_posts", "author", "TEXT"),
                   ("vacancies", "job_type", "TEXT"), ("vacancies", "draft", "TEXT"),
                   ("channels", "discovered", "TEXT"), ("channels", "in_folder", "INTEGER DEFAULT 0"),
                   ("vacancies", "backup", "INTEGER DEFAULT 0"),
                   ("vacancies", "flags", "TEXT"), ("vacancies", "prep", "TEXT"), ("vacancies", "cv_path", "TEXT"),
                   ("vacancies", "employment", "TEXT"), ("vacancies", "rf_only", "INTEGER DEFAULT 0"),
                   ("vacancies", "eu_only", "INTEGER DEFAULT 0")):   # дата, когда автопилот нашёл источник
    if _c not in {r[1] for r in _conn.execute(f"PRAGMA table_info({_t})")}:
        _conn.execute(f"ALTER TABLE {_t} ADD COLUMN {_c} {_d}")
_conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def q(sql: str, params=()) -> list[sqlite3.Row]:
    with _lock:
        return _conn.execute(sql, params).fetchall()


def one(sql: str, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql: str, params=()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


# ---------- channels ----------

def upsert_channel(tg_id: int, title: str, username: str | None, auto_found: bool, enabled_default: bool,
                   is_chat: bool = False):
    row = one("SELECT id FROM channels WHERE tg_id=?", (tg_id,))
    if row:
        x("UPDATE channels SET title=?, username=?, is_chat=? WHERE tg_id=?", (title, username, int(is_chat), tg_id))
        return
    x("INSERT INTO channels(tg_id,title,username,enabled,auto_found,is_chat) VALUES(?,?,?,?,?,?)",
      (tg_id, title, username, int(enabled_default), int(auto_found), int(is_chat)))


def channel_by_tg(tg_id: int):
    return one("SELECT * FROM channels WHERE tg_id=?", (tg_id,))


def enabled_channels():
    return q("SELECT * FROM channels WHERE enabled=1")


# ---------- raw posts ----------

def insert_raw(channel_id: int, msg_id: int, date: str, text: str, buttons: list[str], url: str, h: str,
               author: str | None = None) -> int | None:
    try:
        rid = x("INSERT INTO raw_posts(channel_id,msg_id,date,text,buttons,url,hash,author) VALUES(?,?,?,?,?,?,?,?)",
                (channel_id, msg_id, date, text, json.dumps(buttons, ensure_ascii=False), url, h, author))
    except sqlite3.IntegrityError:
        return None
    x("UPDATE channels SET posts=posts+1, last_msg_id=MAX(last_msg_id, ?) WHERE id=?", (msg_id, channel_id))
    return rid


def set_stage(raw_id: int, stage: str):
    x("UPDATE raw_posts SET stage=? WHERE id=?", (stage, raw_id))


# ---------- vacancies ----------

def vacancy_by_hash(h: str):
    return one("SELECT * FROM vacancies WHERE hash=?", (h,))


def add_source(vacancy_id: int, raw_id: int, date: str):
    x("INSERT OR IGNORE INTO vacancy_sources(vacancy_id, raw_post_id) VALUES(?,?)", (vacancy_id, raw_id))
    x("UPDATE vacancies SET last_seen=MAX(COALESCE(last_seen,''), ?) WHERE id=?", (date, vacancy_id))


def insert_vacancy(h: str, v: dict, date: str, model: str) -> int:
    return x("""INSERT INTO vacancies(hash,title,category,company,vertical,format,location,
                salary_from,salary_to,currency,salary_raw,experience,stack,summary,
                relevance,relevance_reason,first_seen,last_seen,model,job_type)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (h, v["title"], v["category"], v["company"], v["vertical"], v["format"], v["location"],
              v["salary_from"], v["salary_to"], v["currency"], v["salary_raw"], v["experience"],
              json.dumps(v["stack"], ensure_ascii=False), v["summary"],
              v["relevance"], v["relevance_reason"], date, date, model, v.get("job_type")))


def add_contacts(vacancy_id: int, contacts: list[tuple[str, str]]):
    for t, val in contacts:
        x("INSERT OR IGNORE INTO contacts(vacancy_id,type,value) VALUES(?,?,?)", (vacancy_id, t, val))
