"""Цепочка OpenAI-совместимых провайдеров (Groq -> Ollama). Возвращает JSON-объект."""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from . import net
from .settings import CONFIG, DATA, env

log = logging.getLogger("llm")


class Provider:
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.model = cfg["model"]
        self.base_url = (env(cfg["base_url_env"]) if cfg.get("base_url_env") else cfg["base_url"]).rstrip("/")
        self.api_key = env(cfg["api_key_env"]) if cfg.get("api_key_env") else ""
        proxy = env(cfg["proxy_env"]) if cfg.get("proxy_env") else ""
        # облачные модели — через общий VPN-прокси радара; локальная Ollama — напрямую
        if not proxy and not any(h in self.base_url for h in ("localhost", "127.0.0.1")):
            proxy = net.httpx_proxy() or ""
        self.min_interval = float(cfg.get("min_interval_sec", 0))
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=CONFIG["llm"]["timeout_sec"], proxy=proxy or None)
        # провайдер с ключом, но без ключа — пропускаем
        self.enabled = bool(self.base_url) and (not cfg.get("api_key_env") or bool(self.api_key))
        self.cooldown_until = 0.0
        self.daily_limit = int(cfg.get("daily_limit", 0))  # 0 = без лимита

    def over_daily_limit(self) -> bool:
        return bool(self.daily_limit) and _usage_today(self.name) >= self.daily_limit

    async def complete_json(self, system: str, user: str) -> dict:
        _count(self.name)
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        r = await self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        if r.status_code == 429:
            retry = float(r.headers.get("retry-after", "30") or 30)
            self.cooldown_until = time.monotonic() + retry
            raise RuntimeError(f"{self.name}: rate limit, пауза {retry:.0f}s")
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"] or ""
        return parse_json(content)


# ---------- учёт запросов за сутки (переживает перезапуск) ----------
_USAGE_FILE = DATA / "llm_usage.json"


def _load_usage() -> dict:
    try:
        u = json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        u = {}
    today = datetime.now(timezone.utc).date().isoformat()   # квота Google сбрасывается по UTC-ish полуночи
    return u if u.get("date") == today else {"date": today, "counts": {}}


def _usage_today(name: str) -> int:
    return _load_usage()["counts"].get(name, 0)


def _count(name: str):
    u = _load_usage()
    u["counts"][name] = u["counts"].get(name, 0) + 1
    _USAGE_FILE.write_text(json.dumps(u), encoding="utf-8")


def usage_summary() -> str:
    u = _load_usage()["counts"]
    return ", ".join(f"{p.name}: {u.get(p.name, 0)}" + (f"/{p.daily_limit}" if p.daily_limit else "")
                     for p in _providers if p.enabled)


def parse_json(content: str) -> dict:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


_providers = [Provider(c) for c in CONFIG["llm"]["chain"]]


def active_providers() -> list[str]:
    return [f"{p.name} ({p.model})" for p in _providers if p.enabled]


async def complete_json(system: str, user: str) -> tuple[dict, str]:
    """Пробует провайдеров по порядку. Возвращает (json, 'provider/model')."""
    errors = []
    for p in _providers:
        if not p.enabled or time.monotonic() < p.cooldown_until or p.over_daily_limit():
            continue
        try:
            return await p.complete_json(system, user), f"{p.name}/{p.model}"
        except Exception as e:  # сеть, лимит, битый JSON — идём к следующему
            log.warning("LLM %s: %r", p.name, e)
            errors.append(f"{p.name}: {e!r}")
    raise RuntimeError("все LLM недоступны: " + " | ".join(errors) if errors else "нет активных LLM в config.yaml/.env")


async def complete_local(system: str, user: str) -> tuple[dict, str]:
    """Только локальные провайдеры (Ollama): для фоновых задач, на которые жалко квоту Gemini."""
    for p in _providers:
        if p.enabled and any(h in p.base_url for h in ("localhost", "127.0.0.1")):
            return await p.complete_json(system, user), f"{p.name}/{p.model}"
    raise RuntimeError("локальная модель не настроена (OLLAMA_BASE_URL)")
