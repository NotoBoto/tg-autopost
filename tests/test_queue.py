"""Очередь генерации: пауза при исчерпанном лимите, автоповтор, повтор после перезапуска, журнал."""
import asyncio
import logging
from datetime import datetime, timedelta

import pytest
from PIL import Image

import core
import drafts
import imagegen
from drafts import CANCELLED, DraftStore
from engine import Engine
from limits import LimitHit


class FakeText:
    name = "ChatGPT (Codex)"

    def __init__(self):
        self.calls, self.limited = 0, False

    async def generate(self, system, user):
        self.calls += 1
        if self.limited:
            raise LimitHit("Исчерпан лимит подписки ChatGPT для Codex", raw="try again in 3 hours")
        return f"Пост номер {self.calls}"


class FakeImage:
    name, backend, can_edit, generates = "OpenAI API", "openai_api", True, True

    def __init__(self, tmp):
        self.calls, self.limited, self.tmp = 0, False, tmp

    async def generate(self, prompt, out_dir, base=None):
        self.calls += 1
        if self.limited:
            raise RuntimeError("OpenAI: лимит или нет средств на балансе — Rate limit reached, retry after 10s")
        src = self.tmp / "src.png"
        Image.new("RGB", (8, 8)).save(src)
        return imagegen.save_image(src.read_bytes(), out_dir)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(drafts, "DRAFTS_DIR", tmp_path / "drafts")
    store = DraftStore(tmp_path / "drafts.json")
    cfg = core.normalize_profile({"id": "p1", "channels": ["mychan"], "post_format": "plain"})
    e = Engine(cfg, store)
    e.text_gen, e.image_gen = FakeText(), FakeImage(tmp_path)
    yield e, store
    stop(e)


def stop(e):
    def go():
        for t in e._wake.values():
            if t:
                t.cancel()
        e.loop.call_soon(e.loop.stop)
    e.loop.call_soon_threadsafe(go)


def run(e, coro):
    return asyncio.run_coroutine_threadsafe(coro, e.loop).result(5)


def wait_all(futs):
    for f in futs:
        f.result(5)


def test_limit_pauses_queue_and_resume_continues(env):
    e, store = env
    e.text_gen.limited = True
    ds = [store.new("p1", f"Тема {i}", "mychan") for i in range(4)]
    wait_all([e.generate(d.id) for d in ds])
    # Нейросеть спросили один раз: остальные темы встали в ожидание, не дёргая её
    assert e.text_gen.calls == 1
    assert all(d.waiting and d.retry_op == "draft" and not d.busy for d in ds)
    lim = e.pause["text"]
    assert lim and lim.known and timedelta(hours=2, minutes=59) < lim.retry_at - datetime.now().astimezone()
    assert e._wake["text"] and not e._wake["text"].done()
    assert "Продолжу сама" in ds[0].error
    # Время сброса пришло — очередь продолжилась сама: текст и картинка у всех
    e.text_gen.limited = False
    run(e, e._resume("text"))
    assert all(d.text.startswith("Пост номер") and d.image_path and not d.retry_op and not d.error for d in ds)


def test_image_limit_keeps_text_and_waits_for_image(env):
    e, store = env
    e.image_gen.limited = True
    a, b = store.new("p1", "А", "mychan"), store.new("p1", "Б", "mychan")
    wait_all([e.generate(a.id), e.generate(b.id)])
    assert a.text and b.text and e.image_gen.calls == 1
    assert a.retry_op == b.retry_op == "image" and a.waiting and b.waiting
    assert e.pause["text"] is None and e.pause["image"]
    e.image_gen.limited = False
    run(e, e._resume("image"))
    assert a.image_path and b.image_path and not a.waiting and not b.waiting


def test_user_action_tries_despite_pause(env):
    e, store = env
    e.text_gen.limited = True
    d = store.new("p1", "Тема", "mychan")
    e.generate(d.id).result(5)
    e.text_gen.limited = False
    e.regen_text(d.id, "короче").result(5)   # человек нажал сам — пробуем сразу
    assert d.text and not d.waiting and e.text_gen.calls == 2


def test_retry_all_runs_failed_and_waiting(env):
    e, store = env
    e.text_gen.limited = True
    w = store.new("p1", "Ждёт", "mychan")
    e.generate(w.id).result(5)
    f = store.new("p1", "Сломался", "mychan")
    f.text, f.retry_op, f.retry_wish, f.error = "старый", "text", "веселее", "Ошибка: что-то"
    e.text_gen.limited = False
    e.retry_all().result(5)
    assert w.text and w.image_path and not w.retry_op
    assert f.text.startswith("Пост номер") and not f.error and e.pause["text"] is None


def test_cancel_clears_waiting(env):
    e, store = env
    e.text_gen.limited = True
    d = store.new("p1", "Тема", "mychan")
    e.generate(d.id).result(5)
    e.cancel(d.id)
    assert d.status == CANCELLED and not d.retry_op
    e.text_gen.limited = False
    run(e, e._resume("text"))
    assert not d.text   # отменённый черновик очередь не трогает


def test_waiting_survives_restart(env, tmp_path):
    e, store = env
    e.text_gen.limited = True
    d = store.new("p1", "Тема", "mychan")
    e.generate(d.id).result(5)
    again = DraftStore(tmp_path / "drafts.json")
    assert again.get(d.id).waiting and again.get(d.id).retry_op == "draft"
    e2 = Engine(e.cfg, again)
    e2.text_gen = FakeText()
    run(e2, asyncio.sleep(0.05))   # _resume_saved отрабатывает при создании движка
    assert e2.pause["text"] and e2._wake["text"]
    assert abs((e2.pause["text"].retry_at - again.get(d.id).wait_time()).total_seconds()) < 2
    stop(e2)


def test_switching_backend_resumes(env):
    e, store = env
    e.text_gen.limited = True
    a, b = store.new("p1", "А", "mychan"), store.new("p1", "Б", "mychan")
    e.generate(a.id).result(5)
    other = FakeText()
    other.name = "Claude Code"
    e.text_gen = other   # сменили нейросеть в «Настройках»
    e.generate(b.id).result(5)
    run(e, asyncio.sleep(0.1))
    assert b.text and a.text and not a.waiting and e.pause["text"] is None


def test_log_is_detailed(env, caplog):
    e, store = env
    d = store.new("p1", "Каким образом извлекается звук из скрипки", "mychan")
    with caplog.at_level(logging.INFO, logger="autopost"):
        e.generate(d.id).result(5)
        e.regen_image(d.id, "больше света").result(5)
        e.regen_text(d.id, "короче", edit=True).result(5)
        e.cancel(d.id)
        e.restore(d.id)
        e.text_gen.limited = True
        e.regen_text(d.id).result(5)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "«Каким образом извлекается звук из скрипки»: пишу текст — ChatGPT (Codex)" in text
    assert "текст готов — ChatGPT (Codex), " in text and "знаков" in text
    assert "рисую картинку заново по пожеланию «больше света» — OpenAI API" in text
    assert "картинка готова — OpenAI API" in text and "вариант 2" in text
    assert "правлю текст по пожеланию «короче»" in text
    assert "отменён" in text and "возвращён на проверку" in text
    assert "лимит исчерпан" in text and "Генерация текста на паузе до" in text
