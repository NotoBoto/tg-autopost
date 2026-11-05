"""
Расписание публикаций: когда ближайшее свободное время для поста.

Два режима (настройка профиля "schedule"):
  slots    — свои времена для каждого дня недели: {"0": ["10:00", "19:00"], …, "6": ["12:00"]} (0 — понедельник);
  interval — каждые interval_hours часов с window_from до window_to по дням days.
Свободный слот — тот, рядом с которым (ближе min_gap_min минут) нет других постов канала: ни запланированных
программой, ни поставленных в отложку вручную в Telegram. К выбранному слоту добавляется случайный сдвиг
± jitter_min минут — посты не выходят секунда в секунду.
"""
import random
import re
from datetime import date, datetime, time, timedelta

TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
MIN_LEAD = timedelta(minutes=3)       # ближе к «сейчас» не планируем: Telegram может не успеть
MAX_AHEAD_DAYS = 360                  # Telegram принимает отложенные посты не дальше чем на год


def parse_time(s: str) -> time | None:
    m = TIME_RE.match(s)
    if not m:
        return None
    h, mi = int(m[1]), int(m[2])
    return time(h, mi) if h < 24 and mi < 60 else None


def parse_times(s: str) -> tuple[list[str], list[str]]:
    """«10:00, 19:30 8.15» → (["08:15", "10:00", "19:30"], ошибки)."""
    good, bad = set(), []
    for part in re.split(r"[,;\s]+", s.strip()):
        if not part:
            continue
        t = parse_time(part)
        if t:
            good.add(f"{t.hour:02}:{t.minute:02}")
        else:
            bad.append(part)
    return sorted(good), bad


def _local(d: date, t: time) -> datetime:
    return datetime.combine(d, t).astimezone()   # местное время с часовым поясом


def day_times(sched: dict, d: date) -> list[time]:
    """Времена публикаций в этот день по расписанию."""
    if sched.get("mode") == "interval":
        if d.weekday() not in sched.get("days", []):
            return []
        start = parse_time(sched.get("window_from", "09:00")) or time(9)
        end = parse_time(sched.get("window_to", "22:00")) or time(22)
        step = max(0.25, float(sched.get("interval_hours", 4) or 4))
        out, cur = [], datetime.combine(d, start)
        stop = datetime.combine(d, end)
        while cur <= stop and cur.date() == d:
            out.append(cur.time())
            cur += timedelta(hours=step)
        return out
    raw = (sched.get("slots") or {}).get(str(d.weekday()), [])
    return sorted(t for t in (parse_time(x) for x in raw) if t)


def slots(sched: dict, after: datetime, days: int = MAX_AHEAD_DAYS):
    """Все слоты расписания позже after — по порядку."""
    for i in range(days + 1):
        d = after.date() + timedelta(days=i)
        for t in day_times(sched, d):
            at = _local(d, t)
            if at > after:
                yield at


def is_free(at: datetime, taken: list[datetime], gap: timedelta) -> bool:
    return all(abs(at - x) >= gap for x in taken)


def next_free(sched: dict, taken: list[datetime], now: datetime | None = None, n: int = 1,
              jitter: bool = True) -> list[datetime]:
    """n ближайших свободных времён (с учётом уже занятых taken и друг друга)."""
    now = (now or datetime.now().astimezone())
    gap = timedelta(minutes=max(0, int(sched.get("min_gap_min", 0) or 0)))
    jit = int(sched.get("jitter_min", 0) or 0) if jitter else 0
    taken = list(taken)
    found = []
    for at in slots(sched, now + MIN_LEAD):
        if not is_free(at, taken, gap):
            continue
        if jit:
            shifted = at + timedelta(minutes=random.randint(-jit, jit))
            if shifted >= now + MIN_LEAD and is_free(shifted, taken, gap):
                at = shifted
        found.append(at)
        taken.append(at)
        if len(found) >= n:
            break
    return found


def has_slots(sched: dict) -> bool:
    return any(day_times(sched, date.today() + timedelta(days=i)) for i in range(7))


def fmt(at: datetime) -> str:
    """«сегодня 19:00», «завтра 10:00», «пт 07.11 12:00»."""
    today = datetime.now().astimezone().date()
    d = at.astimezone().date()
    hm = at.astimezone().strftime("%H:%M")
    if d == today:
        return f"сегодня {hm}"
    if d == today + timedelta(days=1):
        return f"завтра {hm}"
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][d.weekday()]
    return f"{wd} {d:%d.%m} {hm}" if d.year == today.year else f"{d:%d.%m.%Y} {hm}"


def parse_when(s: str, now: datetime | None = None) -> datetime | None:
    """Своё время публикации: «19:30» (сегодня, а если прошло — завтра), «07.11 19:30», «07.11.2026 19:30»,
    «завтра 10:00». None — не разобрать."""
    now = now or datetime.now().astimezone()
    s = s.strip().lower()
    m = re.fullmatch(r"(?:(сегодня|завтра)\s+)?(\d{1,2})[:.](\d{2})", s)
    if m:
        t = parse_time(f"{m[2]}:{m[3]}")
        if not t:
            return None
        d = now.date() + timedelta(days=1 if m[1] == "завтра" else 0)
        at = _local(d, t)
        if at <= now and not m[1]:
            at = _local(d + timedelta(days=1), t)
        return at
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(\d{1,2})[:.](\d{2})", s)
    if m:
        t = parse_time(f"{m[4]}:{m[5]}")
        year = int(m[3]) if m[3] else now.year
        if year < 100:
            year += 2000
        try:
            at = _local(date(year, int(m[2]), int(m[1])), t) if t else None
        except ValueError:
            return None
        if at and not m[3] and at <= now:   # без года и уже прошло — значит, в следующем году
            at = at.replace(year=year + 1)
        return at
    return None
