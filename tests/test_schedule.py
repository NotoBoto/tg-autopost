import random
from datetime import datetime, timedelta

import schedule as sc

TZ = datetime.now().astimezone().tzinfo
MON = datetime(2026, 11, 2, 8, 0, tzinfo=TZ)   # понедельник 08:00


def sched(**kw):
    s = {"mode": "slots", "slots": {str(d): ["10:00", "19:00"] for d in range(5)} | {"5": ["12:00"], "6": []},
         "jitter_min": 0, "min_gap_min": 60}
    s.update(kw)
    return s


def test_next_free_basic():
    got = sc.next_free(sched(), [], now=MON, n=3)
    assert [g.strftime("%a %H:%M") for g in got] == ["Mon 10:00", "Mon 19:00", "Tue 10:00"]


def test_taken_skipped_with_gap():
    taken = [MON.replace(hour=10, minute=30)]   # ручная отложка рядом со слотом 10:00
    got = sc.next_free(sched(), taken, now=MON, n=1)
    assert got[0].hour == 19


def test_weekend_and_empty_day():
    sat = datetime(2026, 11, 7, 13, 0, tzinfo=TZ)
    got = sc.next_free(sched(), [], now=sat, n=1)
    assert got[0].strftime("%a %H:%M") == "Mon 10:00"   # в воскресенье слотов нет


def test_interval_mode():
    s = sched(mode="interval", interval_hours=4, window_from="09:00", window_to="22:00", days=[0])
    assert [t.strftime("%H:%M") for t in sc.day_times(s, MON.date())] == ["09:00", "13:00", "17:00", "21:00"]
    assert sc.day_times(s, MON.date() + timedelta(days=1)) == []


def test_jitter_stays_in_range():
    random.seed(1)
    s = sched(jitter_min=10, min_gap_min=0)
    for g in sc.next_free(s, [], now=MON, n=5):
        base = g.replace(minute=0) if g.minute < 30 else (g + timedelta(hours=1)).replace(minute=0)
        assert abs((g - base).total_seconds()) <= 600


def test_min_lead():
    now = MON.replace(hour=9, minute=59)
    assert sc.next_free(sched(), [], now=now, n=1)[0].hour == 19


def test_parse_times():
    assert sc.parse_times("19:30, 8.15 10:00") == (["08:15", "10:00", "19:30"], [])
    assert sc.parse_times("25:00 abc")[1] == ["25:00", "abc"]


def test_parse_when():
    now = MON
    assert sc.parse_when("19:30", now) == MON.replace(hour=19, minute=30)
    assert sc.parse_when("07:00", now).day == 3            # прошло — завтра
    assert sc.parse_when("завтра 10:00", now).day == 3
    assert sc.parse_when("05.12 18:00", now).month == 12
    assert sc.parse_when("01.01 10:00", now).year == 2027
    assert sc.parse_when("ерунда", now) is None
