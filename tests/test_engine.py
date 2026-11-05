import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

import core
import drafts
from drafts import PUBLISHED, REVIEW, SCHEDULED, DraftStore
from engine import Engine


class FakeClient:
    def __init__(self):
        self.sent, self.scheduled, self.next_id = [], [], 100

    def _msg(self, text, media, schedule):
        self.next_id += 1
        m = SimpleNamespace(id=self.next_id, date=schedule or datetime.now(timezone.utc), raw_text=text,
                            media=media, grouped_id=None)
        self.sent.append(m)
        if schedule:
            self.scheduled.append(m)
        return m

    async def send_file(self, ent, file, caption="", parse_mode=None, schedule=None):
        return self._msg(caption, True, schedule)

    async def send_message(self, ent, text, parse_mode=None, link_preview=True, schedule=None):
        return self._msg(text, None, schedule)

    async def iter_messages(self, ent, scheduled=False):
        for m in list(self.scheduled):
            yield m

    async def get_messages(self, ent, limit=20):
        return [m for m in self.sent if m not in self.scheduled][-limit:]

    async def disconnect(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(drafts, "DRAFTS_DIR", tmp_path / "drafts")
    store = DraftStore(tmp_path / "drafts.json")
    cfg = core.normalize_profile({"id": "p1", "channels": ["mychan"], "post_format": "plain"})
    e = Engine(cfg, store)
    e.client, e.running = FakeClient(), True
    ent = SimpleNamespace(username="mychan", id=1)
    e.targets["mychan"] = ent
    monkeypatch.setattr("engine.utils.get_peer_id", lambda x: -1001)
    yield e, store, tmp_path
    e.loop.call_soon_threadsafe(e.loop.stop)


def run(e, coro):
    return asyncio.run_coroutine_threadsafe(coro, e.loop).result(5)


def make_img(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10)).save(path)
    return str(path)


def test_publish_now_with_caption(env):
    e, store, tmp = env
    d = store.new("p1", "Тема", "mychan")
    d.text, d.image = "Короткий пост", make_img(tmp / "a.png")
    run(e, e._publish(d, None))
    assert d.status == PUBLISHED and len(d.msg_ids) == 1 and d.link == f"https://t.me/mychan/{d.msg_ids[0]}"


def test_long_text_split_and_schedule(env):
    e, store, tmp = env
    d = store.new("p1", "Тема", "mychan")
    d.text, d.image = "x" * 1500, make_img(tmp / "b.png")
    when = datetime.now(timezone.utc) + timedelta(hours=2)
    run(e, e._publish(d, when))
    assert d.status == SCHEDULED and len(d.msg_ids) == 2
    run(e, e._refresh_scheduled())
    items = e.scheduled["mychan"]
    assert len(items) == 1 and items[0]["draft"] == d.id and items[0]["ids"] == d.msg_ids


def test_long_text_shorten_mode_blocks(env):
    e, store, tmp = env
    e.cfg["long_text"] = "shorten"
    d = store.new("p1", "Тема", "mychan")
    d.text, d.image = "x" * 1500, make_img(tmp / "c.png")
    assert "не помещается" in e.publish_problem(d)


def test_refresh_marks_published_and_removed(env):
    e, store, tmp = env
    past = store.new("p1", "Вышел", "mychan")
    past.text = "Вышедший пост"
    run(e, e._publish(past, datetime.now(timezone.utc) + timedelta(hours=1)))
    gone = store.new("p1", "Снят", "mychan")
    gone.text = "Снятый пост"
    run(e, e._publish(gone, datetime.now(timezone.utc) + timedelta(hours=3)))
    # Telegram опубликовал первый (он исчез из отложенных, время пришло) и кто-то снял второй вручную
    m = next(m for m in e.client.scheduled if m.id == past.msg_ids[0])
    e.client.scheduled.clear()
    m.date = datetime.now(timezone.utc)
    past.publish_at = m.date.isoformat()
    run(e, e._refresh_scheduled())
    assert past.status == PUBLISHED and past.link.startswith("https://t.me/mychan/")
    assert gone.status == REVIEW and gone.msg_ids == [] and "убрали" in gone.error


def test_store_roundtrip(env):
    e, store, tmp = env
    d = store.new("p1", "Тема", "mychan")
    d.text, d.text_busy = "Текст", True
    store.save()
    again = DraftStore(store.path)
    assert again.get(d.id).text == "Текст" and again.get(d.id).text_busy is False


def test_generation_pipeline(env, monkeypatch):
    e, store, tmp = env
    e.cfg["post_max_len"] = 100
    calls = []

    async def fake_text(system, user):
        calls.append(system + " | " + user)
        return "**Заголовок**\n" + ("слово " * 40 if len(calls) == 1 else "коротко")

    async def fake_image(prompt, out_dir, base=None):
        calls.append(prompt)
        return __import__("imagegen").save_image(open(make_img(tmp / "src.png"), "rb").read(), out_dir)

    monkeypatch.setattr(e.text_gen, "generate", fake_text)
    monkeypatch.setattr(e.image_gen, "generate", fake_image)
    e.cfg["image_backend"] = "openai_api"
    d = store.new("p1", "Кофе", "mychan")
    run(e, e._gen_text(d, "draft", "", False, False))
    assert d.text == "Заголовок\nкоротко" and "сократи до 100" in calls[1]
    run(e, e._gen_image(d, "image", "ярче", False, False))
    assert d.image_path and d.images == [d.image] and "ярче" in calls[2] and "Кофе" in calls[2]
    run(e, e._gen_image(d, "image", "темнее", True, False))
    assert len(d.images) == 2 and "темнее" in calls[3]


class LoginClient:
    """Клиент на этапе облачного пароля: sign_in бросает заданную ошибку или пускает."""
    def __init__(self, error=None):
        self.error, self.flood_sleep_threshold, self.seen_threshold = error, 60, None

    async def sign_in(self, password=None):
        self.seen_threshold = self.flood_sleep_threshold
        if self.error:
            raise self.error

    async def get_me(self):
        return SimpleNamespace(first_name="Аня", premium=False)

    async def disconnect(self):
        pass


def login_events(e, error):
    from telethon import errors
    e.targets.clear()
    e.client = LoginClient(error)
    e.cfg["channels"] = []
    e.sign_in_password("секрет").result(5)
    got = []
    while not e.events.empty():
        got.append(e.events.get())
    return e.client, got, errors


def test_password_wrong_is_reported(env):
    from telethon import errors
    e, _, _ = env
    client, got, _ = login_events(e, errors.PasswordHashInvalidError(request=None))
    texts = [d for k, d in got if k == "login_error"]
    assert texts and "Неверный облачный пароль" in texts[0]
    assert client.seen_threshold == 0 and client.flood_sleep_threshold == 60   # не ждём молча, порог вернули


def test_password_flood_wait_is_reported(env):
    from telethon import errors
    e, _, _ = env
    _, got, _ = login_events(e, errors.FloodWaitError(request=None, capture=125))
    texts = [d for k, d in got if k == "login_error"]
    assert texts and "подождать 3 мин" in texts[0]


def test_password_ok_logs_in(env):
    e, _, _ = env
    e.running = False
    _, got, _ = login_events(e, None)
    assert ("me", "Аня") in got and ("status", "running") in got
    e.loop.call_soon_threadsafe(e._refresh_task.cancel)
