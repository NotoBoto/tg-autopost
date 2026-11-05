"""
Исчерпанные лимиты нейросетей: распознать ошибку лимита и понять, когда он сбросится.

Время сброса берём, где сервис его сообщает:
  - заголовки ответа API: retry-after, x-ratelimit-reset-*, anthropic-ratelimit-*-reset;
  - текст ошибки: «Please retry in 34s» (Gemini), «try again in 2 days 3 hours» / «try again at 8:05 PM»
    (Codex), «limit reached|1730000000» и «resets 3pm (Europe/Moscow)» (Claude Code),
    «reset at 2026-11-05 15:41:42» (Z.ai).
Не сообщил — повторяем с нарастающей паузой (BACKOFF_MIN). Каждый следующий отказ подряд удлиняет
паузу, даже если сервис обещает сброс раньше: так не долбим сервис, у которого лимит на деле нулевой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

BACKOFF_MIN = (15, 30, 60)     # пауза без известного времени сброса: 1-й, 2-й, 3-й и дальше отказ подряд
MIN_GAP_MIN = (0, 5, 15, 60)   # не чаще этого, даже если сервис назвал время раньше
REFILL_MIN = 60                # нет денег на балансе — проверяем раз в час (вдруг пополнили)
MARGIN = timedelta(seconds=60)  # запас после объявленного сброса
MAX_WAIT = timedelta(days=8)    # дальше — явно ошибка разбора

LIMIT_MARKERS = (
    "usage limit", "rate limit", "rate_limit", "ratelimit", "quota", "resource_exhausted", "resource exhausted",
    "too many requests", "limit reached", "hit your limit", "limit exceeded", "overloaded", "credit balance",
    "insufficient_quota", "исчерпан лимит", "лимит подписки", "лимит или нет средств", "слишком много запросов",
    "перегружен", "(429)", " 429", "status 429",
)
REFILL_MARKERS = ("insufficient_quota", "exceeded your current quota", "credit balance", "insufficient balance",
                  "billing")

MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
UNITS = {"d": 86400, "day": 86400, "days": 86400, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600,
         "hours": 3600, "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "s": 1, "sec": 1,
         "secs": 1, "second": 1, "seconds": 1}


class LimitHit(RuntimeError):
    """Ошибка лимита с исходным ответом сервиса (raw) — в нём бывает время сброса."""

    def __init__(self, text: str, raw: str = ""):
        super().__init__(text)
        self.raw = raw


@dataclass
class Limit:
    reason: str            # что сказал сервис (для журнала и черновика)
    retry_at: datetime     # когда пробовать снова (с часовым поясом)
    known: bool            # время сброса сообщил сам сервис
    refill: bool = False   # похоже, кончились деньги на балансе — сам не сбросится


def _chain(e: BaseException):
    seen = 0
    while e is not None and seen < 6:
        yield e
        e, seen = e.__cause__ or e.__context__, seen + 1


def _texts(e: BaseException) -> str:
    parts = []
    for x in _chain(e):
        parts.append(str(x))
        if getattr(x, "raw", ""):
            parts.append(x.raw)
        body = getattr(x, "body", None)
        if body:
            parts.append(str(body))
    return "\n".join(parts)


def _headers(e: BaseException):
    for x in _chain(e):
        resp = getattr(x, "response", None)
        h = getattr(resp, "headers", None)
        if h:
            yield h


def _duration(text: str) -> float | None:
    """«6m0s», «1.5s», «20ms», «2 days 3 hours 5 minutes», «3h 20m» → секунды."""
    t = text.strip().lower()
    m = re.fullmatch(r"([\d.]+)ms", t)
    if m:
        return float(m.group(1)) / 1000
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", t):
        try:
            total += float(num) * UNITS[unit]
            found = True
        except ValueError:
            pass
    return total if found else None


def _zone(text: str, now: datetime):
    m = re.search(r"\(([A-Za-z_]+/[A-Za-z_/]+)\)", text)
    if m:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(m.group(1))
        except Exception:
            pass
    return now.tzinfo


def _clock(h: int, mi: int, ampm: str | None) -> tuple[int, int] | None:
    if ampm:
        ampm = ampm.lower()
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ampm == "pm" else 0)
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def _absolute(text: str, now: datetime) -> datetime | None:
    """Время сброса, записанное как момент: ISO, «Nov 7th, 2026 3:05 PM», «3pm», «15:05»."""
    tz = _zone(text, now)
    local_now = now.astimezone(tz)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups()[:5])
        try:
            return datetime(y, mo, d, h, mi, int(m.group(6) or 0), tzinfo=tz)
        except ValueError:
            return None
    m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?,?\s*(?:at\s*)?"
                  r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", text, re.I)
    if m and m.group(1).lower() in MONTHS:
        hm = _clock(int(m.group(4)), int(m.group(5) or 0), m.group(6))
        if hm:
            y = int(m.group(3) or local_now.year)
            try:
                at = datetime(y, MONTHS[m.group(1).lower()], int(m.group(2)), *hm, tzinfo=tz)
            except ValueError:
                return None
            if not m.group(3) and at < local_now - timedelta(days=1):
                at = at.replace(year=y + 1)
            return at
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", text, re.I) or \
        re.search(r"\b(\d{1,2}):(\d{2})()\b", text)
    if m:
        hm = _clock(int(m.group(1)), int(m.group(2) or 0), m.group(3) or None)
        if hm:
            at = local_now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            return at if at > local_now else at + timedelta(days=1)
    return None


def reset_time(text: str, now: datetime) -> datetime | None:
    """Когда сбросится лимит, по тексту ошибки; None — не сказано."""
    m = re.search(r"limit reached\|(\d{10})", text, re.I)          # Claude Code: «…|1730000000»
    if m:
        return datetime.fromtimestamp(int(m.group(1)), timezone.utc)
    m = re.search(r"retry(?:delay)?['\"]?\s*(?:in|[:=])\s*['\"]?([\d.]+)\s*s\b", text, re.I)   # Gemini
    if m:
        return now + timedelta(seconds=float(m.group(1)))
    m = re.search(r"(?:try again|retry|resets?|available again|reset)\s+(?:in|after)\s+([^.;\n]{1,60})", text, re.I)
    if m:
        sec = _duration(m.group(1))
        if sec:
            return now + timedelta(seconds=sec)
    m = re.search(r"(?:try again|resets?|reset|available again|renews?)\s*(?:at|on|after)?\s*:?\s*([^;\n]{1,80})",
                  text, re.I)
    if m:
        return _absolute(m.group(1), now)
    return None


def _header_time(e: BaseException, now: datetime) -> datetime | None:
    for h in _headers(e):
        try:
            v = h.get("retry-after")
            if v:
                try:
                    return now + timedelta(seconds=float(v))
                except ValueError:
                    return parsedate_to_datetime(v)
            best = None
            for k in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
                sec = _duration(h.get(k) or "")
                if sec is not None:
                    best = max(best or 0, sec)
            if best is not None:
                return now + timedelta(seconds=best)
            for k in ("anthropic-ratelimit-requests-reset", "anthropic-ratelimit-tokens-reset"):
                v = h.get(k)
                if v:
                    return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def is_limit(e: BaseException) -> bool:
    if any(isinstance(x, LimitHit) for x in _chain(e)):
        return True
    low = _texts(e).lower()
    return any(m in low for m in LIMIT_MARKERS)


def classify(e: BaseException, streak: int = 0, now: datetime | None = None) -> Limit | None:
    """Ошибка лимита → когда пробовать снова. streak — сколько отказов по лимиту было подряд до этого."""
    if not is_limit(e):
        return None
    now = now or datetime.now().astimezone()
    text = _texts(e)
    at = _header_time(e, now) or reset_time(text, now)
    if at and not (now < at < now + MAX_WAIT):
        at = None
    known = at is not None
    refill = not known and any(m in text.lower() for m in REFILL_MARKERS)
    if known:
        at = at + MARGIN
    else:
        minutes = REFILL_MIN if refill else BACKOFF_MIN[min(streak, len(BACKOFF_MIN) - 1)]
        at = now + timedelta(minutes=minutes)
    floor = now + timedelta(minutes=MIN_GAP_MIN[min(streak, len(MIN_GAP_MIN) - 1)])
    return Limit(str(e), max(at, floor).astimezone(now.tzinfo), known, refill)
