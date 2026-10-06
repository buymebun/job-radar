"""Отдельное VPN-подключение для радара: весь трафик (Telegram, LLM, бот уведомлений) — через один прокси.
PROXY_URL в .env: socks5://[user:pass@]host:port  (или http://…). Пусто — прямое соединение.
Kill-switch: если прокси задан, но не отвечает — радар не стартует и не уходит в прямое соединение."""
import asyncio
import logging
import socket
from urllib.parse import unquote, urlparse

import httpx

from .settings import env

log = logging.getLogger("net")
PROXY_URL = env("PROXY_URL")


def telethon_proxy() -> dict | None:
    """Формат прокси для Telethon (через python-socks)."""
    if not PROXY_URL:
        return None
    u = urlparse(PROXY_URL)
    scheme = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "http": "http", "https": "http"}[u.scheme]
    p = {"proxy_type": scheme, "addr": u.hostname, "port": u.port, "rdns": True}
    if u.username:
        p.update(username=unquote(u.username), password=unquote(u.password or ""))
    return p


def httpx_proxy() -> str | None:
    if not PROXY_URL:
        return None
    # socks5h — DNS тоже через прокси (не светим запросы к api.telegram.org / googleapis у провайдера)
    return PROXY_URL.replace("socks5://", "socks5h://", 1) if PROXY_URL.startswith("socks5://") else PROXY_URL


def client(timeout: float = 30) -> httpx.AsyncClient:
    """Единая фабрика HTTP-клиентов: всё через прокси, если он задан."""
    return httpx.AsyncClient(timeout=timeout, proxy=httpx_proxy())


async def check() -> str:
    """Проверка перед стартом. Возвращает внешний IP или бросает исключение (kill-switch)."""
    if PROXY_URL:
        u = urlparse(PROXY_URL)
        try:
            with socket.create_connection((u.hostname, u.port), timeout=5):
                pass
        except OSError as e:
            raise RuntimeError(f"прокси {u.hostname}:{u.port} не отвечает ({e}). VPN-клиент запущен?") from e
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            async with client(15) as c:
                r = await c.get(url)
                r.raise_for_status()
                return r.text.strip()
        except Exception as e:
            last = e
            await asyncio.sleep(1)
    if PROXY_URL:
        raise RuntimeError(f"через прокси нет выхода в интернет: {last!r}")
    return "не удалось определить"
