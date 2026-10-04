"""
Production-ready бот-провайдер для Jivo Bot API.

Поток:
  Jivo --CLIENT_MESSAGE--> этот сервер
    * рабочее время -> INVITE_AGENT
    * нерабочее     -> ответ по базе знаний ИЛИ общий автоответ + уведомление в Telegram
  Jivo --AGENT_UNAVAILABLE--> общий автоответ (один раз за cooldown)
  Jivo --CHAT_CLOSED--> очистка состояния чата

Особенности:
  * webhook отвечает быстро; тяжёлая работа выполняется в ThreadPoolExecutor;
  * защита от повторных событий;
  * cooldown автоответов и Telegram-уведомлений;
  * шаблоны автоматически перечитываются при изменении файла;
  * опциональный Redis для запуска нескольких worker-процессов;
  * при отсутствии Redis используется память процесса (тогда рекомендуется 1 worker).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

from out_of_hours import MessageContext, handle_out_of_hours
from omnidesk_client import OmniDeskClient, normalize_omnidesk_payload
from support_store import SupportStore


# -----------------------------------------------------------------------------
# Настройки
# -----------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
JIVO_PROVIDER_ID = os.environ["JIVO_PROVIDER_ID"]

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")
try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = datetime.now().astimezone().tzinfo or timezone.utc
WORK_START = int(os.getenv("WORK_START", "9"))
WORK_END = int(os.getenv("WORK_END", "19"))
WORK_DAYS = {
    int(d.strip())
    for d in os.getenv("WORK_DAYS", "0,1,2,3,4").split(",")
    if d.strip()
}

AUTO_REPLY = os.getenv(
    "AUTO_REPLY",
    "Здравствуйте! Сейчас мы не на связи. Мы работаем в будни с 9:00 до 19:00. "
    "Оставьте ваш вопрос и контакты, и мы ответим, как только вернёмся.",
)

# Если найден точный шаблон, общий автоответ по умолчанию НЕ отправляем.
# При желании можно добавить короткий хвост к шаблонному ответу.
TEMPLATE_FOOTER = os.getenv("TEMPLATE_FOOTER", "").strip()

TEMPLATES_FILE = Path(os.getenv("TEMPLATES_FILE", "templates.json"))
MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "0.50"))
MATCH_MARGIN = float(os.getenv("MATCH_MARGIN", "0.08"))

EVENT_TTL = int(os.getenv("EVENT_TTL", "600"))
REPLY_COOLDOWN = int(os.getenv("REPLY_COOLDOWN", str(12 * 3600)))
TG_COOLDOWN = int(os.getenv("TG_COOLDOWN", "600"))

REQUEST_TIMEOUT_CONNECT = float(os.getenv("REQUEST_TIMEOUT_CONNECT", "3"))
REQUEST_TIMEOUT_READ = float(os.getenv("REQUEST_TIMEOUT_READ", "7"))
REQUEST_TIMEOUT = (REQUEST_TIMEOUT_CONNECT, REQUEST_TIMEOUT_READ)

BACKGROUND_WORKERS = int(os.getenv("BACKGROUND_WORKERS", "8"))
REDIS_URL = os.getenv("REDIS_URL")
SUPPORT_DB_PATH = Path(os.getenv("SUPPORT_DB_PATH", "support.sqlite3"))
OMNI_WEBHOOK_TOKEN = os.getenv("OMNI_WEBHOOK_TOKEN", "")

JIVO_URL = f"https://bot.jivosite.com/webhooks/{JIVO_PROVIDER_ID}/{BOT_TOKEN}"


# -----------------------------------------------------------------------------
# Логирование / Flask / HTTP
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("jivo-bot")
if str(TZ) in {"UTC", "UTC+00:00"} and TIMEZONE_NAME not in {"UTC", "Etc/UTC"}:
    log.warning(
        "Таймзона %r недоступна; использую системное локальное время %s. Для Europe/Moscow можно установить tzdata.",
        TIMEZONE_NAME,
        TZ,
    )

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=BACKGROUND_WORKERS, thread_name_prefix="jivo-bg")
http = requests.Session()
support_store = SupportStore(SUPPORT_DB_PATH)
omnidesk = OmniDeskClient(http)


# -----------------------------------------------------------------------------
# Состояние: Redis при наличии, иначе память процесса
# -----------------------------------------------------------------------------
class MemoryState:
    """Потокобезопасное состояние внутри одного процесса."""

    name = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, float] = {}

    def _cleanup(self) -> None:
        now = time.time()
        expired = [k for k, expires_at in self._values.items() if expires_at <= now]
        for key in expired:
            self._values.pop(key, None)

    def set_if_absent(self, key: str, ttl: int) -> bool:
        """True, если ключ создан сейчас; False, если ещё существует."""
        with self._lock:
            self._cleanup()
            if key in self._values:
                return False
            self._values[key] = time.time() + ttl
            return True

    def delete(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._values.pop(key, None)


class RedisState:
    name = "redis"

    def __init__(self, url: str) -> None:
        import redis  # type: ignore

        self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._redis.ping()

    def set_if_absent(self, key: str, ttl: int) -> bool:
        return bool(self._redis.set(key, "1", ex=ttl, nx=True))

    def delete(self, *keys: str) -> None:
        if keys:
            self._redis.delete(*keys)


def build_state():
    if not REDIS_URL:
        log.warning(
            "REDIS_URL не задан: состояние хранится в памяти процесса. "
            "Для нескольких gunicorn workers подключите Redis."
        )
        return MemoryState()

    try:
        state = RedisState(REDIS_URL)
        log.info("Состояние хранится в Redis")
        return state
    except Exception as exc:
        log.exception("Redis недоступен, переключаюсь на память процесса: %s", exc)
        return MemoryState()


state = build_state()


def event_key(event_id: str) -> str:
    return f"jivo:event:{event_id}"


def reply_key(chat_id: str) -> str:
    return f"jivo:reply:{chat_id}"


def telegram_key(chat_id: str) -> str:
    return f"jivo:telegram:{chat_id}"


# -----------------------------------------------------------------------------
# Вспомогательные функции
# -----------------------------------------------------------------------------
def is_working_time(now: datetime | None = None) -> bool:
    current = now or datetime.now(TZ)
    return current.weekday() in WORK_DAYS and WORK_START <= current.hour < WORK_END


def required_str(data: dict[str, Any], field: str) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def get_message_text(data: dict[str, Any]) -> str:
    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    return str(message.get("text") or "").strip()


# -----------------------------------------------------------------------------
# База шаблонов / улучшенный гибридный поиск
# -----------------------------------------------------------------------------
_tpl_lock = threading.RLock()
_tpl_state: dict[str, Any] = {
    "mtime": None,
    "default": None,
    "items": [],
    "idf": {},
}
_MATCH_METRICS: dict[str, int] = {"matched": 0, "not_found": 0, "ambiguous": 0}


def reset_match_metrics() -> None:
    with _tpl_lock:
        for key in _MATCH_METRICS:
            _MATCH_METRICS[key] = 0


def record_match_metric(kind: str) -> None:
    if kind not in _MATCH_METRICS:
        raise ValueError(f"Unknown match metric: {kind}")
    with _tpl_lock:
        _MATCH_METRICS[kind] += 1


def get_match_metrics() -> dict[str, int]:
    with _tpl_lock:
        return dict(_MATCH_METRICS)


MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "0.52"))
MATCH_MARGIN = float(os.getenv("MATCH_MARGIN", "0.08"))
FUZZY_THRESHOLD = float(os.getenv("FUZZY_THRESHOLD", "0.86"))

_WORD = re.compile(r"[a-zа-я0-9]+", re.IGNORECASE)
_STOP = {
    "что", "как", "это", "для", "или", "при", "над", "под", "про", "все", "вас", "нам",
    "нас", "мне", "наш", "наши", "ваш", "ваши", "его", "ему", "они", "она", "оно",
    "есть", "быть", "если", "нужно", "можно", "подскажите", "пожалуйста", "добрый",
    "день", "здравствуйте", "клиент", "хотим", "прошу", "почему", "когда", "где", "ли",
    "могу", "можем", "хочу", "хотел", "скажите", "подскажи",
    "за", "по", "на", "из", "от", "до", "не", "ни", "у", "с", "со", "в", "во", "к", "ко", "и", "а", "но", "же",
}

# Нормализация терминов и популярных формулировок из базы REDSMS.
_SYNONYMS = {
    "смс": "sms", "sms": "sms",
    "флешкол": "flashcall", "флэшкол": "flashcall", "fcall": "flashcall", "flash-call": "flashcall",
    "wait-call": "waitcall", "wcall": "waitcall",
    "пушок": "pushok", "push-ok": "pushok", "sim-push": "pushok", "simpush": "pushok",
    "голосовой": "voice", "голос": "voice",
    "вебхук": "webhook", "callback": "webhook", "коллбек": "webhook",
    "апи": "api", "httpapi": "api",
    "тг": "telegram", "телеграм": "telegram",
    "вконтакте": "vk", "вк": "vk",
    "платеж": "оплата", "платёж": "оплата", "платежка": "оплата", "оплатили": "оплата",
    "оплатить": "оплата", "пополнили": "пополнение", "пополнить": "пополнение",
    "деньги": "баланс", "средства": "баланс", "счет": "баланс", "счёт": "баланс",
    "зачислили": "зачисление", "зачислить": "зачисление", "поступили": "зачисление",
    "упд": "закрывающие", "акт": "закрывающие", "акты": "закрывающие",
    "отправитель": "sender", "отправителя": "sender", "имя": "sender",
    "логин": "account", "аккаунт": "account", "кабинет": "account", "лк": "account",
    "доставка": "delivery", "доставки": "delivery", "доставку": "delivery", "доставить": "delivery",
    "доходит": "delivery", "доходят": "delivery",
    "приходит": "delivery", "приходят": "delivery", "получил": "delivery", "получает": "delivery",
    "недоставлено": "undelivered", "недоставленное": "undelivered", "недоставленные": "undelivered",
    "недоставленного": "undelivered", "недоставлен": "undelivered", "недоставка": "undelivered",
    "списали": "debit", "списано": "debit", "списание": "debit", "списывают": "debit",
    "дошло": "delivery", "дошли": "delivery", "дойдет": "delivery", "дойдёт": "delivery",
    "тариф": "price", "тарифы": "price", "цена": "price", "цены": "price", "стоимость": "price",
    "модерация": "moderation", "модерации": "moderation",
    "лимит": "limit", "лимиты": "limit", "ограничение": "limit", "ограничения": "limit",
    "ошибка": "error", "ошибку": "error",
    "статус": "status", "статусы": "status",
    "регистрация": "register", "зарегистрировать": "register", "зарегистрировал": "register",
    "подключить": "connect", "подключение": "connect", "настроить": "setup", "настройка": "setup",
}

_STRONG = {
    "sms", "hlr", "flashcall", "waitcall", "pushok", "voice", "webhook", "api", "vk", "viber",
    "telegram", "401", "404", "2fa",
}

_RU_SUFFIXES = tuple(sorted({
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "его", "ов", "ев", "ей",
    "ий", "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ам", "ям", "ах", "ях",
    "ом", "ем", "ою", "ею", "ить", "ыть", "ать", "ять", "еть", "ует", "ют", "ет", "ит",
    "или", "али", "яет", "ают", "у", "ю", "а", "я", "ы", "и", "ь",
}, key=len, reverse=True))


def _norm(text: str) -> str:
    text = str(text).lower().replace("ё", "е")
    text = text.replace("flash call", "flashcall").replace("wait call", "waitcall")
    text = text.replace("push ok", "pushok").replace("http api", "httpapi")
    return " ".join(text.split())


def _stem(token: str) -> str:
    token = _SYNONYMS.get(token, token)
    if token in _STRONG or len(token) <= 4 or not re.fullmatch(r"[а-я]+", token):
        return token
    for suffix in _RU_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[:-len(suffix)]
    return token


def _tokens(text: str) -> list[str]:
    result = []
    for raw in _WORD.findall(_norm(text)):
        raw = raw.strip("._-+")
        if not raw or raw in _STOP:
            continue
        token = _stem(_SYNONYMS.get(raw, raw))
        if token and token not in _STOP:
            result.append(token)
    return result


def _ngrams(tokens: list[str], n: int) -> set[str]:
    return {" ".join(tokens[i:i+n]) for i in range(len(tokens) - n + 1)} if len(tokens) >= n else set()


def _parse_kb(raw: dict[str, Any]) -> list[dict[str, Any]]:
    categories = {
        str(c.get("id")): str(c.get("label") or "")
        for c in raw.get("categories", []) if isinstance(c, dict)
    }
    items: list[dict[str, Any]] = []
    for entry in raw.get("data", []):
        if not isinstance(entry, dict):
            continue
        details = entry.get("details") or {}
        if not isinstance(details, dict):
            continue
        response = str(details.get("response") or "").strip()
        if entry.get("status") != "ready" or not response:
            continue

        for link in details.get("links") or []:
            if isinstance(link, (list, tuple)) and len(link) == 2:
                label, url = link
                if url and str(url) not in response:
                    response += f"\n{label}: {url}"

        title = str(entry.get("title") or "")
        description = str(entry.get("description") or "")
        trigger = re.sub(r"[«»]|\s·\s", " ", str(details.get("trigger") or ""))
        category_id = str(entry.get("category") or "")
        category = categories.get(category_id, category_id)

        fields = {
            "title": set(_tokens(title)),
            "trigger": set(_tokens(trigger)),
            "description": set(_tokens(description)),
            "category": set(_tokens(category.replace("-", " "))),
        }
        all_tokens = set().union(*fields.values())
        phrase_tokens = _tokens(f"{title} {trigger} {description}")
        phrases = _ngrams(phrase_tokens, 2) | _ngrams(phrase_tokens, 3)

        items.append({
            "id": entry.get("id"),
            "title": title,
            "category": category_id,
            "text": response,
            "fields": fields,
            "tokens": all_tokens,
            "phrases": phrases,
        })
    return items


def _parse_simple(raw: Any) -> list[dict[str, Any]]:
    source = raw.get("templates", []) if isinstance(raw, dict) else raw
    if not isinstance(source, list):
        return []
    out = []
    for template in source:
        if not isinstance(template, dict):
            continue
        keywords = [_norm(k) for k in template.get("keywords", []) if k]
        text = str(template.get("text") or "").strip()
        if keywords and text:
            out.append({"id": template.get("id"), "keywords": keywords, "text": text})
    return out


def load_templates() -> None:
    with _tpl_lock:
        try:
            mtime = TEMPLATES_FILE.stat().st_mtime_ns
        except FileNotFoundError:
            _tpl_state.update(mtime=None, default=None, items=[], idf={})
            return
        if mtime == _tpl_state["mtime"]:
            return
        try:
            raw = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Не удалось прочитать %s: %s. Оставляю предыдущие шаблоны.", TEMPLATES_FILE, exc)
            return

        default = raw.get("default") if isinstance(raw, dict) else None
        items = _parse_kb(raw) if isinstance(raw, dict) and "data" in raw else _parse_simple(raw)

        df: dict[str, int] = {}
        for item in items:
            for token in item.get("tokens", set()):
                df[token] = df.get(token, 0) + 1
        n = max(len(items), 1)
        idf = {token: math.log(1 + n / count) for token, count in df.items()}
        _tpl_state.update(mtime=mtime, default=default, items=items, idf=idf)
        log.info("Загружено шаблонов: %d (%s)", len(items), TEMPLATES_FILE)


def _fuzzy_map(tokens: list[str], vocabulary: set[str]) -> dict[str, str]:
    from difflib import SequenceMatcher
    result: dict[str, str] = {}
    for token in tokens:
        if token in vocabulary or len(token) < 5 or token in _STRONG:
            continue
        best = (0.0, None)
        for candidate in vocabulary:
            if len(candidate) < 5 or candidate[0] != token[0] or abs(len(candidate) - len(token)) > 3:
                continue
            ratio = SequenceMatcher(None, token, candidate).ratio()
            if ratio > best[0]:
                best = (ratio, candidate)
        if best[1] and best[0] >= FUZZY_THRESHOLD:
            result[token] = best[1]
    return result


def _kb_score(template: dict[str, Any], query: list[str], qphrases: set[str], idf: dict[str, float]) -> float:
    qset = set(query)
    doc = template["tokens"]
    matched = qset & doc
    if not matched:
        return 0.0

    strong_q = qset & _STRONG
    if strong_q and not (strong_q & doc):
        return 0.0

    total = sum(idf.get(t, 1.0) for t in qset)
    covered = sum(idf.get(t, 1.0) for t in matched)
    coverage = covered / max(total, 1e-9)

    field_score = 0.0
    weights = {"title": 2.7, "trigger": 2.3, "description": 1.4, "category": 0.6}
    for token in matched:
        best_weight = max((w for name, w in weights.items() if token in template["fields"][name]), default=0.0)
        field_score += idf.get(token, 1.0) * best_weight
    field_score /= max(total * 2.7, 1e-9)

    phrase_hits = len(qphrases & template["phrases"])
    phrase_bonus = min(0.18, phrase_hits * 0.06)
    entity_bonus = 0.08 if strong_q and strong_q <= doc else 0.0
    rare_bonus = min(0.08, 0.02 * sum(1 for t in matched if idf.get(t, 0) >= 2.0))

    score = 0.62 * coverage + 0.38 * field_score + phrase_bonus + entity_bonus + rare_bonus

    # Если пользователь не называл конкретный канал, узкоспециализированный шаблон
    # (например VK) не должен обгонять общую инструкцию по той же теме.
    extra_entities = (doc & _STRONG) - strong_q
    if extra_entities and not strong_q:
        score *= 0.82

    return min(1.25, score)


def find_template(text: str) -> dict[str, Any] | None:
    """Гибридный поиск по KB: поля с весами + IDF + синонимы + фразы + опечатки."""
    load_templates()
    msg = _norm(text or "")
    if not msg:
        record_match_metric("not_found")
        log.info("Поиск шаблона: query=%r result=not_found reason=empty_message", text)
        return None

    with _tpl_lock:
        items = list(_tpl_state["items"])
        idf = dict(_tpl_state["idf"])

    # Совместимость со старым простым templates.json.
    if items and "keywords" in items[0]:
        scored = []
        for template in items:
            matches = [kw for kw in template.get("keywords", []) if kw in msg]
            if matches:
                scored.append((1.0 + max(map(len, matches)) / 100.0, template))
        if not scored:
            record_match_metric("not_found")
            log.info("Поиск шаблона: query=%r result=not_found reason=no_keyword_match", text)
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        score, template = scored[0]
        record_match_metric("matched")
        log.info("Поиск шаблона: query=%r result=matched template_id=%s score=%.4f", text, template.get("id"), round(score, 4))
        return {"id": template.get("id"), "text": template["text"], "score": round(score, 4)}

    query = _tokens(msg)
    if not query:
        record_match_metric("not_found")
        log.info("Поиск шаблона: query=%r result=not_found reason=no_tokens_after_normalization", text)
        return None
    fuzzy = _fuzzy_map(query, set(idf))
    query = [fuzzy.get(t, t) for t in query]
    qphrases = _ngrams(query, 2) | _ngrams(query, 3)

    scored: list[tuple[float, dict[str, Any]]] = []
    for template in items:
        score = _kb_score(template, query, qphrases, idf)
        if score > 0:
            scored.append((score, template))
    if not scored:
        record_match_metric("not_found")
        log.info("Поиск шаблона: query=%r result=not_found reason=no_template_score", text)
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    threshold = MATCH_THRESHOLD - (0.04 if set(query) & _STRONG else 0.0)
    if best_score < threshold:
        record_match_metric("not_found")
        log.info("Поиск шаблона: query=%r result=not_found reason=below_threshold score=%.4f threshold=%.4f", text, best_score, threshold)
        return None

    if len(scored) > 1:
        second_score, second = scored[1]
        required_margin = 0.02 if best_score >= 1.00 else (0.04 if best_score >= 0.90 else MATCH_MARGIN)
        if best_score - second_score < required_margin and best.get("id") != second.get("id"):
            record_match_metric("ambiguous")
            log.info(
                "Поиск шаблона: query=%r result=ambiguous best=%s %.3f second=%s %.3f",
                text,
                best.get("id"),
                best_score,
                second.get("id"),
                second_score,
            )
            return None

    record_match_metric("matched")
    log.info("Поиск шаблона: query=%r result=matched template_id=%s score=%.4f fuzzy=%s", text, best.get("id"), best_score, fuzzy)
    return {
        "id": best.get("id"),
        "title": best.get("title"),
        "category": best.get("category"),
        "text": best["text"],
        "score": round(best_score, 4),
    }


def default_reply() -> str:
    load_templates()
    with _tpl_lock:
        configured = _tpl_state.get("default")
    return str(configured or AUTO_REPLY)


# -----------------------------------------------------------------------------
# Отправка в Jivo
# -----------------------------------------------------------------------------
def jivo_post(payload: dict[str, Any]) -> bool:
    event = payload.get("event", "UNKNOWN")
    is_handoff = event == "INVITE_AGENT"
    try:
        support_store.enqueue_outbound(
            str(payload["id"]),
            payload,
            is_handoff=is_handoff,
        )
    except Exception:
        log.exception("Не удалось сохранить исходящее событие Jivo %s", event)
        raise

    dispatch_outbound(str(payload["id"]))
    return True


def dispatch_outbound(job_id: str | None = None) -> bool:
    job = support_store.claim_outbound(job_id)
    if job is None:
        return False
    payload = job["payload"]
    event = payload.get("event", "UNKNOWN")
    try:
        if payload.get("provider") == "omnidesk":
            operation = payload.get("operation")
            if operation == "reply":
                omnidesk.send_reply(payload["case_id"], payload["content"])
            elif operation == "assign":
                omnidesk.assign_operator(payload["case_id"])
            else:
                raise ValueError(f"Unknown OmniDesk operation: {operation}")
            support_store.finish_outbound(job["job_id"])
            return True

        response = http.post(JIVO_URL, json=payload, timeout=REQUEST_TIMEOUT)
        if not response.ok:
            error = f"HTTP {response.status_code}: {response.text[:500]}"
            support_store.finish_outbound(job["job_id"], error=error)
            log.error("Jivo %s: %s", event, error)
            return False
        support_store.finish_outbound(job["job_id"])
        return True
    except requests.RequestException as exc:
        support_store.finish_outbound(job["job_id"], error=str(exc))
        log.error("Jivo %s request failed: %s", event, exc)
        return False


def send_text(client_id: str, chat_id: str, text: str) -> bool:
    return jivo_post(
        {
            "id": str(uuid.uuid4()),
            "client_id": client_id,
            "chat_id": chat_id,
            "message": {
                "type": "TEXT",
                "text": text,
                "timestamp": int(time.time()),
            },
            "event": "BOT_MESSAGE",
        }
    )


def invite_agent(client_id: str, chat_id: str) -> bool:
    return jivo_post(
        {
            "id": str(uuid.uuid4()),
            "client_id": client_id,
            "chat_id": chat_id,
            "event": "INVITE_AGENT",
        }
    )


def auto_reply_once(client_id: str, chat_id: str) -> bool:
    if not state.set_if_absent(reply_key(chat_id), REPLY_COOLDOWN):
        return False

    try:
        sent = send_text(client_id, chat_id, default_reply())
    except Exception:
        state.delete(reply_key(chat_id))
        raise
    if not sent:
        state.delete(reply_key(chat_id))
    return sent


# -----------------------------------------------------------------------------
# Telegram
# -----------------------------------------------------------------------------
def notify_telegram(data: dict[str, Any]) -> bool:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return False

    chat_id = required_str(data, "chat_id") or "unknown"
    if not state.set_if_absent(telegram_key(chat_id), TG_COOLDOWN):
        return False

    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    name = str(sender.get("name") or "без имени")
    page = str(sender.get("url") or "-")
    message = get_message_text(data) or "(сообщение без текста)"

    text = (
        "Новое сообщение в Jivo (вне рабочего времени)\n"
        f"От: {name}\n"
        f"Chat ID: {chat_id}\n"
        f"Страница: {page}\n\n"
        f"{message}"
    )

    try:
        response = http.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            log.error("Telegram: HTTP %s %s", response.status_code, response.text[:500])
            state.delete(telegram_key(chat_id))
            return False
        return True
    except requests.RequestException as exc:
        log.error("Telegram request failed: %s", exc)
        state.delete(telegram_key(chat_id))
        return False


# -----------------------------------------------------------------------------
# Обработка событий
# -----------------------------------------------------------------------------
def handle_client_message(data: dict[str, Any]) -> None:
    client_id = required_str(data, "client_id")
    chat_id = required_str(data, "chat_id")
    if not client_id or not chat_id:
        log.error("CLIENT_MESSAGE без client_id/chat_id: %r", data)
        return

    if is_working_time():
        invite_agent(client_id, chat_id)
        return

    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    ctx = MessageContext(
        channel="jivo",
        chat_id=chat_id,
        client_id=client_id,
        message_text=get_message_text(data),
        sender_name=str(sender.get("name") or "без имени"),
        page=str(sender.get("url") or ""),
        raw=data,
    )

    def send_text_wrapper(channel: str, client_id_value: str | None, chat_id_value: str, text: str) -> bool:
        if client_id_value is None:
            return False
        if channel == "jivo":
            return send_text(client_id_value, chat_id_value, text)
        return False

    def invite_wrapper(channel: str, client_id_value: str | None, chat_id_value: str) -> bool:
        if client_id_value is None:
            return False
        if channel == "jivo":
            return invite_agent(client_id_value, chat_id_value)
        return False

    def match_with_footer(text: str) -> dict[str, Any] | None:
        match = find_template(text)
        if not match:
            return None
        answer = match["text"]
        if TEMPLATE_FOOTER:
            answer = f"{answer.rstrip()}\n\n{TEMPLATE_FOOTER}"
        match = dict(match)
        match["text"] = answer
        return match

    def on_match(match: dict[str, Any]) -> None:
        log.info(
            "Шаблонный ответ: chat=%s template=%s score=%.4f",
            chat_id,
            match.get("id"),
            match.get("score", 0.0),
        )

    handle_out_of_hours(
        ctx,
        match_fn=match_with_footer,
        default_reply_fn=default_reply,
        send_text_fn=send_text_wrapper,
        invite_agent_fn=invite_wrapper,
        notify_telegram_fn=notify_telegram,
        state=state,
        reply_key_fn=lambda channel, chat_key: reply_key(chat_key),
        reply_cooldown=REPLY_COOLDOWN,
        on_match=on_match,
    )


def handle_agent_unavailable(data: dict[str, Any]) -> None:
    client_id = required_str(data, "client_id")
    chat_id = required_str(data, "chat_id")
    if not client_id or not chat_id:
        log.error("AGENT_UNAVAILABLE без client_id/chat_id: %r", data)
        return
    auto_reply_once(client_id, chat_id)


def handle_omnidesk_message(data: dict[str, Any]) -> None:
    normalized = normalize_omnidesk_payload(data)
    if normalized["is_staff_message"] == "1":
        log.info("Игнорирую исходящее сообщение сотрудника OmniDesk case=%s", normalized["case_id"])
        return

    message_id = normalized["event_id"] or str(uuid.uuid4())

    def queue_omni_operation(operation: str, *, text: str = "") -> bool:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"omnidesk:{message_id}:{operation}"))
        payload = {
            "provider": "omnidesk",
            "operation": operation,
            "id": job_id,
            "client_id": normalized["client_id"],
            "chat_id": normalized["chat_id"],
            "case_id": normalized["case_id"],
        }
        if text:
            payload["content"] = text
        support_store.enqueue_outbound(job_id, payload, is_handoff=operation == "assign")
        dispatch_outbound(job_id)
        return True

    def send_text(channel: str, client_id: str | None, case_id: str, text: str) -> bool:
        if channel != "omni" or client_id is None:
            return False
        return queue_omni_operation("reply", text=text)

    def assign_operator(channel: str, client_id: str | None, case_id: str) -> bool:
        if channel != "omni" or client_id is None:
            return False
        return queue_omni_operation("assign")

    ctx = MessageContext(
        channel="omni",
        chat_id=normalized["chat_id"],
        client_id=normalized["client_id"],
        message_text=normalized["message_text"],
        sender_name=normalized["sender_name"],
        raw=data,
    )

    def match_with_footer(text: str) -> dict[str, Any] | None:
        match = find_template(text)
        if match and TEMPLATE_FOOTER:
            match = dict(match)
            match["text"] = f"{match['text'].rstrip()}\n\n{TEMPLATE_FOOTER}"
        return match

    handle_out_of_hours(
        ctx,
        match_fn=match_with_footer,
        default_reply_fn=default_reply,
        send_text_fn=send_text,
        invite_agent_fn=assign_operator,
    )


def handle_chat_closed(data: dict[str, Any]) -> None:
    chat_id = required_str(data, "chat_id")
    if not chat_id:
        return
    state.delete(reply_key(chat_id), telegram_key(chat_id))


def submit_background(fn, *args) -> None:
    try:
        executor.submit(fn, *args)
    except RuntimeError as exc:
        log.error("Не удалось поставить задачу в background pool: %s", exc)


def _process_claimed_event(event: dict[str, Any]) -> None:
    event_id = event["event_id"]
    data = event["payload"]
    try:
        if data.get("_provider") == "omnidesk":
            handle_omnidesk_message(data)
            support_store.finish_event(event_id)
            return

        event_type = required_str(data, "event")
        if event_type == "CLIENT_MESSAGE":
            handle_client_message(data)
        elif event_type == "AGENT_UNAVAILABLE":
            handle_agent_unavailable(data)
        elif event_type == "CHAT_CLOSED":
            handle_chat_closed(data)
        else:
            log.info("Необрабатываемое событие: %s", event_type)
        support_store.finish_event(event_id)
    except Exception as exc:
        support_store.retry_event(event_id, str(exc))
        log.exception("Ошибка обработки события Jivo %s; событие оставлено в очереди", event_id)


def process_event(event_id: str) -> None:
    event = support_store.claim_event(event_id)
    if event is not None:
        _process_claimed_event(event)


def recovery_worker() -> None:
    while True:
        did_work = False
        try:
            event = support_store.claim_event()
            if event is not None:
                did_work = True
                _process_claimed_event(event)
            if dispatch_outbound():
                did_work = True
        except Exception:
            log.exception("Ошибка фонового восстановления очереди поддержки")
        if not did_work:
            time.sleep(1)


# -----------------------------------------------------------------------------
# Webhook / health
# -----------------------------------------------------------------------------
@app.post("/<token>")
def webhook(token: str):
    if not hmac.compare_digest(token, BOT_TOKEN):
        return jsonify(error={"code": "invalid_client", "message": "invalid token"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error={"code": "invalid_request", "message": "JSON object expected"}), 400

    event = required_str(data, "event")
    if not event:
        return jsonify(error={"code": "invalid_request", "message": "event is required"}), 400

    if event in {"CLIENT_MESSAGE", "AGENT_UNAVAILABLE"}:
        if not required_str(data, "client_id") or not required_str(data, "chat_id"):
            return (
                jsonify(
                    error={
                        "code": "invalid_request",
                        "message": "client_id and chat_id are required",
                    }
                ),
                400,
            )

    event_id = required_str(data, "id") or str(uuid.uuid4())
    try:
        accepted = support_store.enqueue_event(event_id, data, EVENT_TTL)
    except Exception:
        log.exception("Не удалось сохранить входящее событие Jivo")
        return jsonify(error={"code": "temporarily_unavailable"}), 503
    if accepted:
        submit_background(process_event, event_id)

    return jsonify({}), 200


@app.post("/omni/<token>")
def omnidesk_webhook(token: str):
    if not OMNI_WEBHOOK_TOKEN or not hmac.compare_digest(token, OMNI_WEBHOOK_TOKEN):
        return jsonify(error={"code": "invalid_client"}), 401
    if not omnidesk.configured or not omnidesk.handoff_configured:
        return jsonify(error={"code": "omnidesk_not_configured"}), 503

    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return jsonify(error={"code": "invalid_request", "message": "JSON object expected"}), 400
    try:
        normalized = normalize_omnidesk_payload(raw)
    except ValueError as exc:
        return jsonify(error={"code": "invalid_request", "message": str(exc)}), 400
    if normalized["is_staff_message"] == "1":
        return jsonify({"ignored": "staff_message"}), 200

    event_id = normalized["event_id"] or str(uuid.uuid4())
    queued_payload = dict(raw)
    queued_payload["_provider"] = "omnidesk"
    try:
        accepted = support_store.enqueue_event(f"omni:{event_id}", queued_payload, EVENT_TTL)
    except Exception:
        log.exception("Не удалось сохранить входящее событие OmniDesk")
        return jsonify(error={"code": "temporarily_unavailable"}), 503
    if accepted:
        submit_background(process_event, f"omni:{event_id}")
    return jsonify({}), 200


@app.get("/health")
def health():
    load_templates()
    with _tpl_lock:
        template_count = len(_tpl_state["items"])

    return jsonify(
        ok=True,
        working_time=is_working_time(),
        timezone=str(TZ),
        state_backend=state.name,
        templates=template_count,
        background_workers=BACKGROUND_WORKERS,
    )


@app.get("/ready")
def ready():
    """Readiness-проверка: конфиг загружен, шаблоны доступны либо не обязательны."""
    try:
        load_templates()
        return jsonify(ok=True), 200
    except Exception as exc:  # защитный барьер для orchestrator health checks
        log.exception("Readiness failed: %s", exc)
        return jsonify(ok=False), 503


@app.get("/metrics")
def metrics():
    return jsonify(get_match_metrics())


recovery_thread = threading.Thread(
    target=recovery_worker,
    name="support-recovery",
    daemon=True,
)
recovery_thread.start()


if __name__ == "__main__":
    # Для production лучше запускать через gunicorn/uwsgi.
    # Если используется MemoryState, держите 1 worker-процесс.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)
