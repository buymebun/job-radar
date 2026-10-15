import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

with open(ROOT / "config.yaml", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)


def profile_text() -> str:
    # читаем каждый раз, чтобы правки profile.md подхватывались без перезапуска
    p = ROOT / "profile.md"
    return (p if p.exists() else ROOT / "profile.example.md").read_text(encoding="utf-8")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


TG_API_ID = int(env("TG_API_ID") or 0)
TG_API_HASH = env("TG_API_HASH")
TG_SESSION = str(DATA / (env("TG_SESSION") or "parser"))
DB_PATH = DATA / "jobs.db"
WEB_HOST = env("WEB_HOST", "127.0.0.1")
WEB_PORT = int(env("WEB_PORT", "8765"))
NOTIFY_BOT_TOKEN = env("NOTIFY_BOT_TOKEN")
NOTIFY_CHAT_ID = env("NOTIFY_CHAT_ID")
# Доступ из домашней сети: логин/пароль для всех, кроме самого ноута; адрес для ссылок в уведомлениях
DASH_USER = env("DASH_USER", "admin")
DASH_PASSWORD = env("DASH_PASSWORD")
PUBLIC_URL = (env("PUBLIC_URL") or f"http://{'127.0.0.1' if WEB_HOST in ('0.0.0.0', '') else WEB_HOST}:{WEB_PORT}").rstrip("/")
STATUSES = CONFIG["statuses"]
CV_ROOT = Path(env("CV_ROOT") or ROOT / "cv")   # папка с CV и трекером откликов

APP_NAME = "HRmagistr"
APP_VERSION = "1.1"
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
