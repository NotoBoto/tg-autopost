from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from limits import MARGIN, LimitHit, classify, is_limit, reset_time

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone(timedelta(hours=3)))


def near(a, b, sec=2):
    return abs((a - b).total_seconds()) <= sec


@pytest.mark.parametrize("text, expect", [
    # Codex
    ("You've hit your usage limit. Upgrade to Pro or try again in 2 days 3 hours 5 minutes.",
     NOW + timedelta(days=2, hours=3, minutes=5)),
    ("You've hit your usage limit. Try again at 8:05 PM.", NOW.replace(hour=20, minute=5)),
    ("You've hit your usage limit. Try again at 9:30 AM.", NOW.replace(hour=9, minute=30) + timedelta(days=1)),
    ("You've hit your usage limit, try again at Nov 7th, 2026 3:05 PM.", NOW.replace(day=7, hour=15, minute=5)),
    # Gemini
    ("429 RESOURCE_EXHAUSTED. You exceeded your current quota. Please retry in 34.5s.",
     NOW + timedelta(seconds=34.5)),
    ("{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '41s'}", NOW + timedelta(seconds=41)),
    # Z.ai
    ("Usage limit reached for 5 hour. Your limit will reset at 2026-11-05 15:41:42",
     NOW.replace(hour=15, minute=41, second=42)),
    # Claude Code
    ("You've hit your limit · resets 3pm", NOW.replace(hour=15)),
    ("5-hour limit reached ∙ resets 1:30am", NOW.replace(hour=1, minute=30) + timedelta(days=1)),
    ("Your rate limit resets in 3h 20m", NOW + timedelta(hours=3, minutes=20)),
])
def test_reset_time(text, expect):
    assert near(reset_time(text, NOW), expect)


def test_claude_epoch_and_zone():
    at = NOW + timedelta(hours=2)
    assert near(reset_time(f"Claude AI usage limit reached|{int(at.timestamp())}", NOW), at)
    got = reset_time("You've hit your limit · resets 3pm (Europe/Moscow)", NOW)
    assert got.hour == 15 and got.utcoffset() == timedelta(hours=3)


def test_no_time_backoff_grows():
    e = RuntimeError("Слишком много запросов к API — попробуйте чуть позже")
    first, second, later = (classify(e, n, NOW) for n in (0, 1, 5))
    assert not first.known and first.retry_at == NOW + timedelta(minutes=15)
    assert second.retry_at == NOW + timedelta(minutes=30)
    assert later.retry_at == NOW + timedelta(minutes=60)


def test_known_time_and_floor():
    e = LimitHit("Исчерпан лимит подписки ChatGPT для Codex", raw="try again in 2 minutes")
    lim = classify(e, 0, NOW)
    assert lim.known and lim.retry_at == NOW + timedelta(minutes=2) + MARGIN
    # Третий отказ подряд: сервис обещает через 2 минуты, но так часто не долбим
    assert classify(e, 2, NOW).retry_at == NOW + timedelta(minutes=15)


def test_headers_from_chained_sdk_error():
    sdk = Exception("Rate limit reached for gpt-image-1")
    sdk.response = SimpleNamespace(headers={"retry-after": "90"})
    try:
        try:
            raise sdk
        except Exception:
            raise RuntimeError("OpenAI: лимит или нет средств на балансе — Rate limit reached")
    except RuntimeError as e:
        lim = classify(e, 0, NOW)
    assert lim.known and lim.retry_at == NOW + timedelta(seconds=90) + MARGIN


def test_refill_waits_an_hour():
    e = RuntimeError("OpenAI: лимит или нет средств на балансе — You exceeded your current quota, "
                     "please check your plan and billing details. insufficient_quota")
    lim = classify(e, 0, NOW)
    assert lim.refill and lim.retry_at == NOW + timedelta(minutes=60)


@pytest.mark.parametrize("text", ["Ключ OpenAI не подходит — проверьте его в «Настройках»",
                                  "Codex не ответил вовремя", "Нет связи с OpenAI API — проверьте интернет",
                                  "Модель gpt-x недоступна — выберите другую в «Настройках»"])
def test_not_limits(text):
    assert not is_limit(RuntimeError(text)) and classify(RuntimeError(text)) is None


def test_past_time_ignored():
    # Сервис назвал момент в прошлом — не верим, ждём по умолчанию
    e = RuntimeError("Usage limit reached. Your limit will reset at 2026-11-05 10:00:00")
    lim = classify(e, 0, NOW)
    assert not lim.known and lim.retry_at == NOW + timedelta(minutes=15)
