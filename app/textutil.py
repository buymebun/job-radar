import hashlib
import re

from .settings import CONFIG

_URL = re.compile(r"https?://\S+", re.I)
_NON_WORD = re.compile(r"[^\w]+", re.U)

# Контакты
_TG_USER = re.compile(r"(?<![\w/@.])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
_TG_LINK = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(\+?[A-Za-z0-9_\-]{4,64})(?:/\d+)?", re.I)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_WEB = re.compile(r"https?://(?!t\.me|telegram\.me)[^\s)>\]\"']+", re.I)
# «Контакты: Gorskines» — username без @ после метки контакта
_TG_BARE = re.compile(r"(?:контакт\w*|связь|писать|пишите|telegram|телеграм|tg|тг|hr)\s*[:\-—–]\s*"
                      r"([A-Za-z][A-Za-z0-9_]{4,31})\b(?![@.:/])", re.I)

# Служебные t.me пути, которые не являются контактом
_TG_SKIP = {"joinchat", "share", "addstickers", "proxy", "socks", "iv", "c", "s"}


def dedup_hash(text: str) -> str:
    """Хеш по «смысловому» тексту: без ссылок, эмодзи, пунктуации и регистра.
    Берём первые 600 символов — хвосты с подписью канала различаются, суть совпадает."""
    t = _URL.sub(" ", text.lower())
    t = _NON_WORD.sub(" ", t)
    t = " ".join(t.split())[:600]
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def extract_contacts(text: str, buttons: list[str]) -> list[tuple[str, str]]:
    blob = text + "\n" + "\n".join(buttons)
    found: list[tuple[str, str]] = []
    for m in _EMAIL.finditer(blob):
        found.append(("email", m.group(0).lower()))
    emails = {v for _, v in found}
    for m in _TG_USER.finditer(blob):
        u = m.group(1)
        if not any(e.startswith(u.lower() + "@") or ("@" + u.lower()) in e for e in emails):
            found.append(("telegram", "@" + u))
    for m in _TG_BARE.finditer(blob):
        found.append(("telegram", "@" + m.group(1)))
    for m in _TG_LINK.finditer(blob):
        path = m.group(1)
        if path.lower() in _TG_SKIP:
            continue
        if path.startswith("+"):
            found.append(("link", "https://t.me/" + path))
        else:
            found.append(("telegram", "@" + path))
    for m in _WEB.finditer(blob):
        found.append(("link", m.group(0).rstrip(".,;")))
    # уникальность без учёта регистра, порядок сохраняем
    seen, out = set(), []
    for t, v in found:
        k = (t, v.lower())
        if k not in seen:
            seen.add(k)
            out.append((t, v))
    return out


_pattern_cache: dict[str, re.Pattern] = {}


def _hits(text: str, words: list[str]) -> list[str]:
    """Совпадение по началу слова: «бот» ловит «боты», но не «работа»; «интеграц» ловит «интеграции»."""
    out = []
    for w in words:
        p = _pattern_cache.get(w)
        if p is None:
            core = w.strip() or w
            # пробел в конце слова в конфиге = «слово целиком»: "git " не ловит github, "capi " не ловит capital
            end = r"(?!\w)" if w.endswith(" ") and w.strip() else ""
            p = _pattern_cache[w] = re.compile(r"(?<!\w)" + re.escape(core) + end)
        if p.search(text):
            out.append(w)
    return out


def tech_evidence(text: str) -> list[str]:
    """Технические слова в самом тексте поста (проверка, что модель не придумала техническую роль)."""
    return _hits(" " + (text or "").lower() + " ", CONFIG["prefilter"].get("tech_evidence", []))


def is_resume(text: str) -> bool:
    """Пост соискателя («#резюме», «Обо мне… Ожидания по зарплате…»), а не вакансия."""
    cfg = CONFIG["prefilter"]
    t = " " + text.lower() + " "
    if any(tag in t for tag in cfg.get("resume_tags", [])):
        return True
    return len(_hits(t, cfg.get("resume_phrases", []))) >= 2


def prefilter_chat(text: str) -> tuple[bool, str]:
    """Для чатов строже: нужен запрос («ищу / нужен / кто может…») И техническое слово.
    Предложения своих услуг и резюме соискателей отсекаем."""
    cfg = CONFIG["prefilter"]
    t = " " + text.lower() + " "
    if is_resume(text) or _hits(t, cfg.get("chat_offer_markers", [])):
        return False, "предлагает услуги / резюме"
    req = _hits(t, cfg.get("chat_request_markers", []))
    plus = _hits(t, cfg["plus"])
    if req and plus:
        return True, "запрос: " + ", ".join((req + plus)[:5])
    return False, "нет запроса на техспеца"


def prefilter(text: str) -> tuple[bool, str]:
    """(пропустить_в_LLM, причина)"""
    cfg = CONFIG["prefilter"]
    if is_resume(text):
        return False, "резюме соискателя"
    t = " " + text.lower() + " "
    plus = _hits(t, cfg["plus"])
    minus = _hits(t, cfg["minus"])
    marker = _hits(t, cfg["vacancy_markers"])
    if not marker and not plus:
        return False, "нет признаков вакансии"
    if cfg.get("exclude_non_tech") and minus and not plus:
        return False, "нетех: " + ", ".join(minus[:3])
    return True, "плюс: " + ", ".join(plus[:5]) if plus else "похоже на вакансию"
