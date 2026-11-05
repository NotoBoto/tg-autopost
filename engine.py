"""
Движок профиля: свой аккаунт Telegram, свой поток с asyncio-циклом, генерация черновиков и публикация.
Все публичные методы вызываются из потока окна: работа уходит в цикл фонового потока.

События для окна (self.events):
  ("status", str)        stopped / connecting / login / running
  ("login_needed", None), ("qr", url), ("password_needed", None), ("login_error", text), ("code_sent", None)
  ("me", name)
  ("draft", id)          черновик изменился (текст, картинка, статус, ошибка)
  ("scheduled", None)    обновился список отложенных постов (self.scheduled)
"""
import asyncio
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, connection, errors, utils
from telethon.tl import functions, types

import schedule as sched_mod
from core import (API_KEY_ERRORS, BASE, CAPTION_LIMIT, MESSAGE_LIMIT, PREMIUM_CAPTION_LIMIT, api_key_error,
                  build_image_prompt, build_text_request, channel_label, channels, clean_post, edit_image_prompt,
                  explain_ai_error, log, post_html, post_length, proxy_label, telegram_app, telegram_proxy,
                  visible_text)
from drafts import CANCELLED, PUBLISHED, REVIEW, SCHEDULED, Draft, DraftStore
from imagegen import ImageGen
from limits import Limit, classify, is_limit
from textgen import TextGen

REFRESH_EVERY = 120          # сек: как часто сверять отложенные посты с Telegram
TOO_LONG_RETRY = 1.15        # текст длиннее лимита больше чем на 15% — просим нейросеть сократить


class ProfileLog(logging.LoggerAdapter):
    """Записи движка помечаются именем профиля, если профилей несколько."""

    def process(self, msg, kwargs):
        e = self.extra["engine"]
        return (f"[{e.cfg['name']}] {msg}" if e.show_name else msg), kwargs



KINDS = ("text", "image")
KIND_NAME = {"text": "Генерация текста", "image": "Генерация картинок"}


def op_kind(op: str) -> str:
    """Какая нейросеть нужна операции: «draft» и «text» начинаются с текста."""
    return "image" if op == "image" else "text"


def short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def label(d: Draft) -> str:
    """Тема черновика для журнала."""
    return short(d.topic, 60)


def took(started: float) -> str:
    sec = time.monotonic() - started
    return f"{sec:.0f} с" if sec < 120 else f"{sec / 60:.1f} мин"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def wait_text(sec: int) -> str:
    """«40 с», «3 мин», «2 ч 5 мин» — сколько ждать по FloodWait."""
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        return f"{(sec + 59) // 60} мин"
    h, m = divmod((sec + 59) // 60, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


class Engine:
    def __init__(self, cfg: dict, store: DraftStore):
        self.cfg, self.store = cfg, store
        self.log = ProfileLog(log, {"engine": self})
        self.show_name = False   # окно включает, когда профилей больше одного
        self.text_gen = TextGen(cfg, self.log)
        self.image_gen = ImageGen(cfg, self.log)
        self.events: queue.Queue = queue.Queue()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.client: TelegramClient | None = None
        self._qr_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._dialogs_loaded = False
        self._phone = ""
        self.targets: dict[str, object] = {}        # запись из списка каналов → найденный канал
        self.titles: dict[str, str] = {}            # запись → как показать («@name» или название)
        self.can_post: dict[str, bool] = {}
        self.scheduled: dict[str, list[dict]] = {}  # запись → отложенные посты канала (см. _refresh_scheduled)
        self.scheduled_at: datetime | None = None   # когда список отложенных сверяли с Telegram
        self.premium = False
        self.running = False
        self.status = "stopped"
        self.me_name = ""
        # Лимиты нейросетей: пауза очереди по виду (текст / картинки) до сброса
        self.pause: dict[str, Limit | None] = {k: None for k in KINDS}
        self.pause_by: dict[str, str] = {k: "" for k in KINDS}     # чья пауза — имя нейросети
        self.streak: dict[str, int] = {k: 0 for k in KINDS}        # отказов по лимиту подряд
        self._wake: dict[str, asyncio.Task | None] = {k: None for k in KINDS}
        self._locks = {k: asyncio.Lock() for k in KINDS}          # по одному запросу каждого вида
        self._submit(self._resume_saved())

    # --- служебное ---

    def _emit(self, kind, data=None):
        if kind == "status":
            self.status = data
        elif kind == "me":
            self.me_name = data
        self.events.put((kind, data))

    def _submit(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(self._report_crash)
        return fut

    def _report_crash(self, fut):
        if not fut.cancelled() and fut.exception():
            self.log.error("Ошибка: %s", fut.exception())

    # --- запуск / остановка / вход ---

    def start(self):
        self._submit(self._start())

    def stop(self):
        self._submit(self._stop())

    def close(self):
        """Окончательно: отключиться и остановить поток движка (профиль удаляют)."""
        try:
            self._submit(self._stop()).result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    async def _start(self):
        if self.client:
            await self._stop()
        cfg = self.cfg
        self._emit("status", "connecting")
        self._dialogs_loaded = False   # новое подключение — список чатов загрузим заново при надобности
        self.targets = {}
        try:
            proxy = telegram_proxy(cfg)
        except ValueError as e:
            self._emit("status", "stopped")
            self.log.error("Прокси: %s", e)
            return
        kw = {}
        if proxy and proxy["type"] == "mtproxy":
            kw = {"connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
                  "proxy": (proxy["host"], proxy["port"], proxy["secret"])}
        elif proxy:
            kw = {"proxy": {"proxy_type": proxy["type"], "addr": proxy["host"], "port": proxy["port"],
                            "username": proxy["user"], "password": proxy["password"], "rdns": True}}
        if proxy:
            self.log.info("Подключаюсь к Telegram через прокси %s", proxy_label(proxy))
        api_id, api_hash = telegram_app(cfg)
        self.client = TelegramClient(str(BASE / cfg["session_name"]), api_id, api_hash, **kw)
        try:
            await self.client.connect()
        except Exception as e:
            self.client = None
            self._emit("status", "stopped")
            if proxy:
                self.log.error("Не удалось подключиться к Telegram через прокси %s: %s — проверьте, "
                               "что VPN включён и адрес прокси верный", proxy_label(proxy), e)
            else:
                self.log.error("Не удалось подключиться к Telegram: %s — если Telegram у вас "
                               "заблокирован, включите VPN (см. «Настройки» → «Продвинутые настройки» → «Прокси»)", e)
            return
        if await self.client.is_user_authorized():
            await self._after_login()
        else:
            self._emit("status", "login")
            self._emit("login_needed")

    async def _stop(self):
        if self._qr_task:
            self._qr_task.cancel()
            self._qr_task = None
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self.client:
            await self.client.disconnect()
            self.client = None
        if self.running:
            self.log.info("Остановлено")
        self.running = False
        self._emit("status", "stopped")

    def send_code(self, phone: str):
        async def go():
            self._phone = phone.strip()
            try:
                await self.client.send_code_request(self._phone)
                self._emit("code_sent")
            except API_KEY_ERRORS:
                self._emit("login_error", api_key_error(self.cfg))
            except Exception as e:
                self._login_error(f"Не удалось отправить код: {e}")
        self._submit(go())

    def sign_in_code(self, code: str):
        async def go():
            try:
                await self.client.sign_in(self._phone, code.strip())
            except errors.SessionPasswordNeededError:
                self._emit("password_needed")
                return
            except errors.FloodWaitError as e:
                self._login_error(f"Слишком много попыток — Telegram просит подождать {wait_text(e.seconds)}.")
                return
            except Exception as e:
                self._login_error(f"Код не подошёл: {e}")
                return
            await self._after_login()
        self._submit(go())

    def sign_in_password(self, password: str):
        async def go():
            if not self.client:
                return
            # Без молчаливого ожидания: если Telegram просит подождать, скажем об этом сразу,
            # иначе окно «зависает» на секунды, а повторные нажатия копят новые попытки
            threshold = self.client.flood_sleep_threshold
            self.client.flood_sleep_threshold = 0
            try:
                await self.client.sign_in(password=password)
            except errors.PasswordHashInvalidError:
                self._login_error("Неверный облачный пароль. Это пароль двухэтапной аутентификации "
                                  "Telegram, не код из SMS. Забыли — Telegram → Настройки → "
                                  "Конфиденциальность → Облачный пароль.")
                return
            except errors.FloodWaitError as e:
                self._login_error(f"Слишком много попыток — Telegram просит подождать {wait_text(e.seconds)} "
                                  "и попробовать снова.")
                return
            except Exception as e:
                self._login_error(f"Пароль не подошёл: {e}")
                return
            finally:
                if self.client:
                    self.client.flood_sleep_threshold = threshold
            await self._after_login()
        return self._submit(go())

    def _login_error(self, text: str):
        self.log.warning("Вход в Telegram: %s", text)
        self._emit("login_error", text)

    def start_qr(self):
        async def go():
            if not self.client:   # окно входа открыто, а подключения уже нет
                return
            if self._qr_task:
                self._qr_task.cancel()
            self._qr_task = asyncio.current_task()
            try:
                qr = await self.client.qr_login()
                while True:
                    self._emit("qr", qr.url)
                    try:
                        await qr.wait(timeout=30)
                        break
                    except asyncio.TimeoutError:
                        await qr.recreate()
            except errors.SessionPasswordNeededError:
                self._qr_task = None
                self._emit("password_needed")
                return
            except API_KEY_ERRORS:
                self._qr_task = None
                self._emit("login_error", api_key_error(self.cfg))
                return
            self._qr_task = None
            await self._after_login()
        self._submit(go())

    def cancel_login(self):
        self.stop()

    async def _resolve_channel(self, name: str):
        """Канал по записи из списка (см. normalize_channel). ValueError — с понятной причиной."""
        from telethon.tl import functions, types
        if name.startswith("t.me/+"):   # ссылка-приглашение в закрытый канал
            try:
                inv = await self.client(functions.messages.CheckChatInviteRequest(name[len("t.me/+"):]))
            except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
                raise ValueError("ссылка-приглашение недействительна или устарела")
            if isinstance(inv, types.ChatInviteAlready):
                return inv.chat
            # Вступать сами не будем — это действие от имени аккаунта
            raise ValueError("аккаунт не состоит в этом канале — вступите в него по ссылке в Telegram "
                             "и переподключите аккаунт")
        if name.startswith("t.me/c/"):   # закрытый канал по id
            peer = int("-100" + name[len("t.me/c/"):])
            try:
                return await self.client.get_entity(peer)
            except ValueError:
                pass
            # Telegram отдаёт закрытый канал по id, только если он есть в списке чатов аккаунта
            if not self._dialogs_loaded:
                await self.client.get_dialogs()
                self._dialogs_loaded = True
            try:
                return await self.client.get_entity(peer)
            except ValueError:
                raise ValueError("аккаунт не состоит в этом закрытом канале")
        return await self.client.get_entity(name)

    @staticmethod
    def _channel_title(ent) -> str:
        return "@" + ent.username if getattr(ent, "username", None) else getattr(ent, "title", "канал")

    async def _with_flood_wait(self, call):
        """call() — запрос к Telegram. Попросил подождать до 5 минут (FloodWait или медленный
        режим группы) — ждём и повторяем один раз; дольше — ошибка уходит наверх."""
        try:
            return await call()
        except (errors.FloodWaitError, errors.SlowModeWaitError) as e:
            if e.seconds > 300:
                raise
            self.log.warning("%s %s сек — подожду и повторю",
                             "В группе медленный режим:" if isinstance(e, errors.SlowModeWaitError)
                             else "Telegram просит подождать", e.seconds)
            await asyncio.sleep(e.seconds + 1)
            return await call()

    def channel_title(self, name: str) -> str:
        return self.titles.get(name) or channel_label(name)

    async def _after_login(self):
        me = await self.client.get_me()
        self.premium = bool(getattr(me, "premium", False))
        self.running = True
        self._emit("me", me.first_name)
        self._emit("status", "running")
        self.log.info("Вошли как %s%s", me.first_name, " (Premium: подпись к картинке до 4096 знаков)"
                      if self.premium else "")
        await self._resolve_targets()
        self._refresh_task = asyncio.ensure_future(self._refresh_loop())

    async def _resolve_targets(self):
        """Находит каналы из настроек и проверяет, может ли аккаунт в них публиковать."""
        for name in channels(self.cfg):
            if name in self.targets:
                continue
            try:
                ent = await self._resolve_channel(name)
            except Exception as e:
                self.log.error("Канал %s не найден: %s", channel_label(name), e)
                continue
            self.targets[name] = ent
            self.titles[name] = self._channel_title(ent)
            broadcast = isinstance(ent, types.Channel) and ent.broadcast
            rights = getattr(ent, "admin_rights", None)
            ok = not broadcast or bool(getattr(ent, "creator", False) or (rights and rights.post_messages))
            self.can_post[name] = ok
            if ok:
                self.log.info("Канал %s: публиковать можно ✓", self.titles[name])
            else:
                self.log.error("Канал %s: аккаунт не может в нём публиковать — нужен админ с правом "
                               "«Публикация сообщений»", self.titles[name])

    def update_channels(self):
        """Список каналов поменяли в настройках — найти новые без переподключения."""
        async def go():
            if not self.running:
                return
            for gone in set(self.targets) - set(channels(self.cfg)):
                self.targets.pop(gone, None)
                self.scheduled.pop(gone, None)
            await self._resolve_targets()
            await self._refresh_scheduled()
        self._submit(go())

    async def _target(self, name: str):
        if name not in self.targets:
            await self._resolve_targets()
        if name not in self.targets:
            raise RuntimeError(f"канал {channel_label(name)} не найден — проверьте его в «Настройках»")
        return self.targets[name]

    # --- генерация ---

    def _draft(self, did: str) -> Draft | None:
        d = self.store.get(did)
        return d if d and d.profile == self.cfg["id"] else None

    def _changed(self, d: Draft):
        self.store.save()
        self._emit("draft", d.id)

    # Очередь генерации. Каждый запрос к нейросети — op: «draft» (текст, потом картинка), «text», «image».
    # Упёрлись в лимит — нейросеть этого вида (текст / картинки) на паузе до сброса: остальные черновики
    # не дёргают её зря, а ждут (retry_op + wait_until), и в момент сброса движок сам продолжает очередь.

    def generate(self, did: str):
        """Новый черновик: сначала текст, потом картинка к нему."""
        return self._submit(self._run(did, "draft", queued=True))

    def regen_text(self, did: str, wish: str = "", edit: bool = False):
        """edit — поправить текущий текст по пожеланию, иначе написать заново."""
        return self._submit(self._run(did, "text", wish, edit))

    def regen_image(self, did: str, wish: str = "", edit: bool = False):
        return self._submit(self._run(did, "image", wish, edit))

    def retry_all(self):
        """«↻ Повторить сейчас»: все черновики профиля с ошибкой генерации или в ожидании лимита — сейчас."""
        async def go():
            for kind in KINDS:
                self._unpause(kind)
            items = self._retry_items()
            if not items:
                return
            self.log.info("↻ Повторяю генерацию: %d %s", len(items),
                          plural(len(items), "черновик", "черновика", "черновиков"))
            for d in items:
                if d.retry_op and d.status == REVIEW and not d.busy:
                    await self._run(d.id, d.retry_op, d.retry_wish, d.retry_edit, queued=True)
        return self._submit(go())

    def _retry_items(self, kind: str | None = None, waiting_only: bool = False) -> list[Draft]:
        items = [d for d in self.store.list(REVIEW, self.cfg["id"])
                 if d.retry_op and (not waiting_only or d.waiting) and (kind is None or op_kind(d.retry_op) == kind)]
        return sorted(items, key=lambda d: d.created)

    def _gen(self, kind: str):
        return self.text_gen if kind == "text" else self.image_gen

    async def _run(self, did: str, op: str, wish: str = "", edit: bool = False, queued: bool = False) -> str:
        """queued — из очереди (новые темы, автоповтор): на паузе по лимиту нейросеть не дёргаем.
        Действие пользователя пробует сразу — вдруг лимит уже сбросился. Итог: ok / error / limit / skip."""
        d = self._draft(did)
        if not d or d.status != REVIEW or (d.image_busy if op == "image" else d.text_busy):
            return "skip"
        d.clear_retry()
        if op == "text":
            return await self._gen_text(d, op, wish, edit, queued)
        if op == "image":
            return await self._gen_image(d, op, wish, edit, queued)
        d.text_busy = d.image_busy = True
        self._emit("draft", d.id)
        result = await self._gen_text(d, op, wish, edit, queued)
        if result == "ok" and d.text.strip():
            return await self._gen_image(d, op, "", False, queued)
        d.image_busy = False
        self._changed(d)
        return result

    def _paused(self, kind: str) -> Limit | None:
        lim = self.pause[kind]
        if lim and self.pause_by[kind] != self._gen(kind).name:
            # Нейросеть сменили в «Настройках» — старый лимит не про неё: ждущие черновики — сразу к новой
            self.log.info("%s: нейросеть сменили на %s — продолжаю очередь", KIND_NAME[kind], self._gen(kind).name)
            self._unpause(kind)
            asyncio.ensure_future(self._resume(kind))
            return None
        return lim

    def _unpause(self, kind: str):
        self.pause[kind], self.streak[kind] = None, 0
        task = self._wake[kind]
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._wake[kind] = None

    def _hit_limit(self, kind: str, e: Exception, name: str) -> Limit:
        lim = classify(e, self.streak[kind])
        self.streak[kind] += 1
        self.pause[kind], self.pause_by[kind] = lim, name
        if lim.known:
            how = "время сброса сообщил сервис"
        elif lim.refill:
            how = "похоже, закончились деньги на балансе — проверю, не пополнили ли"
        else:
            how = "когда сброс, сервис не сказал — попробую"
        self.log.warning("⏸ %s: лимит исчерпан (%s). %s на паузе до %s (%s), потом продолжу сама",
                         name, short(lim.reason, 300), KIND_NAME[kind], sched_mod.fmt(lim.retry_at), how)
        task = self._wake[kind]
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._wake[kind] = asyncio.ensure_future(self._wake_at(kind, lim))
        return lim

    def _wait(self, d: Draft, op: str, wish: str, edit: bool, lim: Limit, name: str):
        d.retry_op, d.retry_wish, d.retry_edit = op, wish, edit
        d.wait_until = lim.retry_at.isoformat(timespec="seconds")
        d.error = (f"⏸ {name}: лимит исчерпан. Продолжу сама {sched_mod.fmt(lim.retry_at)}, если программа "
                   "будет открыта. Не ждать — «↻ Повторить сейчас» вверху или другая нейросеть в «Настройках»")

    async def _wake_at(self, kind: str, lim: Limit):
        # Спим кусками по минуте: после сна компьютера или перевода часов проснёмся вовремя
        while (left := (lim.retry_at - datetime.now().astimezone()).total_seconds()) > 0:
            await asyncio.sleep(min(60.0, left))
        if self.pause[kind] is lim:
            await self._resume(kind)

    async def _resume(self, kind: str):
        self.pause[kind] = None   # счётчик отказов подряд не сбрасываем: снова лимит — подождём дольше
        items = self._retry_items(kind, waiting_only=True)
        if not items:
            return
        self.log.info("▶ %s: время сброса лимита — продолжаю очередь (ждут %d)", self._gen(kind).name, len(items))
        for d in items:
            if d.waiting and d.status == REVIEW and not d.busy:
                await self._run(d.id, d.retry_op, d.retry_wish, d.retry_edit, queued=True)

    async def _resume_saved(self):
        """После перезапуска: черновики, ждавшие сброса лимита, — в очередь к сохранённому времени."""
        now = datetime.now().astimezone()
        for kind in KINDS:
            times = [d.wait_time() or now for d in self._retry_items(kind, waiting_only=True)]
            if not times:
                continue
            at = max(min(times), now + timedelta(seconds=20))
            lim = Limit("ожидание сохранилось с прошлого запуска", at, True)
            self.pause[kind], self.pause_by[kind] = lim, self._gen(kind).name
            self.log.info("%s: ждут сброса лимита — %d %s, продолжу %s", KIND_NAME[kind], len(times),
                          plural(len(times), "черновик", "черновика", "черновиков"), sched_mod.fmt(at))
            self._wake[kind] = asyncio.ensure_future(self._wake_at(kind, lim))

    async def _gen_text(self, d: Draft, op: str, wish: str, edit: bool, queued: bool) -> str:
        cfg, gen = self.cfg, self.text_gen
        async with self._locks["text"]:
            lim = self._paused("text") if queued else None
            if lim:
                self._wait(d, op, wish, edit, lim, self.pause_by["text"])
                d.text_busy = False
                self._changed(d)
                return "limit"
            d.text_busy, d.error = True, ""
            self._emit("draft", d.id)
            edit = edit and bool(d.text.strip())
            what = (f"правлю текст по пожеланию «{short(wish, 120)}»" if edit else
                    f"пишу текст заново по пожеланию «{short(wish, 120)}»" if wish else
                    "пишу текст заново" if op == "text" else "пишу текст")
            self.log.info("«%s»: %s — %s…", label(d), what, gen.name)
            started = time.monotonic()
            try:
                recent = self.store.recent_texts(cfg["id"], d.channel, int(cfg.get("recent_in_prompt", 5)))
                system, user = build_text_request(cfg, d.topic, wish, d.text if edit else "", recent)
                text = clean_post(await gen.generate(system, user), cfg)
                limit = int(cfg.get("post_max_len", 900))
                shortened = ""
                if len(visible_text(text, cfg)) > limit * TOO_LONG_RETRY:
                    self.log.info("«%s»: вышло %d знаков при цели до %d — прошу сократить", label(d),
                                  post_length(text, cfg), limit)
                    shortened = f", сокращён с {post_length(text, cfg)}"
                    system, user = build_text_request(cfg, d.topic, f"сократи до {limit} знаков, сохранив главное",
                                                      text, recent)
                    text = clean_post(await gen.generate(system, user), cfg)
                d.text = d.generated = text
                d.text_source = gen.name
                self.streak["text"] = 0
                self.log.info("«%s»: ✓ текст готов — %s, %d знаков%s, %s", label(d), gen.name,
                              post_length(text, cfg), shortened, took(started))
                return "ok"
            except Exception as e:
                if is_limit(e):
                    self._wait(d, op, wish, edit, self._hit_limit("text", e, gen.name), gen.name)
                    return "limit"
                d.error = explain_ai_error(gen.name, str(e))
                d.retry_op, d.retry_wish, d.retry_edit = op, wish, edit
                self.log.error("«%s»: ✗ текст не получился (%s, %s): %s", label(d), gen.name, took(started),
                               d.error)
                return "error"
            finally:
                d.text_busy = False
                self._changed(d)

    async def _gen_image(self, d: Draft, op: str, wish: str, edit: bool, queued: bool) -> str:
        gen = self.image_gen
        if gen.backend == "none":
            d.image_busy = False
            self._changed(d)
            return "ok"
        base = d.image_path if edit else None
        if edit and not base:
            d.error = "Нет картинки, которую можно поправить — нарисуйте новую"
            d.image_busy = False
            self._changed(d)
            return "error"
        async with self._locks["image"]:
            lim = self._paused("image") if queued else None
            if lim:   # текст уже есть — дорисуем картинку после сброса
                self._wait(d, "image", wish, edit, lim, self.pause_by["image"])
                d.image_busy = False
                self._changed(d)
                return "limit"
            d.image_busy, d.error = True, ""
            self._emit("draft", d.id)
            what = (f"правлю текущую картинку по пожеланию «{short(wish, 120)}»" if edit else
                    f"рисую картинку заново по пожеланию «{short(wish, 120)}»" if wish else
                    "рисую картинку заново" if op == "image" else "рисую картинку")
            if gen.backend == "folder":
                what = "беру картинку из папки"
            self.log.info("«%s»: %s — %s…", label(d), what, gen.name)
            started = time.monotonic()
            try:
                prompt = edit_image_prompt(wish) if edit else build_image_prompt(self.cfg, d.topic, d.text, wish)
                path = await gen.generate(prompt, d.folder, base)
                if path:
                    d.images.append(str(path))
                    d.image, d.image_source = str(path), gen.name
                    self.streak["image"] = 0
                    self.log.info("«%s»: ✓ картинка готова — %s, %s, вариант %d (%s)", label(d), gen.name,
                                  took(started), len(d.images), path.name)
                return "ok"
            except Exception as e:
                if is_limit(e):
                    self._wait(d, "image", wish, edit, self._hit_limit("image", e, gen.name), gen.name)
                    return "limit"
                d.error = "Картинка: " + explain_ai_error(gen.name, str(e))
                d.retry_op, d.retry_wish, d.retry_edit = "image", wish, edit
                self.log.error("«%s»: ✗ картинка не получилась (%s, %s): %s", label(d), gen.name, took(started),
                               d.error)
                return "error"
            finally:
                d.image_busy = False
                self._changed(d)

    # --- публикация ---

    def caption_limit(self) -> int:
        return PREMIUM_CAPTION_LIMIT if self.premium else CAPTION_LIMIT

    def publish_problem(self, d: Draft) -> str:
        """Почему пост нельзя отправить как есть; "" — можно."""
        if not d.text.strip() and not d.image_path:
            return "Пост пустой"
        n = post_length(d.text, self.cfg)
        if n > MESSAGE_LIMIT:
            return f"Пост длиннее {MESSAGE_LIMIT} знаков ({n}) — Telegram такие не принимает, сократите"
        if d.image_path and n > self.caption_limit() and self.cfg.get("long_text") != "split":
            return (f"Текст ({n} знаков) не помещается в подпись к картинке ({self.caption_limit()}) — сократите "
                    "или включите в «Настройках» → «Пост»: «картинка и текст отдельными сообщениями»")
        return ""

    def publish(self, did: str, when: datetime | None):
        """when — отложить до этого времени; None — опубликовать сейчас."""
        async def go():
            d = self._draft(did)
            if not d or d.busy or d.status != REVIEW:
                return
            d.error = ""
            try:
                await self._publish(d, when)
            except Exception as e:
                d.error = self._send_error(e)
                self.log.error("«%s»: ✗ не опубликован — %s", label(d), d.error)
            self._changed(d)
            await self._refresh_scheduled()
        self._submit(go())

    async def _publish(self, d: Draft, when: datetime | None):
        if not self.running or not self.client:
            raise RuntimeError("аккаунт не подключён к Telegram — нажмите «▶ Запустить»")
        problem = self.publish_problem(d)
        if problem:
            raise RuntimeError(problem)
        ent = await self._target(d.channel)
        text = post_html(d.text, self.cfg) if d.text.strip() else ""
        img = d.image_path
        n = post_length(d.text, self.cfg)
        sent = []
        if img and n > self.caption_limit():   # картинка отдельно, текст следом
            sent.append(await self._with_flood_wait(lambda: self.client.send_file(ent, str(img), schedule=when)))
            sent.append(await self._with_flood_wait(lambda: self.client.send_message(
                ent, text, parse_mode="html", link_preview=False, schedule=when)))
        elif img:
            sent.append(await self._with_flood_wait(lambda: self.client.send_file(
                ent, str(img), caption=text, parse_mode="html", schedule=when)))
        else:
            sent.append(await self._with_flood_wait(lambda: self.client.send_message(
                ent, text, parse_mode="html", link_preview=False, schedule=when)))
        d.msg_ids = [m.id for m in sent]
        d.clear_retry()   # опубликовали как есть — ждать картинку больше незачем
        d.chat_id = utils.get_peer_id(ent)
        d.publish_at = (when or datetime.now(timezone.utc)).astimezone().isoformat(timespec="seconds")
        what = self._post_summary(d)
        if when:
            d.status = SCHEDULED
            self.log.info("✅ «%s» в отложенных %s — выйдет %s (%s)", label(d), self.channel_title(d.channel),
                          sched_mod.fmt(when), what)
        else:
            d.status, d.link = PUBLISHED, self._link(ent, d.msg_ids[0])
            self.log.info("✅ «%s» опубликован в %s (%s) — %s", label(d), self.channel_title(d.channel), what,
                          d.link)

    def _post_summary(self, d: Draft) -> str:
        """«812 знаков, текст правлен вручную, картинка — вариант 2 из 3» — для журнала."""
        parts = [f"{post_length(d.text, self.cfg)} знаков" if d.text.strip() else "без текста"]
        if d.text.strip() and d.generated and d.text.strip() != d.generated.strip():
            parts.append("текст правлен вручную")
        if d.image_path:
            n = len(d.images)
            pic = f"картинка {d.image_source}" if d.image_source else "картинка"
            if n > 1 and d.image in d.images:
                pic += f", вариант {d.images.index(d.image) + 1} из {n}"
            parts.append(pic)
            if post_length(d.text, self.cfg) > self.caption_limit():
                parts.append("текст отдельным сообщением")
        else:
            parts.append("без картинки")
        return ", ".join(parts)

    @staticmethod
    def _send_error(e: Exception) -> str:
        if isinstance(e, (errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError)):
            return "аккаунт не может публиковать в канале — нужен админ с правом «Публикация сообщений»"
        if isinstance(e, errors.ScheduleTooMuchError):
            return "в отложке канала уже 100 постов — больше Telegram не принимает"
        if isinstance(e, errors.ScheduleDateTooLateError):
            return "слишком далеко: отложить можно не больше чем на год"
        if isinstance(e, errors.ScheduleDateInvalidError):
            return "время уже прошло или слишком близко — выберите другое"
        if isinstance(e, errors.MediaCaptionTooLongError):
            return "текст не помещается в подпись к картинке — сократите"
        if isinstance(e, errors.MessageTooLongError):
            return f"пост длиннее {MESSAGE_LIMIT} знаков — сократите"
        if isinstance(e, errors.FloodWaitError):
            return f"Telegram ограничил отправку на {e.seconds} сек"
        return str(e)

    @staticmethod
    def _link(ent, msg_id: int) -> str:
        if getattr(ent, "username", None):
            return f"https://t.me/{ent.username}/{msg_id}"
        return f"https://t.me/c/{utils.resolve_id(utils.get_peer_id(ent))[0]}/{msg_id}"

    # --- отложенные посты ---

    def refresh_scheduled(self):
        self._submit(self._refresh_scheduled())

    async def _refresh_loop(self):
        while True:
            try:
                await self._refresh_scheduled()
            except Exception as e:
                self.log.warning("Не удалось сверить отложенные посты: %s", e)
            await asyncio.sleep(REFRESH_EVERY)

    async def _refresh_scheduled(self):
        """Список отложенных постов каналов из Telegram — и наши, и поставленные вручную.
        Заодно черновики, которые Telegram уже опубликовал, переходят в «Опубликовано»."""
        if not self.running or not self.client:
            return
        now = datetime.now(timezone.utc)
        for name in channels(self.cfg):
            ent = self.targets.get(name)
            if not ent:
                continue
            try:
                msgs = [m async for m in self.client.iter_messages(ent, scheduled=True)]
            except errors.ChatAdminRequiredError:
                self.scheduled[name] = []
                continue
            ours = {i: d for d in self.store.list(SCHEDULED, self.cfg["id"]) if d.channel == name for i in d.msg_ids}
            items: dict[str, dict] = {}
            for m in sorted(msgs, key=lambda m: (m.date, m.id)):
                d = ours.get(m.id)
                key = f"d:{d.id}" if d else (f"g:{m.grouped_id}" if m.grouped_id else f"m:{m.id}")
                it = items.setdefault(key, {"channel": name, "ids": [], "date": m.date, "text": "",
                                            "media": False, "draft": d.id if d else ""})
                it["ids"].append(m.id)
                it["text"] = it["text"] or (m.raw_text or "")
                it["media"] = it["media"] or bool(m.media)
            self.scheduled[name] = sorted(items.values(), key=lambda it: it["date"])
            alive = {i for it in items.values() for i in it["ids"]}
            for d in {x.id: x for x in ours.values()}.values():
                if any(i in alive for i in d.msg_ids):
                    continue
                when = d.when()
                if when and when <= now + timedelta(minutes=1):
                    d.status = PUBLISHED
                    d.link = await self._find_published(ent, d) or d.link
                    self.log.info("✅ Отложенный пост «%s» вышел в %s%s", label(d), self.channel_title(name),
                                  f" — {d.link}" if d.link else "")
                else:   # убрали из отложки в самом Telegram
                    d.status, d.msg_ids = REVIEW, []
                    d.error = "Пост убрали из отложенных в Telegram — он снова на проверке"
                    self.log.warning("«%s»: убран из отложенных в самом Telegram — вернул на проверку", label(d))
                self._changed(d)
        self.scheduled_at = datetime.now().astimezone()
        self._emit("scheduled")

    async def _find_published(self, ent, d: Draft) -> str:
        """Ссылка на вышедший отложенный пост: у него уже новый номер — ищем по времени и тексту."""
        when = d.when()
        try:
            recent = await self.client.get_messages(ent, limit=20)
        except Exception:
            return ""
        start = visible_text(d.text, self.cfg).strip()[:40]
        for m in recent:
            if when and abs((m.date - when).total_seconds()) < 600 and (
                    not start or (m.raw_text or "").strip().startswith(start) or (m.media and not m.raw_text)):
                return self._link(ent, m.id)
        return ""

    def taken_times(self, channel: str) -> list[datetime]:
        """Занятые времена канала: отложенные в Telegram и наши запланированные (если список ещё не сверяли)."""
        times = [it["date"] for it in self.scheduled.get(channel, [])]
        times += [w for d in self.store.list(SCHEDULED, self.cfg["id"]) if d.channel == channel
                  if (w := d.when())]
        return times

    def _post_name(self, channel: str, ids: list[int]) -> str:
        """Как назвать отложенный пост в журнале: тема нашего черновика или начало текста."""
        for d in self.store.list(SCHEDULED, self.cfg["id"]):
            if set(d.msg_ids) & set(ids):
                return f"«{label(d)}»"
        for it in self.scheduled.get(channel, []):
            if set(it["ids"]) & set(ids):
                text = short(it["text"], 50)
                return f"пост «{text}»" if text else "пост с картинкой"
        return "пост"

    def reschedule(self, channel: str, ids: list[int], when: datetime):
        async def go():
            name = self._post_name(channel, ids)
            try:
                ent = await self._target(channel)
                for i in ids:
                    await self._with_flood_wait(lambda i=i: self.client.edit_message(ent, i, schedule=when))
                for d in self.store.list(SCHEDULED, self.cfg["id"]):
                    if set(d.msg_ids) & set(ids):
                        d.publish_at = when.astimezone().isoformat(timespec="seconds")
                        self._changed(d)
                self.log.info("🕒 %s в %s перенесён на %s", name, self.channel_title(channel), sched_mod.fmt(when))
            except Exception as e:
                self.log.error("✗ %s: не удалось перенести на %s — %s", name, sched_mod.fmt(when),
                               self._send_error(e))
            await self._refresh_scheduled()
        self._submit(go())

    def send_now(self, channel: str, ids: list[int]):
        async def go():
            name = self._post_name(channel, ids)
            try:
                ent = await self._target(channel)
                await self._with_flood_wait(lambda: self.client(
                    functions.messages.SendScheduledMessagesRequest(ent, ids)))
                for d in self.store.list(SCHEDULED, self.cfg["id"]):
                    if set(d.msg_ids) & set(ids):
                        d.publish_at = datetime.now().astimezone().isoformat(timespec="seconds")
                        self._changed(d)
                self.log.info("✅ %s из отложенных опубликован сейчас в %s", name, self.channel_title(channel))
                await asyncio.sleep(2)
            except Exception as e:
                self.log.error("✗ %s: не удалось опубликовать сейчас — %s", name, self._send_error(e))
            await self._refresh_scheduled()
        self._submit(go())

    def unschedule(self, channel: str, ids: list[int]):
        """Снять с отложки. Наш черновик возвращается на проверку, чужой пост удаляется из отложенных."""
        async def go():
            name = self._post_name(channel, ids)
            ours = False
            try:
                ent = await self._target(channel)
                await self._with_flood_wait(lambda: self.client(
                    functions.messages.DeleteScheduledMessagesRequest(ent, ids)))
                for d in self.store.list(SCHEDULED, self.cfg["id"]):
                    if set(d.msg_ids) & set(ids):
                        d.status, d.msg_ids, d.publish_at = REVIEW, [], ""
                        ours = True
                        self._changed(d)
                if ours:
                    self.log.info("↩ %s снят с отложенных в %s — снова на проверке", name, self.channel_title(channel))
                else:
                    self.log.info("🗑 %s удалён из отложенных %s", name, self.channel_title(channel))
            except Exception as e:
                self.log.error("✗ %s: не удалось снять с отложенных — %s", name, self._send_error(e))
            await self._refresh_scheduled()
        self._submit(go())

    # --- черновики без Telegram ---

    def cancel(self, did: str):
        d = self._draft(did)
        if d and d.status == REVIEW and not d.busy:
            d.status = CANCELLED
            d.clear_retry()
            self.log.info("✕ «%s»: отменён — лежит в «Отменённых», можно вернуть", label(d))
            self._changed(d)

    def restore(self, did: str):
        d = self._draft(did)
        if d and d.status == CANCELLED:
            d.status, d.error = REVIEW, ""
            self.log.info("↩ «%s»: возвращён на проверку", label(d))
            self._changed(d)
