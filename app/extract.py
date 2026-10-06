"""LLM-разбор поста в карточку вакансии + проверка контактов по исходному тексту."""
from . import llm
from .settings import CONFIG, profile_text
from .textutil import extract_contacts, tech_evidence

CATEGORIES = ["Разработка", "DevOps", "Интеграции", "Техспец", "Аналитика", "Медиабаинг", "Продажи", "Другое"]
TECH_CATEGORIES = ("Разработка", "DevOps", "Интеграции", "Техспец")
VERTICALS = ["iGaming", "Crypto", "Nutra", "Fintech", "Forex", "Товарка", "Dating", "E-commerce", "Другое"]
FORMATS = ["Удалёнка", "Офис", "Гибрид", "Не указан"]
JOB_TYPES = ["Постоянная", "Проект / фриланс", "Не указано"]

SYSTEM = """Ты разбираешь посты из Telegram-каналов с вакансиями и оцениваешь, подходят ли они кандидату.
Отвечай ТОЛЬКО JSON-объектом, без пояснений.

Профиль кандидата:
---
{profile}
---

Схема ответа:
{{
  "is_vacancy": true/false,          // false для рекламы, резюме соискателей, дайджестов со списком многих вакансий без деталей, новостей
  "title": "должность по-русски",
  "job_type": одно из {job_types},   // «Проект / фриланс» — разовая задача, «настроить / сделать под ключ», оплата за проект
  "category": одно из {categories},
  "company": "название или пустая строка",
  "vertical": одно из {verticals},
  "format": одно из {formats},
  "location": "город/страна или пустая строка",
  "salary_from": число или null,     // в месяц, в валюте поста
  "salary_to": число или null,
  "currency": "USD/EUR/RUB/UAH/USDT/... или пустая строка",
  "salary_raw": "как написано в посте или пустая строка",
  "experience": "требуемый опыт кратко или пустая строка",
  "stack": ["технологии/инструменты из поста"],
  "summary": "2-3 предложения ПО-РУССКИ: чем заниматься и что предлагают",
  "contacts": ["@username", "https://...", "email"],   // ТОЛЬКО то, что буквально есть в тексте, для отклика. Не подпись канала.
  "relevance": 0-100,               // насколько подходит кандидату по профилю
  "relevance_reason": "одна короткая фраза по-русски почему"
}}

Правила:
- Ничего не выдумывай. Нет данных — пустая строка или null.
- Если в посте несколько разных вакансий — опиши самую подходящую кандидату.
- Медиабаинг, продажи, HR, саппорт, операторы — relevance не выше 20.
- Сообщение из чата: короткий запрос вроде «нужен техспец настроить постбеки, кто может?» — это ТОЖЕ вакансия (is_vacancy=true),
  обычно job_type=«Проект / фриланс». Человек, который сам ищет работу или предлагает свои услуги, — НЕ вакансия.
"""


def _choice(value, allowed: list[str], default: str) -> str:
    value = (value or "").strip()
    for a in allowed:
        if value.lower() == a.lower():
            return a
    return default


def _num(v):
    try:
        n = int(float(str(v).replace(" ", "").replace(",", ".")))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _norm_contact(c: str) -> str:
    """'https://t.me/Hr_X/' и '@hr_x' -> '@hr_x'"""
    c = c.strip().lower().rstrip("/")
    for prefix in ("https://", "http://"):
        c = c.removeprefix(prefix)
    for prefix in ("t.me/", "telegram.me/"):
        if c.startswith(prefix) and not c.startswith(prefix + "+"):
            return "@" + c.removeprefix(prefix)
    return c


def _s(v) -> str:
    return str(v).strip() if v not in (None, "null") else ""


async def analyze(text: str, buttons: list[str], channel_username: str | None, is_chat: bool = False,
                  author: str | None = None) -> tuple[dict | None, list[tuple[str, str]], str]:
    """Возвращает (карточка | None если не вакансия, контакты, модель)."""
    max_chars = CONFIG["llm"]["max_input_chars"]
    post = text[:max_chars]
    if buttons:
        post += "\n\nКнопки/ссылки поста:\n" + "\n".join(buttons)
    if is_chat:
        post = f"[Сообщение из чата от {author or 'автора без username'}]\n{post}"
    system = SYSTEM.format(profile=profile_text(), categories=CATEGORIES, verticals=VERTICALS, formats=FORMATS,
                           job_types=JOB_TYPES)
    data, model = await llm.complete_json(system, post)

    if not data.get("is_vacancy"):
        return None, [], model

    stack = data.get("stack") or []
    card = {
        "title": _s(data.get("title")) or "Без названия",
        "job_type": _choice(data.get("job_type"), JOB_TYPES, "Не указано"),
        "category": _choice(data.get("category"), CATEGORIES, "Другое"),
        "company": _s(data.get("company")),
        "vertical": _choice(data.get("vertical"), VERTICALS, "Другое"),
        "format": _choice(data.get("format"), FORMATS, "Не указан"),
        "location": _s(data.get("location")),
        "salary_from": _num(data.get("salary_from")),
        "salary_to": _num(data.get("salary_to")),
        "currency": _s(data.get("currency")).upper(),
        "salary_raw": _s(data.get("salary_raw")),
        "experience": _s(data.get("experience")),
        "stack": [str(s).strip() for s in stack if str(s).strip()][:15] if isinstance(stack, list) else [],
        "summary": _s(data.get("summary")),
        "relevance": max(0, min(100, _num(data.get("relevance")) or 0)),
        "relevance_reason": _s(data.get("relevance_reason")),
    }

    # Модель могла «додумать» техническую роль (так было с колл-центром: «Team Leaders» → «Head of Tech»)
    if card["category"] in TECH_CATEGORIES and not tech_evidence(text):
        card["category"] = "Другое"
        card["relevance"] = min(card["relevance"], 20)
        card["relevance_reason"] = "в тексте нет технических слов, роль от модели не подтверждена. " + card["relevance_reason"]

    # Контакты: берём только найденные regex'ом в самом посте.
    # LLM используется как подсказка, какие из них для отклика (а не подпись канала).
    found = extract_contacts(text, buttons)
    own = ("@" + channel_username.lower()) if channel_username else None
    found = [(t, v) for t, v in found if v.lower() != own]
    llm_picks = {_norm_contact(c) for c in (data.get("contacts") or []) if isinstance(c, str)}
    picked = [(t, v) for t, v in found if _norm_contact(v) in llm_picks]
    contacts = picked or found
    # в чатах автор поста — запасной контакт: после указанного в тексте или единственный («пишите в лс»)
    if author and _norm_contact(author) not in {_norm_contact(v) for _, v in contacts}:
        contacts = contacts + [("telegram", author)]
    return card, contacts, model
