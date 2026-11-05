"""
Окно автопостера. Запуск: start.bat или python app.py
"""
import ctypes
import json
import logging
import os
import queue
import shutil
import socket
import sys
import threading
import time
import webbrowser
import zlib
from datetime import datetime
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox as tk_messagebox

import customtkinter as ctk
import qrcode
from PIL import Image

import core
import updater
import schedule as sched
from core import BASE
from drafts import CANCELLED, PUBLISHED, REVIEW, Draft, DraftStore
from engine import Engine, short

GREEN, GREEN_HOVER = "#2e9e5b", "#257f49"
GRAY, GRAY_HOVER = ("#9aa0a6", "#4a4d52"), ("#80868b", "#5f6368")
RED = "#d9534f"
ORANGE = "#e0a030"
STATUS = {
    "stopped": ("Остановлен", "#9aa0a6"),
    "connecting": ("Подключаюсь…", "#f0ad4e"),
    "login": ("Нужен вход в Telegram", "#f0ad4e"),
    "running": ("Работает", "#2e9e5b"),
}
BTN_COLOR = ctk.ThemeManager.theme["CTkButton"]["fg_color"]
BTN_HOVER = ctk.ThemeManager.theme["CTkButton"]["hover_color"]
LABEL_W = 270  # ширина колонки подписей в блоке «Нейросеть»
BACKENDS = {"Z.ai (бесплатно)": "zai", "Claude Code": "claude_code", "Claude API": "api",
            "Antigravity (Google)": "gemini_cli", "Gemini API": "gemini_api",
            "ChatGPT (Codex)": "codex", "OpenAI API": "openai_api",
            "Своя модель": "openai_compat"}
DEFAULT_MODEL = "(по умолчанию)"
PROXY_MODES = {"Системный (из VPN)": "system", "Свой": "custom", "Без прокси": "none"}
TEXT_PLACEHOLDERS = "{тема}, {пожелание}, {длина}, {оформление}, {эмодзи}, {хэштеги}, {прошлые посты}"
IMAGE_PLACEHOLDERS = "{тема}, {текст поста}, {пожелание}, {стиль}, {формат}"
IMAGE_BACKENDS = {"OpenAI API": "openai_api", "Gemini API": "gemini_api", "ChatGPT (Codex)": "codex",
                  "Z.ai": "zai", "Своя модель": "openai_compat", "Из папки": "folder", "Без картинки": "none"}
ASPECT_NAMES = {"Квадрат": "square", "Горизонтальная": "landscape", "Вертикальная": "portrait"}
QUALITY_NAMES = {"Авто": "auto", "Низкое": "low", "Среднее": "medium", "Высокое": "high"}
FORMAT_NAMES = {"Без форматирования": "plain", "Жирный и курсив": "basic", "+ ссылки, цитаты, код": "full"}
EMOJI_NAMES = {"Без эмодзи": "none", "Немного": "few", "Много": "many"}
HASHTAG_NAMES = {"Без хэштегов": "none", "Из моего списка": "own", "Подбирает нейросеть": "ai"}
LONG_NAMES = {"Картинка и текст отдельными сообщениями": "split", "Не публиковать — сократить": "shorten"}
SCHED_MODES = {"По дням недели": "slots", "Каждые N часов": "interval"}
REVIEW_VIEWS = ("На проверке", "Отменённые")
IMG_BOX = 300   # размер превью картинки в «Проверке»


def key_of(names: dict, value, default=None):
    """Подпись в окне по значению из настроек."""
    return next((k for k, v in names.items() if v == value), default or next(iter(names)))


log = logging.getLogger("autopost")


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(record)


def setup_logging() -> queue.Queue:
    q = queue.Queue()
    fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s")
    file_h = logging.FileHandler(BASE / "log.txt", encoding="utf-8")
    file_h.setFormatter(fmt)
    gui_h = QueueHandler(q)
    log.setLevel(logging.INFO)
    log.addHandler(file_h)
    log.addHandler(gui_h)
    # Служебный шум Telethon («Got difference…») — только предупреждения и в файл
    tl = logging.getLogger("telethon")
    tl.setLevel(logging.WARNING)
    tl.addHandler(file_h)
    # Кроме пауз по FloodWait: Telethon выжидает их молча (уровень INFO), а это задержка публикации
    logging.getLogger("telethon.client.users").setLevel(logging.INFO)
    return q


def thumbnail(path: Path | None, box: int) -> ctk.CTkImage | None:
    if not path or path.suffix.lower() == ".mp4" or not path.exists():
        return None
    try:
        img = Image.open(path)
        img.thumbnail((box, box))
        return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
    except Exception:
        return None


def play_sound():
    """Свой тихий деревянный «тук-тук» (assets/notify.wav) вместо системного звука Windows."""
    try:
        import winsound
        winsound.PlaySound(str(core.RES / "assets" / "notify.wav"),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:   # нет звуковой карты, файла и т. п. — молча без звука
        log.debug("Звук не проигран: %s", e)


class Messages:
    """Замена tkinter.messagebox: системные окна Windows всплывают поверх всего и звучат.
    Сообщения — тихая полоса над вкладками, вопросы — тихое окно в стиле программы."""
    app: "App | None" = None   # окно программы регистрирует себя при создании

    def _say(self, kind, title, text):
        if self.app:
            self.app.say(kind, title, text)

    def showinfo(self, title, text):
        self._say("info", title, text)

    def showwarning(self, title, text):
        self._say("warning", title, text)

    def showerror(self, title, text):
        self._say("error", title, text)

    def askyesno(self, title, text) -> bool:
        return bool(self.app) and self.app.ask(title, text, "Да", "Нет")

    def askokcancel(self, title, text) -> bool:
        return bool(self.app) and self.app.ask(title, text, "ОК", "Отмена")


messagebox = Messages()   # вызывается так же, как tkinter.messagebox


def open_path(p: Path):
    os.startfile(str(p))  # Windows


HOTKEY_ACTIONS = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}   # V, C, X, A


def hotkey_any_layout(e):
    """Ctrl+V/C/X/A в любой раскладке. tkinter узнаёт сочетание по букве (keysym), а в русской раскладке
    на той же клавише «м», «с», «ч», «ф» — и вставка молча не срабатывает. Смотрим на физическую
    клавишу (keycode — код клавиши Windows) и вызываем нужное действие сами."""
    action = HOTKEY_ACTIONS.get(e.keycode)
    if action and e.keysym.lower() not in ("v", "c", "x", "a"):   # латиница — tkinter справится сам
        e.widget.event_generate(action)
        return "break"


def fix_hotkeys_any_layout(root):
    root.bind_all("<Control-KeyPress>", hotkey_any_layout, add="+")


def repaint_after_scroll(sf: ctk.CTkScrollableFrame):
    """customtkinter на Windows при прокрутке иногда оставляет «огрызки» виджетов (зависит от масштаба
    экрана и видеокарты): виджеты переезжают, а освободившееся место не перерисовывается.
    Через миг после любого сдвига (колесо, ползунок, клавиши) просим Windows перерисовать всю область."""
    if sys.platform != "win32":
        return
    canvas = sf._parent_canvas
    pending = {"id": None}
    flags = 0x0001 | 0x0004 | 0x0080 | 0x0100   # RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW

    def redraw():
        pending["id"] = None
        ctypes.windll.user32.RedrawWindow(canvas.winfo_id(), None, None, flags)

    def on_scroll(*args):
        sf._scrollbar.set(*args)   # как было у customtkinter
        if pending["id"]:
            canvas.after_cancel(pending["id"])
        pending["id"] = canvas.after(40, redraw)
    canvas.configure(yscrollcommand=on_scroll)


# Две копии программы мешали бы друг другу (одни и те же сессии Telegram, двойные посты).
# Первая копия слушает локальный порт; вторая просит её показать окно и выходит.
INSTANCE_HELLO, INSTANCE_REPLY = b"tg-autopost:show", b"tg-autopost:ok"
INSTANCE_PORT = 20000 + zlib.crc32(str(BASE).encode()) % 20000   # своя у каждой папки с программой


def single_instance() -> socket.socket | None | bool:
    """Сокет первой копии; False — программа уже запущена (окно ей показано); None — проверка невозможна."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as c:
            c.sendall(INSTANCE_HELLO)
            if c.recv(64) == INSTANCE_REPLY:
                return False
    except OSError:
        pass
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen()
        return srv
    except OSError:   # порт занят чем-то другим — просто работаем без проверки
        srv.close()
        return None


# ======================================================================
#  Значок в трее
# ======================================================================

APP_COLOR = "#7b4fd6"   # цвет значка программы (assets/icon.ico рисуется из tray_image: python -c "import app; app.make_icon()")


def tray_image(color: str, size: int = 64) -> Image.Image:
    """Белый бумажный самолётик на скруглённом квадрате цвета статуса.
    Квадрат, а не круг, — чтобы не путать с TG Автокомментатором."""
    from PIL import ImageDraw
    big = size * 4                       # рисуем крупно и уменьшаем — края получаются гладкими
    k = big / 64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(14 * k), fill=color)
    d.polygon([(9 * k, 31 * k), (55 * k, 12 * k), (43 * k, 52 * k), (31 * k, 40 * k)], fill="white")
    d.polygon([(31 * k, 40 * k), (55 * k, 12 * k), (27 * k, 52 * k)], fill=(214, 222, 236))
    return img.resize((size, size), Image.LANCZOS)


def make_icon():
    """Пересобрать assets/icon.ico из tray_image."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    tray_image(APP_COLOR, 256).save(core.RES / "assets" / "icon.ico", sizes=[(n, n) for n in sizes])


class Tray:
    """Значок в трее: окно можно закрыть, а программа продолжит работать.
    Меню pystray работает в своём потоке — действия передаём в окно через app.call_soon."""

    def __init__(self, app: "App"):
        import pystray
        self.app = app
        item = pystray.MenuItem
        self.icon = pystray.Icon("tg_autopost", tray_image(STATUS["stopped"][1]), core.APP_TITLE,
                                 pystray.Menu(
                                     item("Открыть", lambda: app.call_soon(app.show_window), default=True),
                                     item(lambda i: "■  Остановить" if app.status != "stopped" else "▶  Запустить",
                                          lambda: app.call_soon(app.toggle_run)),
                                     pystray.Menu.SEPARATOR,
                                     item("Выход", lambda: app.call_soon(app.quit_app))))
        self.color = STATUS["stopped"][1]
        self.icon.run_detached()

    def update(self, status: str, tip: str):
        color = STATUS[status][1]
        if color != self.color:
            self.icon.icon, self.color = tray_image(color), color
        self.icon.title = tip[:127]   # у Windows ограничение длины подсказки
        self.icon.update_menu()

    def notify(self, text: str):
        if not self.app.conf.get("tray_notify"):   # у уведомлений Windows звук — по умолчанию выключены
            return
        try:
            self.icon.notify(text, core.APP_TITLE)
        except Exception:
            pass

    def stop(self):
        self.icon.stop()


# ======================================================================
#  Окно входа в Telegram
# ======================================================================

class LoginDialog(ctk.CTkToplevel):
    def __init__(self, app: "App", engine: Engine):
        super().__init__(app)
        self.app, self.engine = app, engine
        multi = len(app.engines) > 1
        self.title(f"Вход в Telegram — {engine.cfg['name']}" if multi else "Вход в Telegram")
        self.geometry("440x560")
        self.resizable(False, False)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.after(100, self.grab_set)

        head = (f"Профиль «{engine.cfg['name']}»: войдите в аккаунт,\nот имени которого он будет публиковать"
                if multi else "Войдите в аккаунт, от имени которого\nбудут публиковаться посты")
        self.head = ctk.CTkLabel(self, text=head, font=ctk.CTkFont(size=15, weight="bold"))
        self.head.pack(pady=(18, 8))

        self.tabs = ctk.CTkTabview(self, height=380)
        self.tabs.pack(fill="both", expand=True, padx=16)
        qr_tab = self.tabs.add("По QR-коду")
        ph_tab = self.tabs.add("По номеру")

        # --- QR ---
        ctk.CTkLabel(qr_tab, text="Telegram на телефоне → Настройки → Устройства →\n"
                                  "Подключить устройство, и наведите камеру на код",
                     justify="center").pack(pady=(6, 8))
        self.qr_label = ctk.CTkLabel(qr_tab, text="", width=240, height=240)
        self.qr_label.pack()
        self.qr_btn = ctk.CTkButton(qr_tab, text="Обновить QR-код", command=self.show_qr)
        self.qr_btn.pack(pady=10)

        # --- телефон ---
        ctk.CTkLabel(ph_tab, text="Номер телефона в международном формате").pack(anchor="w", pady=(10, 2))
        self.phone = ctk.CTkEntry(ph_tab, placeholder_text="+7 900 123-45-67")
        self.phone.pack(fill="x")
        self.code_btn = ctk.CTkButton(ph_tab, text="Получить код", command=self.send_code)
        self.code_btn.pack(fill="x", pady=8)
        self.code_frame = ctk.CTkFrame(ph_tab, fg_color="transparent")
        ctk.CTkLabel(self.code_frame, text="Код из Telegram (придёт в чат «Telegram»)").pack(anchor="w")
        self.code = ctk.CTkEntry(self.code_frame, placeholder_text="12345")
        self.code.pack(fill="x")
        self.code.bind("<Return>", lambda e: self.sign_in_code())
        ctk.CTkButton(self.code_frame, text="Войти", command=self.sign_in_code).pack(fill="x", pady=8)
        ctk.CTkLabel(ph_tab, text="Код не приходит? Используйте вход по QR-коду.",
                     text_color="gray").pack(side="bottom", pady=6)

        # --- общий блок пароля 2FA ---
        self.pw_frame = ctk.CTkFrame(self)
        ctk.CTkLabel(self.pw_frame, text="На аккаунте включён облачный пароль",
                     font=ctk.CTkFont(weight="bold")).pack(pady=(14, 2))
        ctk.CTkLabel(self.pw_frame, text="Это пароль двухэтапной аутентификации Telegram,\n"
                                         "который вы задавали сами — не код из SMS.",
                     text_color="gray", justify="center").pack(pady=(0, 8))
        self.pw = ctk.CTkEntry(self.pw_frame, show="•", placeholder_text="Облачный пароль")
        self.pw.pack(fill="x", padx=16)
        self.pw.bind("<Return>", lambda e: self.sign_in_pw())
        self.pw_show = ctk.CTkCheckBox(self.pw_frame, text="Показать пароль", command=self.toggle_pw)
        self.pw_show.pack(anchor="w", padx=16, pady=(8, 0))
        self.pw_btn = ctk.CTkButton(self.pw_frame, text="Войти", command=self.sign_in_pw)
        self.pw_btn.pack(fill="x", padx=16, pady=(10, 16))

        self.err = ctk.CTkLabel(self, text="", text_color=RED, wraplength=400)
        self.err.pack(pady=(4, 10))
        self.after(300, self.show_qr)

    # действия
    def show_qr(self):
        self.err.configure(text="")
        self.engine.start_qr()

    def send_code(self):
        if not self.phone.get().strip():
            self.err.configure(text="Введите номер телефона")
            return
        self.err.configure(text="")
        self.code_btn.configure(state="disabled", text="Отправляю…")
        self.engine.send_code(self.phone.get())

    def sign_in_code(self):
        self.err.configure(text="")
        self.engine.sign_in_code(self.code.get())

    def sign_in_pw(self):
        if str(self.pw_btn.cget("state")) == "disabled":   # уже проверяем — не копим попытки
            return
        if not self.pw.get():
            self.err.configure(text="Введите облачный пароль")
            return
        self.err.configure(text="")
        self.pw_btn.configure(state="disabled", text="Проверяю…")
        self.engine.sign_in_password(self.pw.get())

    def toggle_pw(self):
        self.pw.configure(show="" if self.pw_show.get() else "•")

    def cancel(self):
        self.engine.cancel_login()
        self.close()

    def close(self):
        self.grab_release()
        self.destroy()
        self.app.login_dialog = None
        self.app.after(300, self.app.next_login)   # следующий аккаунт, которому нужен вход

    # события от движка
    def on_event(self, kind, data):
        if kind == "qr":
            qr = qrcode.QRCode(border=2)
            qr.add_data(data)
            qr.make()
            m = qr.get_matrix()
            img = Image.new("L", (len(m), len(m)), 255)
            img.putdata([0 if v else 255 for row in m for v in row])
            img = img.resize((240, 240), Image.NEAREST)
            self.qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(240, 240))
            self.qr_label.configure(image=self.qr_img)
        elif kind == "code_sent":
            self.code_btn.configure(state="normal", text="Отправить код ещё раз")
            self.code_frame.pack(fill="x", pady=(4, 0))
            self.code.focus()
        elif kind == "password_needed":
            # QR и номер больше не нужны — на их месте поле пароля, ошибки остаются видны
            self.tabs.pack_forget()
            self.head.configure(text="Осталось ввести облачный пароль")
            self.pw_frame.pack(fill="x", padx=16, pady=(8, 0), before=self.err)
            self.pw.focus()
        elif kind == "login_error":
            self.code_btn.configure(state="normal", text="Получить код")
            self.pw_btn.configure(state="normal", text="Войти")
            self.err.configure(text=data)
            if self.pw_frame.winfo_ismapped():
                self.pw.select_range(0, "end")
                self.pw.focus()


# ======================================================================
#  Блоки настроек нейросети
# ======================================================================

def describe_claude(st):
    if not st.get("loggedIn"):
        return None
    plan = st.get("subscriptionType") or ""
    text = f"Вход в Claude выполнен ✓  {st.get('email', '')}" + (f"  ·  подписка {plan}" if plan else "")
    if st.get("authMethod") not in (None, "claude.ai"):
        return text + "  ·  ⚠ вход не по подписке — запросы могут оплачиваться по API", "#e0a030"
    return text, GREEN


class CliBlock:
    """Строка «установлена ли программа / выполнен ли вход» + кнопка, которая делает недостающее."""

    def __init__(self, app: "App", parent, row, name, account, status, install, login, describe, hint,
                 relogin_hint=None):
        self.app, self.name, self.account = app, name, account
        self.status_fn, self.install_fn, self.login_fn = status, install, login
        self.describe, self.hint, self.relogin_hint = describe, hint, relogin_hint
        self.waiting = 0
        self.seen_logout = False
        self.state = {"installed": False}
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row, column=0, columnspan=3, sticky="ew", padx=14, pady=(4, 4))
        self.lbl = ctk.CTkLabel(f, text=f"{name}: проверяю…", text_color="gray")
        self.lbl.pack(side="left")
        # Почта аккаунта скрыта, пока не нажать «показать»
        self.reveal_email = False
        self.email_btn = ctk.CTkButton(f, text="показать", width=70, height=24, fg_color="transparent",
                                       text_color=("#1f6aa5", "#5aa9e6"), hover=False,
                                       command=self.toggle_email)
        self.btn = ctk.CTkButton(f, text="", width=200, command=self.action)
        self.btn_visible = False
        app.after(200, self.refresh)

    def toggle_email(self):
        self.reveal_email = not self.reveal_email
        self.show(self.state)

    def refresh(self):
        self.app.in_thread(self.status_fn, self.show)

    def show(self, st):
        self.state = st
        if not st["installed"]:
            text, color, btn = f"{self.name} не установлен", RED, "Установить и войти"
        elif not st.get("loggedIn"):
            text, color, btn = f"{self.name} установлен, но вход не выполнен", "#e0a030", f"Войти в {self.account}"
        else:
            (text, color), btn = self.describe(st), "Войти другим аккаунтом"
        email = st.get("email") if st.get("loggedIn") else ""
        if email and not self.reveal_email:
            text = text.replace(email, "••••••••@••••")
        self.lbl.configure(text=text, text_color=color)
        if email:
            self.email_btn.configure(text="скрыть" if self.reveal_email else "показать")
            self.email_btn.pack(side="left", padx=(4, 0), after=self.lbl)
        else:
            self.email_btn.pack_forget()
        logged = st.get("loggedIn")
        self.btn.configure(text=btn, state="normal",
                           fg_color=GRAY if logged else BTN_COLOR,
                           hover_color=GRAY_HOVER if logged else BTN_HOVER)
        if not self.btn_visible:
            self.btn.pack(side="right")
            self.btn_visible = True

    def action(self):
        self.btn.configure(state="disabled")
        if not self.state["installed"]:
            self.lbl.configure(text=f"Скачиваю и устанавливаю {self.name}… (1–5 минут)", text_color="gray")
            log.info("Устанавливаю %s…", self.name)
            self.app.in_thread(self.install_fn, self.after_install)
        else:
            self.start_login()

    def after_install(self, res):
        ok, out = res
        if not ok:
            log.error("Установка %s не удалась:\n%s", self.name, out[-1500:])
            messagebox.showerror(self.name, f"Не получилось установить {self.name}.\n"
                                            "Подробности во вкладке «Журнал».")
            self.refresh()
            return
        log.info("%s установлен", self.name)
        self.start_login()

    def vpn_ok(self) -> bool:
        """Вход идёт из окна самой CLI — подсказку про VPN нужно дать до него, потом будет поздно."""
        sp = core.system_proxy()
        if sp and sp.startswith("http"):
            return True
        where = (f"Системный прокси {sp} — SOCKS, а {self.name} понимает только HTTP-прокси."
                 if sp else "Системный прокси не найден.")
        return messagebox.askokcancel(f"Вход в {self.account}", (
            f"{where}\n\n{self.account} недоступен в некоторых странах (например, в России), и вход оттуда "
            "без VPN не пройдёт — Google, OpenAI и Anthropic проверяют страну.\n\n"
            "Если вы в такой стране — нажмите «Отмена», включите VPN с сервером в США или Европе "
            "(режим TUN или системный HTTP-прокси) и повторите вход.\n\n"
            "Если VPN в режиме TUN уже включён или ваша страна поддерживается — нажмите «ОК».\n\n"
            "Нет VPN — выберите в «Нейросети» способ «Z.ai (бесплатно)»: он работает в России."))

    def start_login(self):
        relogin = bool(self.state.get("loggedIn"))
        if not self.vpn_ok():
            self.refresh()
            return
        try:
            self.login_fn()
        except Exception as e:
            messagebox.showerror(self.name, f"Не удалось открыть вход: {e}")
            self.refresh()
            return
        messagebox.showinfo(f"Вход в {self.account}",
                            self.relogin_hint if relogin and self.relogin_hint else self.hint)
        self.lbl.configure(text="Жду завершения входа… Когда войдёте — нажмите «Готово — проверить»",
                           text_color="gray")
        self.waiting += 1            # номер текущего ожидания: старые циклы проверки сами завершатся
        self.seen_logout = not relogin
        self.btn.configure(text="Готово — проверить", state="normal", command=self.finish_wait,
                           fg_color=BTN_COLOR, hover_color=BTN_HOVER)
        self.wait_login(self.waiting, tries=100)   # ~5 минут

    def finish_wait(self):
        self.waiting += 1            # останавливаем фоновое ожидание
        self.btn.configure(state="disabled", command=self.action)
        self.lbl.configure(text="Проверяю…", text_color="gray")

        def done(st):
            self.show(st)
            if st.get("loggedIn"):
                log.info("Вход в %s выполнен", self.account)
        self.app.in_thread(self.status_fn, done)

    def wait_login(self, wait_id, tries):
        def done(st):
            if wait_id != self.waiting:
                return               # ожидание уже закончено кнопкой или новым входом
            if not st.get("loggedIn"):
                self.seen_logout = True
            elif self.seen_logout or st != self.state:
                self.waiting += 1
                self.btn.configure(command=self.action)
                self.show(st)
                log.info("Вход в %s выполнен", self.account)
                return
            if tries > 0:
                self.app.after(3000, lambda: self.wait_login(wait_id, tries - 1))
            else:
                self.waiting += 1
                self.btn.configure(command=self.action)
                self.show(st)
        self.app.in_thread(self.status_fn, done)


class KeyBlock:
    """Поле API-ключа + «показать» + «Проверить ключ» + ссылка, где взять ключ."""

    def __init__(self, app: "App", parent, key_field, placeholder, link_text, url,
                 model_widget, model_field, check_fn, autoselect=False):
        """autoselect — после проверки выбрать первую модель из списка, если выбранной там нет."""
        self.app, self.key_field, self.model_field = app, key_field, model_field
        self.model_widget, self.check_fn, self.autoselect = model_widget, check_fn, autoselect
        ctk.CTkLabel(parent, text="API-ключ").grid(row=1, column=0, sticky="w", padx=14, pady=5)
        self.entry = e = ctk.CTkEntry(parent, show="•", textvariable=app.var(key_field))
        e.grid(row=1, column=1, sticky="ew", padx=4)
        app.fields.setdefault(key_field, e)
        kb = ctk.CTkFrame(parent, fg_color="transparent")
        kb.grid(row=1, column=2, sticky="w", padx=6)
        ctk.CTkButton(kb, text="показать", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: e.configure(show="" if e.cget("show") else "•")
                      ).pack(side="left", padx=2)
        self.btn = ctk.CTkButton(kb, text="Проверить ключ", width=130, command=self.check)
        self.btn.pack(side="left", padx=2)
        ctk.CTkButton(parent, text=link_text, fg_color="transparent",
                      text_color=("#1f6aa5", "#5aa9e6"), hover=False, anchor="w",
                      command=lambda: webbrowser.open(url)).grid(row=2, column=1, sticky="w")
        self.lbl = ctk.CTkLabel(parent, text="", text_color="gray")
        self.lbl.grid(row=2, column=2, sticky="w", padx=10)

    def check(self):
        cfg = dict(self.app.cfg, **{self.key_field: self.entry.get().strip(),
                                    self.model_field: self.model_widget.get().strip()})
        self.btn.configure(state="disabled")
        self.lbl.configure(text="Проверяю…", text_color="gray")

        def done(res):
            ok, msg, *models = res
            self.btn.configure(state="normal")
            self.lbl.configure(text=msg, text_color=GREEN if ok else RED)
            if models and models[0]:   # проверка вернула список доступных моделей
                self.model_widget.configure(values=models[0])
                if self.autoselect and self.model_widget.get().strip() not in models[0]:
                    self.model_widget.set(models[0][0])
        self.app.in_thread(lambda: self.check_fn(cfg), done)


# ======================================================================

# ======================================================================
#  Главное окно
# ======================================================================

class App(ctk.CTk):
    def __init__(self, hidden=False, run_delay: int | None = None, updated=False,
                 instance: socket.socket | None = None):
        """hidden — сразу в трей; run_delay — через сколько секунд самим подключить аккаунты
        (автозапуск с Windows, продолжение после обновления); updated — запуск после обновления."""
        super().__init__()
        if hidden:
            self.withdraw()   # до первой отрисовки — окно не мелькнёт
        icon = core.RES / "assets" / "icon.ico"
        if icon.exists():
            self.iconbitmap(str(icon))
        self.log_q = setup_logging()
        self.ui_q: queue.Queue = queue.Queue()   # задания для окна из фоновых потоков (call_soon)
        first_run = not core.CONFIG_FILE.exists()
        self.conf = core.load_config()            # не self.config: это имя метода tkinter
        resume = self.conf.pop("resume_profiles", None) or []   # записано перед автообновлением
        self.save()
        self.store = DraftStore()
        for d in self.store.list():   # генерацию прервали выходом — пусть человек решит, что делать
            if d.status == REVIEW and not d.text.strip() and not d.error:
                d.error = "Генерация прервалась (программу закрыли) — нажмите «↻ Повторить сейчас» вверху"
                d.retry_op = d.retry_op or "draft"
        self.engines: dict[str, Engine] = {p["id"]: Engine(p, self.store) for p in self.conf["profiles"]}
        self.update_show_names()
        self.vars: dict[str, ctk.StringVar] = {}
        self.fields: dict[str, ctk.CTkEntry] = {}
        self.login_dialog: LoginDialog | None = None
        self.login_waiting: list[str] = []   # профили, ждущие окна входа (оно одно на всех)
        self.selected: str | None = None     # id черновика в «Проверке»
        self.shown_text: dict[str, str] = {}  # что вставлено в поле текста (чтобы не затирать правки)
        self.text_did: str | None = None     # чей текст сейчас в поле
        self.review_view = REVIEW_VIEWS[0]
        self.published_count = 0
        self.status = "stopped"   # общий статус; нужен сразу — меню трея читает его из своего потока
        self._save_job = None
        self._pick_job = None
        self.text_edited: set[str] = set()   # черновики с ручными правками текста — запишем в журнал

        self.title(core.APP_TITLE)
        self.geometry("1180x800")
        self.minsize(980, 660)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        blank = Image.new("RGBA", (1, 1), (0, 0, 0, 0))   # «нет картинки» для подписей (см. show_image)
        self.blank_img = ctk.CTkImage(light_image=blank, dark_image=blank, size=(1, 1))
        self._build_header()
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._build_topics_tab(self.tabs.add("Темы"))
        self._build_review_tab(self.tabs.add("Проверка"))
        self._build_scheduled_tab(self.tabs.add("Запланировано"))
        self._build_published_tab(self.tabs.add("Опубликовано"))
        self._build_schedule_tab(self.tabs.add("Расписание"))
        self._build_prompt_tab(self.tabs.add("Промпты"))
        self._build_settings_tab(self.tabs.add("Настройки"))
        self._build_log_tab(self.tabs.add("Журнал"))
        self.tabs.configure(command=self.on_tab)
        self.update_channel_menus()
        for sf in (self.settings_sf, self.review_list, self.sched_list, self.pub_list, self.schedule_sf):
            repaint_after_scroll(sf)
        fix_hotkeys_any_layout(self)
        # Сообщения программы — здесь, а не системными окнами со звуком (см. Messages)
        self.notice = ctk.CTkFrame(self)
        self.notice_lbl = ctk.CTkLabel(self.notice, text="", anchor="w", justify="left", wraplength=1000)
        self.notice_lbl.pack(side="left", fill="x", expand=True, padx=12, pady=6)
        ctk.CTkButton(self.notice, text="✕", width=32, fg_color="transparent", hover_color=GRAY_HOVER,
                      text_color=("gray20", "gray80"), command=self.hide_notice).pack(side="right", padx=6)
        self.notice_timer = None
        messagebox.app = self
        self.statusbar = ctk.CTkLabel(self, text="", anchor="w", text_color="gray")
        self.statusbar.pack(fill="x", padx=16, pady=(0, 6))

        self.tray_hinted = False
        try:
            self.tray: Tray | None = Tray(self)
        except Exception as e:   # нет pystray или трей недоступен — крестик просто закрывает программу
            self.tray = None
            log.warning("Значок в трее недоступен: %s", e)
            self.tray_sw.configure(state="disabled")
            self.notify_sw.configure(state="disabled")
        self.render_status()
        self.render_review()
        if first_run:
            self.show_welcome()
        if instance:
            threading.Thread(target=self.listen_instances, args=(instance,), daemon=True).start()
        # Программу перенесли в другую папку или сменили Python — обновляем команду автозапуска
        if core.get_autostart() not in (None, core.autostart_command()):
            try:
                core.set_autostart(True)
            except OSError as e:
                log.warning("Не удалось обновить автозапуск: %s", e)
        if hidden and not self.tray:
            self.deiconify()
        if updated:
            log.info("Программа обновлена до версии %s", core.VERSION)
            if self.tray:
                self.tray.notify(f"Программа обновлена до версии {core.VERSION}")
        if run_delay is not None:
            log.info("Подключу аккаунты через %d сек", run_delay)
            only = set(resume) if updated and resume else None
            self.after(run_delay * 1000, lambda: self.auto_run(only))
        self.update_info: updater.Update | None = None
        self.updating = False
        self.after(15_000, self.check_updates)
        self.after(100, self.poll)
        self.after(30_000, self.tick_minute)

    def var(self, key: str) -> ctk.StringVar:
        """Общая переменная поля настроек: один ключ может стоять в двух блоках (текст и картинка)."""
        if key not in self.vars:
            self.vars[key] = ctk.StringVar(self)
        return self.vars[key]

    # ------------------------------------------------------------------ обновления

    UPDATE_EVERY_MS = 6 * 3600 * 1000

    def check_updates(self, manual=False):
        """Фоновая проверка GitHub Releases: при запуске и раз в 6 часов; manual — по кнопке."""
        if not manual:
            self.after(self.UPDATE_EVERY_MS, self.check_updates)
        if self.updating:
            return
        if manual:
            self.update_check_lbl.configure(text="Проверяю…", text_color="gray")

        def work():
            try:
                return updater.check(), None
            except Exception as e:
                return None, e

        def done(res):
            u, err = res
            if err:
                log.warning("Не удалось проверить обновления: %s", err)
                if manual:
                    self.update_check_lbl.configure(text="Не удалось проверить — нет связи с GitHub", text_color=RED)
                return
            if not u:
                if manual:
                    self.update_check_lbl.configure(text="У вас последняя версия ✓", text_color=GREEN)
                return
            if manual:
                self.update_check_lbl.configure(text=f"Доступна версия {u.version}", text_color=GREEN)
            fresh = not self.update_info or self.update_info.version != u.version
            self.update_info = u
            if fresh:
                log.info("Доступна новая версия %s", u.version)
            if core.FROZEN and u.url and self.conf["auto_update"] and not manual and self.can_update_quietly():
                self.start_update()
                return
            self.show_update_bar()
            if fresh and self.tray and self.state() == "withdrawn":
                self.tray.notify(f"Доступна версия {u.version} — откройте окно, чтобы обновить")
        self.in_thread(work, done)

    def can_update_quietly(self) -> bool:
        """Сами ставим обновление, только если ничего не прервём: ничего не генерируется, промпты сохранены,
        нет входа. Отложенные посты живут в Telegram — обновлению не мешают."""
        return (not any(d.busy for d in self.store.list()) and not self.prompts_dirty() and not self.login_dialog)

    def show_update_bar(self):
        u = self.update_info
        can_install = core.FROZEN and u.url
        self.update_lbl.configure(text=f"Доступна новая версия {u.version} (у вас {core.VERSION})")
        self.update_btn.configure(text="Обновить сейчас" if can_install else "Скачать",
                                  state="normal", command=self.start_update if can_install
                                  else lambda: webbrowser.open(u.page))
        self.update_bar.pack(fill="x", padx=12, pady=(0, 4), after=self.header_top)

    def start_update(self):
        u = self.update_info
        if not u or self.updating:
            return
        self.updating = True
        self.show_update_bar()
        self.update_btn.configure(state="disabled")
        self.update_lbl.configure(text=f"Скачиваю версию {u.version}…")
        log.info("Скачиваю обновление %s…", u.version)

        def progress(x):
            self.call_soon(lambda: self.update_lbl.configure(text=f"Скачиваю версию {u.version}… {int(x * 100)}%"))

        def work():
            try:
                return updater.download(u, progress), None
            except Exception as e:
                return None, e

        def done(res):
            setup, err = res
            if err:
                self.updating = False
                log.error("Не удалось скачать обновление: %s", err)
                self.update_lbl.configure(text=f"Не удалось скачать обновление: {err}")
                self.update_btn.configure(state="normal", text="Повторить")
                return
            running = [pid for pid, e in self.engines.items() if e.status != "stopped"]
            tray = self.state() == "withdrawn"
            log.info("Устанавливаю версию %s — программа перезапустится", u.version)
            self.conf["resume_profiles"] = running
            self.save()
            self.shutdown()
            try:
                updater.install(setup, run=bool(running), tray=tray)
            except Exception as e:   # антивирус удалил файл и т. п. — возвращаем всё, как было
                log.error("Не удалось запустить установщик обновления: %s", e)
                self.conf.pop("resume_profiles", None)
                self.save()
                self.restore_after_failed_update(running)
                self.update_lbl.configure(text=f"Не удалось установить обновление: {e}")
                return
            self.destroy()
        self.in_thread(work, done)

    def restore_after_failed_update(self, running: list[str]):
        self.updating = False
        try:
            self.tray = Tray(self)
        except Exception as e:
            self.tray = None
            log.warning("Значок в трее недоступен: %s", e)
        for pid in running:
            if pid in self.engines:
                self.engines[pid].start()
        self.show_update_bar()
        self.update_btn.configure(state="normal", text="Повторить")
        self.render_status()

    def shutdown(self):
        """Отключает все аккаунты и убирает значок — перед выходом и перед обновлением."""
        self.flush_text()
        stops = [e._submit(e._stop()) for e in self.engines.values()]
        for fut in stops:
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        self.store.save()
        if self.tray:
            self.tray.stop()

    def listen_instances(self, srv: socket.socket):
        """Повторный запуск программы: показываем это окно вместо второй копии."""
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(2)
                    if conn.recv(64) == INSTANCE_HELLO:
                        conn.sendall(INSTANCE_REPLY)
                        self.call_soon(self.show_window)
            except OSError:
                if srv.fileno() < 0:
                    return
                time.sleep(1)

    def auto_run(self, only: set[str] | None = None):
        """Подключение без участия человека (вход в Windows, после обновления): проблемы — в журнал."""
        targets = [p for p in self.conf["profiles"]
                   if (p["id"] in only if only else p["enabled"]) and self.engines[p["id"]].status == "stopped"]
        started = []
        for p in targets:
            problems = core.validate_telegram(p)
            if problems:
                log.warning("Автозапуск: «%s» не подключён — %s", p["name"], "; ".join(problems))
                continue
            self.engines[p["id"]].start()
            started.append(p["name"])
        if started:
            log.info("Автозапуск: подключаю — %s", ", ".join(started))

    # ------------------------------------------------------------------ профили

    @property
    def cfg(self) -> dict:
        """Настройки профиля, открытого в окне (тот же словарь, что у его движка)."""
        return self.profile(self.conf["current"])

    @property
    def engine(self) -> Engine:
        return self.engines[self.conf["current"]]

    def profile(self, pid: str) -> dict:
        return next(p for p in self.conf["profiles"] if p["id"] == pid)

    def save(self):
        core.save_config(self.conf)

    def update_show_names(self):
        for e in self.engines.values():
            e.show_name = len(self.engines) > 1

    @property
    def multi(self) -> bool:
        return len(self.engines) > 1

    def _build_profile_bar(self):
        pb = ctk.CTkFrame(self)
        pb.pack(fill="x", padx=12, pady=(4, 6))
        ctk.CTkLabel(pb, text="Аккаунт:").pack(side="left", padx=(12, 6), pady=8)
        self.profile_menu = ctk.CTkOptionMenu(pb, values=["—"], width=200, command=self.on_profile_menu)
        self.profile_menu.pack(side="left")
        self.profile_state = ctk.CTkLabel(pb, text="", text_color="gray")
        self.profile_state.pack(side="left", padx=12)
        self.del_btn = ctk.CTkButton(pb, text="Удалить", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                                     command=self.delete_profile)
        self.del_btn.pack(side="right", padx=(4, 10))
        ctk.CTkButton(pb, text="Переименовать", width=120, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.rename_profile).pack(side="right", padx=4)
        ctk.CTkButton(pb, text="+ Добавить аккаунт", width=150,
                      command=self.add_profile).pack(side="right", padx=4)
        self.profile_run_btn = ctk.CTkButton(pb, text="", width=150, command=self.toggle_profile)
        self.enabled_sw = ctk.CTkSwitch(pb, text="Запускать со всеми", command=self.on_enabled)

    def render_profile_bar(self):
        names = [p["name"] for p in self.conf["profiles"]]
        self.profile_menu.configure(values=names)
        self.profile_menu.set(self.cfg["name"])
        e = self.engine
        text, color = STATUS[e.status]
        if e.status == "running" and e.me_name:
            text += f" · {e.me_name}"
        self.profile_state.configure(text=f"● {text}", text_color=color)
        self.del_btn.configure(state="normal" if self.multi else "disabled")
        if self.multi:
            active = e.status != "stopped"
            self.profile_run_btn.configure(
                state="normal", text="■  Отключить этот" if active else "▶  Запустить только этот",
                fg_color=RED if active else GREEN, hover_color="#b94541" if active else GREEN_HOVER)
            self.enabled_sw.select() if self.cfg["enabled"] else self.enabled_sw.deselect()
            self.profile_run_btn.pack(side="left", padx=(0, 8), after=self.profile_state)
            self.enabled_sw.pack(side="left", after=self.profile_run_btn)
        else:
            self.profile_run_btn.pack_forget()
            self.enabled_sw.pack_forget()

    def leave_profile(self) -> bool:
        """Перед переключением: сохраняем форму настроек и спрашиваем про промпты."""
        if not self.apply_settings(silent=True) or not self.apply_schedule(silent=True):
            return False
        if self.prompts_dirty() and messagebox.askyesno(
                "Промпты", f"Промпты профиля «{self.cfg['name']}» изменены, но не сохранены. Сохранить?"):
            self.save_prompts()
        return True

    def on_profile_menu(self, name):
        pid = next(p["id"] for p in self.conf["profiles"] if p["name"] == name)
        if pid == self.conf["current"]:
            return
        if not self.leave_profile():
            self.profile_menu.set(self.cfg["name"])
            return
        self.switch_profile(pid)

    def switch_profile(self, pid):
        self.conf["current"] = pid
        self.save()
        self.load_settings_form()
        self.load_schedule_form()
        self.load_prompts()
        self.update_channel_menus()
        self.render_status()
        self.render_review()
        self.render_scheduled()
        self.render_published()

    def ask_name(self, title, text, initial="") -> str | None:
        d = ctk.CTkInputDialog(title=title, text=text)
        if initial:
            d.after(150, lambda: d._entry.insert(0, initial))
        name = (d.get_input() or "").strip()
        if not name:
            return None
        if any(p["name"] == name for p in self.conf["profiles"]):
            messagebox.showwarning(title, f"Профиль «{name}» уже есть — выберите другое название")
            return None
        return name

    def add_profile(self):
        if not self.leave_profile():
            return
        name = self.ask_name("Новый аккаунт", "Название профиля (например, имя канала):")
        if not name:
            return
        base = self.cfg
        p = core.new_profile(self.conf, base, name)
        self.engines[p["id"]] = Engine(p, self.store)
        self.update_show_names()
        self.switch_profile(p["id"])
        log.info("Добавлен профиль «%s»", name)
        messagebox.showinfo("Новый аккаунт", (
            f"Настройки, промпты и расписание скопированы из «{base['name']}» — поменяйте, что нужно. "
            "Затем нажмите «▶ Запустить только этот» — откроется вход в Telegram для нового аккаунта."))

    def rename_profile(self):
        name = self.ask_name("Переименовать", "Новое название профиля:", self.cfg["name"])
        if not name:
            return
        self.cfg["name"] = name
        self.save()
        self.render_profile_bar()
        self.render_review()

    def delete_profile(self):
        if len(self.engines) < 2:
            return
        p, e = self.cfg, self.engine
        n = len([d for d in self.store.list(profile=p["id"]) if d.status == REVIEW])
        if not messagebox.askyesno("Удалить профиль", f"Удалить профиль «{p['name']}»?"
                                   + (f"\n\nЧерновиков на проверке: {n} — они пропадут." if n else "")
                                   + "\n\nОтложенные посты в Telegram останутся и выйдут сами."):
            return
        e.close()
        session = BASE / f"{p['session_name']}.session"
        if session.exists() and messagebox.askyesno("Файл сессии", (
                f"Удалить и файл сессии {session.name}?\n\n"
                "Он даёт полный доступ к аккаунту Telegram. Если аккаунт больше не нужен — лучше удалить; "
                "чтобы вернуть его, придётся снова войти.")):
            for f in (session, session.with_name(session.name + "-journal")):
                f.unlink(missing_ok=True)
        self.store.remove_profile(p["id"])
        if self.login_dialog and self.login_dialog.engine is e:
            self.login_dialog.close()
        self.login_waiting = [x for x in self.login_waiting if x != p["id"]]
        self.conf["profiles"].remove(p)
        del self.engines[p["id"]]
        self.update_show_names()
        log.info("Профиль «%s» удалён", p["name"])
        self.switch_profile(self.conf["profiles"][0]["id"])

    def on_enabled(self):
        self.cfg["enabled"] = bool(self.enabled_sw.get())
        self.save()

    def toggle_profile(self):
        """Подключение/отключение только открытого профиля."""
        e = self.engine
        if e.status != "stopped":
            e.stop()
            return
        if not self.apply_settings(silent=True):
            return
        problems = core.validate_telegram(self.cfg)
        if problems:
            messagebox.showwarning("Не всё настроено", "Перед подключением:\n\n• " + "\n• ".join(problems))
            self.tabs.set("Настройки")
            return
        e.start()

    def next_login(self):
        """Окно входа одно — аккаунты, которым нужен вход, проходят его по очереди."""
        while self.login_waiting and not self.login_dialog:
            e = self.engines.get(self.login_waiting.pop(0))
            if e and e.status == "login":
                self.login_dialog = LoginDialog(self, e)

    # ------------------------------------------------------------------ шапка

    def _build_header(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 4))
        self.header_top = top
        ctk.CTkLabel(top, text=core.APP_TITLE, font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkLabel(top, text=f"v{core.VERSION}", text_color="gray").pack(side="left", padx=(6, 0), pady=(6, 0))

        self.update_bar = ctk.CTkFrame(self, fg_color=("#e3f0ff", "#1d2f44"))
        self.update_lbl = ctk.CTkLabel(self.update_bar, text="")
        self.update_lbl.pack(side="left", padx=12, pady=6)
        ctk.CTkButton(self.update_bar, text="Позже", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.update_bar.pack_forget).pack(side="right", padx=(4, 10))
        ctk.CTkButton(self.update_bar, text="Что нового", width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: webbrowser.open(self.update_info.page)).pack(side="right", padx=4)
        self.update_btn = ctk.CTkButton(self.update_bar, text="Обновить сейчас", width=150, fg_color=GREEN,
                                        hover_color=GREEN_HOVER)
        self.update_btn.pack(side="right", padx=4)
        self.status_dot = ctk.CTkLabel(top, text="●", font=ctk.CTkFont(size=18))
        self.status_dot.pack(side="left", padx=(18, 4))
        self.status_lbl = ctk.CTkLabel(top, text="")
        self.status_lbl.pack(side="left")
        self.start_btn = ctk.CTkButton(top, text="", width=150, height=36,
                                       font=ctk.CTkFont(size=14, weight="bold"), command=self.toggle_run)
        self.start_btn.pack(side="right")
        self.counter_lbl = ctk.CTkLabel(top, text="", text_color="gray")
        self.counter_lbl.pack(side="right", padx=14)
        self._build_profile_bar()

    def render_status(self):
        """Общий статус по всем аккаунтам: самый «требующий внимания» из их статусов."""
        engines = list(self.engines.values())
        states = {e.status for e in engines}
        st = next((s for s in ("login", "connecting", "running") if s in states), "stopped")
        text, color = STATUS[st]
        running = [e for e in engines if e.status == "running"]
        if st == "running" and len(engines) > 1:
            text = f"Подключено аккаунтов: {len(running)} из {len(engines)}"
        elif st == "running" and running[0].me_name:
            text = f"Подключён · {running[0].me_name}"
        elif st == "stopped":
            text = "Telegram не подключён — генерировать можно, публиковать нельзя"
        self.status_dot.configure(text_color=color)
        self.status_lbl.configure(text=text)
        active = st != "stopped"
        self.start_btn.configure(state="normal", text="■  Отключить" if active else "▶  Запустить",
                                 fg_color=RED if active else GREEN, hover_color="#b94541" if active else GREEN_HOVER)
        self.status = st
        self.render_profile_bar()
        self.update_tray()

    def update_tray(self):
        if not getattr(self, "tray", None):
            return
        n = len(self.store.list(REVIEW))
        tip = f"{core.APP_TITLE} — {self.status_lbl.cget('text')}" + (f" · на проверке: {n}" if n else "")
        self.tray.update(self.status, tip)

    def show_welcome(self):
        self.tabs.set("Настройки")
        self.welcome.pack(fill="x", padx=4, pady=(0, 10), before=self.settings_first)

    def toggle_run(self):
        """Общая кнопка: подключает все профили с «Запускать со всеми» или отключает все."""
        if self.status != "stopped":
            for e in self.engines.values():
                if e.status != "stopped":
                    e.stop()
            return
        if not self.apply_settings(silent=True):
            return
        targets = [p for p in self.conf["profiles"] if p["enabled"]]
        if not targets:
            messagebox.showwarning("Запуск", "Ни у одного аккаунта не включено «Запускать со всеми»")
            return
        for p in targets:
            problems = core.validate_telegram(p)
            if problems:
                if p["id"] != self.conf["current"]:
                    self.switch_profile(p["id"])
                where = f" аккаунта «{p['name']}»" if self.multi else ""
                messagebox.showwarning("Не всё настроено", f"Перед подключением{where}:\n\n• " + "\n• ".join(problems))
                self.tabs.set("Настройки")
                return
        for p in targets:
            if self.engines[p["id"]].status == "stopped":
                self.engines[p["id"]].start()

    def validate_async(self, cfg: dict, done):
        """Проверка нейросетей текста и картинки в фоне: CLI отвечают до десятков секунд."""
        snap = json.loads(json.dumps(cfg))
        self.in_thread(lambda: core.validate_text(snap) + core.validate_image(snap), done)

    # ------------------------------------------------------------------ темы

    def _build_topics_tab(self, tab):
        ctk.CTkLabel(tab, justify="left", anchor="w", text_color="gray", wraplength=1050, text=(
            "Впишите темы постов — одна строка, одна тема. По каждой нейросеть напишет текст по промпту текста "
            "и нарисует картинку по промпту картинки. Готовые черновики появятся во вкладке «Проверка»: там их "
            "можно поправить, перегенерировать, опубликовать сразу или поставить в отложенные по расписанию."
        )).pack(fill="x", pady=(0, 8))
        row = ctk.CTkFrame(tab, fg_color="transparent", height=1)   # пустая — не занимает места
        row.pack(fill="x", pady=(0, 6))
        self.topic_chan_lbl = ctk.CTkLabel(row, text="Канал:")
        self.topic_chan = ctk.CTkOptionMenu(row, values=["—"], width=240)
        self.topics_box = ctk.CTkTextbox(tab, wrap="word", font=ctk.CTkFont(size=14))
        self.topics_box.pack(fill="both", expand=True)
        self.topics_box.bind("<KeyRelease>", lambda e: self.update_topics_count())
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(8, 0))
        self.gen_btn = ctk.CTkButton(bottom, text="✨  Сгенерировать посты", height=42, width=260,
                                     font=ctk.CTkFont(size=15, weight="bold"), fg_color=GREEN,
                                     hover_color=GREEN_HOVER, command=self.generate_topics)
        self.gen_btn.pack(side="right")
        self.topics_count = ctk.CTkLabel(bottom, text="", text_color="gray")
        self.topics_count.pack(side="right", padx=12)
        self.topics_msg = ctk.CTkLabel(bottom, text="", text_color=RED, anchor="w", justify="left",
                                       wraplength=700)
        self.topics_msg.pack(side="left")

    def chan_labels(self, cfg=None) -> dict[str, str]:
        """{подпись в окне: запись канала} для профиля."""
        cfg = cfg or self.cfg
        e = self.engines[cfg["id"]]
        return {e.channel_title(c): c for c in core.channels(cfg)}

    def update_channel_menus(self):
        labels = list(self.chan_labels()) or ["— укажите канал в «Настройках» —"]
        for menu in (self.topic_chan, self.preview_chan):
            menu.configure(values=labels)
            if menu.get() not in labels:
                menu.set(labels[0])
        if len(labels) > 1:
            self.topic_chan_lbl.pack(side="left", padx=(0, 6))
            self.topic_chan.pack(side="left")
        else:
            self.topic_chan_lbl.pack_forget()
            self.topic_chan.pack_forget()
        self.update_topics_count()

    def topics(self) -> list[str]:
        return list(dict.fromkeys(t.strip() for t in self.topics_box.get("1.0", "end").splitlines() if t.strip()))

    def update_topics_count(self):
        n = len(self.topics())
        self.topics_count.configure(text=f"Тем: {n}" if n else "")

    def generate_topics(self):
        topics = self.topics()
        if not topics:
            self.topics_msg.configure(text="Впишите хотя бы одну тему")
            return
        if not core.channels(self.cfg):
            self.topics_msg.configure(text="Сначала укажите канал — «Настройки» → «Telegram»")
            return
        if not self.apply_settings(silent=True):
            return
        if self.prompts_dirty() and messagebox.askyesno("Промпты", "Промпты изменены, но не сохранены. "
                                                                   "Сохранить перед генерацией?"):
            self.save_prompts()
        cfg, engine = self.cfg, self.engine
        channel = self.chan_labels().get(self.topic_chan.get()) or core.channels(cfg)[0]
        self.gen_btn.configure(state="disabled", text="Проверяю нейросети…")
        self.topics_msg.configure(text="")

        def done(problems):
            self.gen_btn.configure(state="normal", text="✨  Сгенерировать посты")
            if problems:
                self.topics_msg.configure(text="Не всё настроено:\n• " + "\n• ".join(problems))
                return
            first = None
            for t in topics:
                d = self.store.new(cfg["id"], t, channel)
                first = first or d.id
                engine.generate(d.id)
            names = ", ".join(f"«{short(t, 40)}»" for t in topics[:5]) + (f" и ещё {len(topics) - 5}"
                                                                          if len(topics) > 5 else "")
            engine.log.info("Новые темы (%d) для %s: %s", len(topics), engine.channel_title(channel), names)
            self.topics_box.delete("1.0", "end")
            self.update_topics_count()
            self.review_view = REVIEW_VIEWS[0]
            self.selected = first
            self.tabs.set("Проверка")
            self.render_review()
        self.validate_async(cfg, done)

    # ------------------------------------------------------------------ проверка

    def _build_review_tab(self, tab):
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.view_seg = ctk.CTkSegmentedButton(top, values=list(REVIEW_VIEWS), command=self.on_review_view)
        self.view_seg.set(self.review_view)
        self.view_seg.pack(side="left")
        self.bulk_btn = ctk.CTkButton(top, text="🕒  Все готовые — в расписание", width=250,
                                      command=self.schedule_all)
        self.bulk_btn.pack(side="right")
        self.retry_btn = ctk.CTkButton(top, text="↻  Повторить сейчас", width=200, command=self.retry_all,
                                       fg_color="#c07a1a", hover_color="#a0640f")

        self.review_list = ctk.CTkScrollableFrame(tab, width=270, label_text="")
        self.review_list.grid(row=1, column=0, sticky="ns", padx=(0, 10))
        self.review_btns: dict[str, ctk.CTkButton] = {}
        self.review_empty = ctk.CTkLabel(tab, text="", font=ctk.CTkFont(size=15), text_color="gray",
                                         justify="center")

        d = self.detail = ctk.CTkFrame(tab, fg_color="transparent")
        d.grid_columnconfigure(0, weight=1)
        d.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(d, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        self.d_topic = ctk.CTkLabel(head, text="", anchor="w", justify="left", wraplength=640,
                                    font=ctk.CTkFont(size=15, weight="bold"))
        self.d_topic.grid(row=0, column=0, sticky="w")
        self.d_chan = ctk.CTkOptionMenu(head, values=["—"], width=220, command=self.on_draft_channel)
        self.d_chan.grid(row=0, column=1, padx=(8, 0))

        cnt = ctk.CTkFrame(d, fg_color="transparent")
        cnt.grid(row=1, column=0, sticky="ew", pady=(6, 2))
        ctk.CTkLabel(cnt, text="Текст поста (можно править)", font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.d_count = ctk.CTkLabel(cnt, text="", text_color="gray")
        self.d_count.pack(side="right")
        self.text_box = ctk.CTkTextbox(d, wrap="word", font=ctk.CTkFont(size=14))
        self.text_box.grid(row=2, column=0, sticky="nsew")
        self.text_box.bind("<KeyRelease>", lambda e: self.on_text_edit())

        tw = ctk.CTkFrame(d, fg_color="transparent")
        tw.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        tw.grid_columnconfigure(0, weight=1)
        self.text_wish = ctk.CTkEntry(tw, placeholder_text="Пожелание к тексту: короче, веселее, добавь пример… "
                                                           "(необязательно)")
        self.text_wish.grid(row=0, column=0, sticky="ew")
        self.text_wish.bind("<Return>", lambda e: self.regen_text(edit=True))
        self.edit_text_btn = ctk.CTkButton(tw, text="✎  Поправить", width=120,
                                           command=lambda: self.regen_text(edit=True))
        self.edit_text_btn.grid(row=0, column=1, padx=(6, 0))
        self.new_text_btn = ctk.CTkButton(tw, text="↻  Написать заново", width=150, fg_color=GRAY,
                                          hover_color=GRAY_HOVER, command=lambda: self.regen_text(edit=False))
        self.new_text_btn.grid(row=0, column=2, padx=(6, 0))

        ic = ctk.CTkFrame(d)
        ic.grid(row=1, column=1, rowspan=3, sticky="ns", padx=(10, 0), pady=(6, 0))
        ctk.CTkLabel(ic, text="Картинка", font=ctk.CTkFont(weight="bold")).pack(pady=(8, 4))
        self.img_preview = ctk.CTkLabel(ic, text="", width=IMG_BOX, height=IMG_BOX,
                                        fg_color=("gray85", "gray20"), corner_radius=8)
        self.img_preview.pack(padx=10)
        self.img_preview.bind("<Double-Button-1>", lambda e: self.open_image())
        self.img_info = ctk.CTkLabel(ic, text="", text_color="gray", wraplength=IMG_BOX)
        self.img_info.pack(pady=2)
        nav = ctk.CTkFrame(ic, fg_color="transparent")
        nav.pack(pady=(0, 4))
        self.img_nav = [
            ctk.CTkButton(nav, text="◀", width=40, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=lambda: self.step_image(-1)),
            ctk.CTkButton(nav, text="▶", width=40, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=lambda: self.step_image(1)),
            ctk.CTkButton(nav, text="Своя…", width=80, command=self.choose_image),
            ctk.CTkButton(nav, text="Без картинки", width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=self.no_image),
        ]
        for b in self.img_nav:
            b.pack(side="left", padx=2)
        self.img_wish = ctk.CTkEntry(ic, placeholder_text="Пожелание к картинке: светлее, без людей…")
        self.img_wish.pack(fill="x", padx=10, pady=(4, 4))
        self.img_wish.bind("<Return>", lambda e: self.regen_image(edit=False))
        ib = ctk.CTkFrame(ic, fg_color="transparent")
        ib.pack(padx=10, pady=(0, 10), fill="x")
        self.new_img_btn = ctk.CTkButton(ib, text="↻  Нарисовать заново", command=lambda: self.regen_image(edit=False))
        self.new_img_btn.pack(side="left", expand=True, fill="x", padx=(0, 3))
        self.edit_img_btn = ctk.CTkButton(ib, text="✎  Поправить эту", fg_color=GRAY, hover_color=GRAY_HOVER,
                                          command=lambda: self.regen_image(edit=True))
        self.edit_img_btn.pack(side="left", expand=True, fill="x", padx=(3, 0))

        self.d_progress = ctk.CTkProgressBar(d, mode="indeterminate", height=6)
        self.d_msg = ctk.CTkLabel(d, text="", text_color=RED, anchor="w", justify="left", wraplength=900)
        self.d_msg.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        d.bind("<Configure>", lambda ev: self.d_msg.configure(wraplength=max(300, ev.width - 30)), add="+")

        act = self.actions = ctk.CTkFrame(d, fg_color="transparent")
        act.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.pub_now_btn = ctk.CTkButton(act, text="✓  Опубликовать сейчас", height=42, width=200,
                                         font=ctk.CTkFont(size=14, weight="bold"), fg_color=GREEN,
                                         hover_color=GREEN_HOVER, command=self.publish_now)
        self.pub_slot_btn = ctk.CTkButton(act, text="🕒  В расписание", height=42, width=250,
                                          font=ctk.CTkFont(size=14, weight="bold"), command=self.publish_slot)
        self.when_entry = ctk.CTkEntry(act, width=150, height=42, placeholder_text="19:30 или 07.11 19:30")
        self.when_entry.bind("<Return>", lambda e: self.publish_at_time())
        self.pub_at_btn = ctk.CTkButton(act, text="На это время", height=42, width=120, fg_color=GRAY,
                                        hover_color=GRAY_HOVER, command=self.publish_at_time)
        self.cancel_btn = ctk.CTkButton(act, text="Отменить", height=42, width=110, fg_color=GRAY,
                                        hover_color=GRAY_HOVER, command=self.cancel_draft)
        self.restore_btn = ctk.CTkButton(act, text="↩  Вернуть на проверку", height=42, width=220,
                                         command=self.restore_draft)
        self.delete_btn = ctk.CTkButton(act, text="Удалить навсегда", height=42, width=160, fg_color=RED,
                                        hover_color="#b94541", command=self.delete_draft)
        self.reserved: dict[str, tuple[str, datetime]] = {}   # черновик → занятый им слот, пока идёт отправка

    def on_review_view(self, value):
        self.flush_text()
        # На кнопках — с количеством: «На проверке (2)»
        self.review_view = next((v for v in REVIEW_VIEWS if value.startswith(v)), REVIEW_VIEWS[0])
        self.selected = None
        self.render_review()

    def review_drafts(self) -> list[Draft]:
        status = REVIEW if self.review_view == REVIEW_VIEWS[0] else CANCELLED
        return sorted(self.store.list(status), key=lambda d: d.created)

    def current(self) -> Draft | None:
        d = self.store.get(self.selected) if self.selected else None
        status = REVIEW if self.review_view == REVIEW_VIEWS[0] else CANCELLED
        return d if d and d.status == status else None

    def owner(self, d: Draft) -> Engine | None:
        return self.engines.get(d.profile)

    def select(self, did):
        self.flush_text()
        self.selected = did
        self.render_review()

    def draft_state(self, d: Draft) -> str:
        if d.text_busy:
            return "⏳ пишу текст…"
        if d.image_busy:
            return "🎨 рисую картинку…"
        if d.waiting:
            at = d.wait_time()
            return "⏸ лимит" + (f" · продолжу {sched.fmt(at)}" if at else "")
        if d.error:
            return "⚠ " + ("ошибка" if not d.text.strip() else "есть замечание")
        if d.status == CANCELLED:
            return "✕ отменён"
        return "✓ готов"

    def list_text(self, d: Draft) -> str:
        e = self.owner(d)
        who = f"{e.cfg['name']} · " if e and self.multi else ""
        chan = f" · {e.channel_title(d.channel)}" if e and len(core.channels(e.cfg)) > 1 else ""
        return f"{who}{self.draft_state(d)}{chan}\n{d.topic[:70]}"

    def render_review(self):
        drafts = self.review_drafts()
        n_review, n_cancel = len(self.store.list(REVIEW)), len(self.store.list(CANCELLED))
        self.view_seg.configure(values=[f"{REVIEW_VIEWS[0]} ({n_review})", f"{REVIEW_VIEWS[1]} ({n_cancel})"])
        self.view_seg.set(f"{self.review_view} ({n_review if self.review_view == REVIEW_VIEWS[0] else n_cancel})")
        ids = [d.id for d in drafts]
        if self.selected not in ids:
            self.selected = ids[0] if ids else None
        for w in self.review_list.winfo_children():
            w.destroy()
        self.review_btns = {}
        for d in drafts:
            sel = d.id == self.selected
            b = self.review_btns[d.id] = ctk.CTkButton(
                self.review_list, text=self.list_text(d), anchor="w", height=58, corner_radius=8,
                fg_color=BTN_COLOR if sel else ("gray80", "gray25"),
                text_color=("white", "white") if sel else ("gray10", "gray90"),
                command=lambda k=d.id: self.select(k))
            b.pack(fill="x", pady=3)
            b._text_label.configure(wraplength=235, justify="left")
        self.bulk_btn.configure(state="normal" if self.review_view == REVIEW_VIEWS[0] and drafts else "disabled")
        self.update_retry_btn()
        self.title(f"({n_review}) {core.APP_TITLE}" if n_review else core.APP_TITLE)
        self.update_tray()
        d = self.current()
        if not d:
            self.detail.grid_forget()
            if self.review_view == REVIEW_VIEWS[1]:
                msg = "Отменённых черновиков нет."
            else:
                msg = ("Черновиков на проверке нет.\n\nВпишите темы во вкладке «Темы» и нажмите\n"
                       "«Сгенерировать посты» — готовые черновики появятся здесь.")
            self.review_empty.configure(text=msg)
            self.review_empty.grid(row=1, column=1, sticky="nsew")
            return
        self.review_empty.grid_forget()
        self.detail.grid(row=1, column=1, sticky="nsew")
        self.show_draft(d)

    def show_draft(self, d: Draft):
        e = self.owner(d)
        cfg = e.cfg
        who = f"  ·  {cfg['name']}" if self.multi else ""
        self.d_topic.configure(text=f"{d.topic}{who}")
        labels = self.chan_labels(cfg)
        self.d_chan.configure(values=list(labels) or ["—"])
        self.d_chan.set(e.channel_title(d.channel))
        if len(labels) > 1 and d.status == REVIEW:
            self.d_chan.grid()
        else:
            self.d_chan.grid_remove()
        # Не затираем правки: текст в поле меняем, только если он новый (сгенерирован) или это другой черновик
        if self.text_did != d.id or self.shown_text.get(d.id) != d.text:
            self.text_box.configure(state="normal")
            self.text_box.delete("1.0", "end")
            self.text_box.insert("1.0", d.text)
            self.shown_text[d.id] = d.text
            self.text_did = d.id
        self.update_count(d)
        self.show_image(d)
        busy = d.busy
        if busy:
            self.d_progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            self.d_progress.start()
            what = []
            if d.text_busy:
                what.append(f"{e.text_gen.name} пишет текст")
            if d.image_busy:
                what.append(f"{e.image_gen.name} рисует картинку")
            self.d_msg.configure(text="⏳ " + " · ".join(what) + "… (обычно от 10 секунд до пары минут)",
                                 text_color="gray")
        else:
            self.d_progress.stop()
            self.d_progress.grid_forget()
            problem = e.publish_problem(d) if d.status == REVIEW and d.text.strip() else ""
            msg = "\n".join(x for x in (d.error, problem) if x)
            if not msg and d.status == REVIEW and e.status != "running":
                msg, color = "Telegram не подключён — опубликовать можно после «▶ Запустить»", ORANGE
            else:
                color = ORANGE if d.waiting else RED
            self.d_msg.configure(text=msg, text_color=color)
        review = d.status == REVIEW
        st = "disabled" if d.text_busy or not review else "normal"
        for w in (self.edit_text_btn, self.new_text_btn, self.text_wish):
            w.configure(state=st)
        self.text_box.configure(state=st)
        ist = "disabled" if d.image_busy or not review else "normal"
        for w in (*self.img_nav, self.img_wish, self.new_img_btn):
            w.configure(state=ist)
        self.edit_img_btn.configure(state=ist if e.image_gen.can_edit and d.image_path else "disabled")
        folder = e.image_gen.backend == "folder"
        can_new = (e.image_gen.generates or folder) and ist == "normal"
        self.new_img_btn.configure(text="↻  Другая из папки" if folder else "↻  Нарисовать заново",
                                   state="normal" if can_new else "disabled",
                                   fg_color=BTN_COLOR if can_new else GRAY)
        self.edit_img_btn.configure(fg_color=GRAY)
        if len(d.images) < 2:
            for b in self.img_nav[:2]:
                b.configure(state="disabled")
        for w in self.actions.winfo_children():
            w.pack_forget()
        if review:
            for w in (self.pub_now_btn, self.pub_slot_btn, self.when_entry, self.pub_at_btn):
                w.pack(side="left", padx=(0, 6))
            self.cancel_btn.pack(side="right")
            ok = not busy and e.status == "running" and not e.publish_problem(d) and bool(d.text.strip() or d.image)
            for w, color, hover in ((self.pub_now_btn, GREEN, GREEN_HOVER), (self.pub_slot_btn, BTN_COLOR, BTN_HOVER),
                                    (self.pub_at_btn, BTN_COLOR, BTN_HOVER)):
                w.configure(state="normal" if ok else "disabled", fg_color=color if ok else GRAY,
                            hover_color=hover if ok else GRAY_HOVER)
            self.cancel_btn.configure(state="disabled" if busy else "normal")
            slot = self.next_slot(d)
            self.pub_slot_btn.configure(text=f"🕒  В расписание: {sched.fmt(slot)}" if slot
                                        else "🕒  В расписании нет времени")
            if not slot:
                self.pub_slot_btn.configure(state="disabled", fg_color=GRAY)
        else:
            self.restore_btn.pack(side="left")
            self.delete_btn.pack(side="right")

    def update_count(self, d: Draft):
        e = self.owner(d)
        cfg = e.cfg
        n = core.post_length(d.text, cfg)
        limit = int(cfg.get("post_max_len", 900))
        cap = e.caption_limit()
        parts = [f"{n} знаков" + (" с подписью" if str(cfg.get("post_signature", "")).strip() else ""),
                 f"цель — до {limit}"]
        color = "gray"
        if d.image_path and n > cap:
            parts.append(f"в подпись к картинке ({cap}) не помещается — "
                         + ("уйдёт отдельным сообщением" if cfg.get("long_text") == "split" else "сократите"))
            color = "#e0a030" if cfg.get("long_text") == "split" else RED
        elif n > limit:
            color = "#e0a030"
        self.d_count.configure(text="  ·  ".join(parts), text_color=color)

    def on_text_edit(self):
        d = self.current()
        if not d or self.text_did != d.id or d.text_busy:
            return
        text = self.text_box.get("1.0", "end-1c")
        if text == d.text:   # стрелки, выделение, копирование — не правка
            return
        d.text = text
        self.shown_text[d.id] = d.text
        self.text_edited.add(d.id)
        self.update_count(d)
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(1000, self.store.save)

    def flush_text(self):
        """Правки из поля — в черновик и на диск (перед переключением, публикацией, выходом)."""
        d = self.store.get(self.text_did) if self.text_did else None
        if d and not d.text_busy and d.status == REVIEW and str(self.text_box.cget("state")) == "normal":
            text = self.text_box.get("1.0", "end-1c")
            if text != d.text:
                d.text = text
                self.shown_text[d.id] = text
                self.text_edited.add(d.id)
        for did in list(self.text_edited):
            x = self.store.get(did)
            if x and self.owner(x):
                self.owner(x).log.info("✎ «%s»: текст поправлен вручную (%d знаков)", short(x.topic, 60),
                                       core.post_length(x.text, self.owner(x).cfg))
        self.text_edited.clear()
        if self._save_job:
            self.after_cancel(self._save_job)
            self._save_job = None
        self.store.save()

    def show_image(self, d: Draft):
        path = d.image_path
        img = None
        if path:
            try:
                pic = Image.open(path)
                pic.thumbnail((IMG_BOX, IMG_BOX))
                img = ctk.CTkImage(light_image=pic, dark_image=pic, size=pic.size)
            except Exception:
                img = None
        self._img_ref = img
        if img:
            self.img_preview.configure(image=img, text="")
        else:
            # Не image=None: у customtkinter после этого следующая картинка падает — ставим заглушку
            self.img_preview.configure(image=self.blank_img,
                                       text="рисую…" if d.image_busy else "без картинки")
        info = []
        if path and d.image in d.images and len(d.images) > 1:
            info.append(f"вариант {d.images.index(d.image) + 1} из {len(d.images)}")
        if path and d.image_source:
            info.append(d.image_source)
        if path:
            info.append("двойной щелчок — открыть")
        self.img_info.configure(text="  ·  ".join(info))

    def open_image(self):
        d = self.current()
        if d and d.image_path:
            open_path(d.image_path)

    def step_image(self, delta: int):
        d = self.current()
        if not d or len(d.images) < 2:
            return
        i = d.images.index(d.image) if d.image in d.images else len(d.images) - 1
        d.image = d.images[(i + delta) % len(d.images)]
        self.store.save()
        # Листание — частое действие: запишем выбор, когда человек остановится
        if self._pick_job:
            self.after_cancel(self._pick_job)
        self._pick_job = self.after(1500, lambda: self.log_image_pick(d))
        self.show_draft(d)

    def log_image_pick(self, d: Draft):
        self._pick_job = None
        if d.image in d.images and self.owner(d):
            self.owner(d).log.info("🖼 «%s»: выбран вариант картинки %d из %d", short(d.topic, 60),
                                   d.images.index(d.image) + 1, len(d.images))

    def choose_image(self):
        d = self.current()
        if not d:
            return
        f = filedialog.askopenfilename(title="Картинка к посту",
                                       filetypes=[("Картинки", " ".join("*" + e for e in core.IMAGE_EXT))])
        if not f:
            return
        d.folder.mkdir(parents=True, exist_ok=True)
        dst = d.folder / f"own_{len(d.images) + 1}{Path(f).suffix.lower()}"
        shutil.copy(f, dst)
        d.images.append(str(dst))
        d.image, d.image_source = str(dst), "своя"
        if d.retry_op == "image":   # ждали картинку от нейросети — своя её заменила
            d.clear_retry()
            d.error = ""
        self.owner(d).log.info("🖼 «%s»: поставлена своя картинка %s (вариант %d)", short(d.topic, 60),
                               Path(f).name, len(d.images))
        self.store.save()
        self.show_draft(d)

    def no_image(self):
        d = self.current()
        if d:
            d.image = ""
            if d.retry_op == "image":
                d.clear_retry()
                d.error = ""
            self.owner(d).log.info("🖼 «%s»: пост без картинки", short(d.topic, 60))
            self.store.save()
            self.show_draft(d)

    def regen_text(self, edit: bool):
        d = self.current()
        if not d or d.text_busy:
            return
        wish = self.text_wish.get().strip()
        if edit and not wish:
            self.d_msg.configure(text="Напишите в поле, что поправить в тексте, — или нажмите «Написать заново»",
                                 text_color=RED)
            return
        self.flush_text()
        self.owner(d).regen_text(d.id, wish, edit=edit and bool(d.text.strip()))
        self.text_wish.delete(0, "end")

    def regen_image(self, edit: bool):
        d = self.current()
        if not d or d.image_busy:
            return
        wish = self.img_wish.get().strip()
        if edit and not wish:
            self.d_msg.configure(text="Напишите в поле под картинкой, что в ней поправить", text_color=RED)
            return
        self.flush_text()
        self.owner(d).regen_image(d.id, wish, edit=edit)
        self.img_wish.delete(0, "end")

    def on_draft_channel(self, label):
        d = self.current()
        if not d:
            return
        name = self.chan_labels(self.owner(d).cfg).get(label)
        if name and name != d.channel:
            d.channel = name
            self.owner(d).log.info("«%s»: канал — %s", short(d.topic, 60), self.owner(d).channel_title(name))
            self.store.save()
            self.show_draft(d)

    # --- публикация из «Проверки» ---

    def next_slot(self, d: Draft, jitter=False) -> datetime | None:
        e = self.owner(d)
        taken = e.taken_times(d.channel) + [t for did, (ch, t) in self.reserved.items()
                                             if ch == d.channel and did != d.id]
        found = sched.next_free(e.cfg["schedule"], taken, n=1, jitter=jitter)
        return found[0] if found else None

    def ready(self, d: Draft) -> bool:
        if not d or d.busy or d.status != REVIEW:
            return False
        self.flush_text()
        e = self.owner(d)
        if e.status != "running":
            messagebox.showwarning("Публикация", f"Аккаунт «{e.cfg['name']}» не подключён — нажмите «▶ Запустить»")
            return False
        problem = e.publish_problem(d)
        if problem:
            messagebox.showwarning("Публикация", problem)
            return False
        return True

    def publish_now(self):
        d = self.current()
        if self.ready(d):
            self.owner(d).publish(d.id, None)
            self.d_msg.configure(text="Публикую…", text_color="gray")

    def publish_slot(self):
        d = self.current()
        if not self.ready(d):
            return
        at = self.next_slot(d, jitter=True)
        if not at:
            messagebox.showwarning("Расписание", "В расписании нет ни одного времени — заполните вкладку «Расписание»")
            return
        self.reserved[d.id] = (d.channel, at)
        self.owner(d).publish(d.id, at)
        self.d_msg.configure(text=f"Ставлю в отложенные на {sched.fmt(at)}…", text_color="gray")

    def publish_at_time(self):
        d = self.current()
        if not d:
            return
        at = sched.parse_when(self.when_entry.get())
        if not at:
            self.d_msg.configure(text="Время: «19:30», «завтра 10:00» или «07.11 19:30»", text_color=RED)
            return
        if at <= datetime.now().astimezone() + sched.MIN_LEAD:
            self.d_msg.configure(text="Это время уже прошло или слишком близко — для «сейчас» есть своя кнопка",
                                 text_color=RED)
            return
        if not self.ready(d):
            return
        self.when_entry.delete(0, "end")
        self.reserved[d.id] = (d.channel, at)
        self.owner(d).publish(d.id, at)
        self.d_msg.configure(text=f"Ставлю в отложенные на {sched.fmt(at)}…", text_color="gray")

    def schedule_all(self):
        """Все готовые черновики подключённых аккаунтов — по ближайшим свободным слотам."""
        self.flush_text()
        groups: dict[tuple[str, str], list[Draft]] = {}
        skipped = 0
        for d in self.review_drafts():
            e = self.owner(d)
            if (d.busy or e.status != "running" or not (d.text.strip() or d.image_path)
                    or e.publish_problem(d)):
                skipped += 1
                continue
            groups.setdefault((d.profile, d.channel), []).append(d)
        if not groups:
            messagebox.showwarning("Расписание", "Нет готовых черновиков, которые можно поставить: проверьте, "
                                                 "что аккаунт подключён и генерация закончилась")
            return
        plan = []
        for (pid, channel), items in groups.items():
            e = self.engines[pid]
            taken = e.taken_times(channel) + [t for ch, t in self.reserved.values() if ch == channel]
            slots = sched.next_free(e.cfg["schedule"], taken, n=len(items))
            if len(slots) < len(items):
                messagebox.showwarning("Расписание", f"В расписании «{e.cfg['name']}» не хватает свободного "
                                                     "времени на все посты — добавьте слоты")
                return
            plan += list(zip(items, slots))
        plan.sort(key=lambda x: x[1])
        text = (f"Поставить в отложенные постов: {len(plan)}\n"
                f"с {sched.fmt(plan[0][1])} по {sched.fmt(plan[-1][1])}?"
                + (f"\n\nНе готовы и останутся на проверке: {skipped}" if skipped else ""))
        if not messagebox.askyesno("Расписание", text):
            return
        for d, at in plan:
            self.reserved[d.id] = (d.channel, at)
            self.owner(d).publish(d.id, at)
        log.info("🕒 Все готовые — в расписание: %d %s, с %s по %s", len(plan),
                 "пост" if len(plan) == 1 else "поста" if len(plan) < 5 else "постов",
                 sched.fmt(plan[0][1]), sched.fmt(plan[-1][1]))

    def retry_items(self) -> list[Draft]:
        return [d for d in self.store.list(REVIEW) if d.retry_op and not d.busy]

    def update_retry_btn(self):
        n = len(self.retry_items())
        if n and self.review_view == REVIEW_VIEWS[0]:
            self.retry_btn.configure(text=f"↻  Повторить сейчас ({n})")
            if not self.retry_btn.winfo_ismapped():
                self.retry_btn.pack(side="right", padx=(0, 8))
        elif self.retry_btn.winfo_ismapped():
            self.retry_btn.pack_forget()

    def retry_all(self):
        self.flush_text()
        items = self.retry_items()
        if not items:
            return
        for pid in {d.profile for d in items}:
            if pid in self.engines:
                self.engines[pid].retry_all()

    def cancel_draft(self):
        d = self.current()
        if d:
            self.flush_text()
            self.owner(d).cancel(d.id)

    def restore_draft(self):
        d = self.current()
        if d:
            self.owner(d).restore(d.id)
            self.review_view = REVIEW_VIEWS[0]
            self.selected = d.id

    def delete_draft(self):
        d = self.current()
        if d and messagebox.askyesno("Удалить", f"Удалить черновик «{d.topic[:60]}» вместе с картинками?"):
            if self.owner(d):
                self.owner(d).log.info("🗑 «%s»: черновик удалён", short(d.topic, 60))
            self.store.delete(d.id)
            self.selected = None
            self.render_review()

    # ------------------------------------------------------------------ запланировано

    def _build_scheduled_tab(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(top, justify="left", anchor="w", text_color="gray", wraplength=780, text=(
            "Отложенные посты каналов — прямо из Telegram: и поставленные программой, и добавленные вручную. "
            "Они выйдут сами, даже если компьютер выключен.")).pack(side="left")
        ctk.CTkButton(top, text="⟳  Обновить", width=110, command=self.refresh_scheduled).pack(side="right")
        self.sched_info = ctk.CTkLabel(top, text="", text_color="gray")
        self.sched_info.pack(side="right", padx=10)
        self.sched_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.sched_list.pack(fill="both", expand=True)

    def refresh_scheduled(self):
        for e in self.engines.values():
            if e.status == "running":
                e.refresh_scheduled()
        self.sched_info.configure(text="Сверяю с Telegram…")

    def scheduled_items(self) -> list[tuple[Engine, dict]]:
        items = [(e, it) for e in self.engines.values() if e.status == "running"
                 for ch in core.channels(e.cfg) for it in e.scheduled.get(ch, [])]
        return sorted(items, key=lambda x: x[1]["date"])

    def render_scheduled(self):
        for w in self.sched_list.winfo_children():
            w.destroy()
        running = [e for e in self.engines.values() if e.status == "running"]
        if not running:
            ctk.CTkLabel(self.sched_list, text="Подключите аккаунт («▶ Запустить») — тогда здесь будут "
                                               "отложенные посты его каналов.", text_color="gray",
                         font=ctk.CTkFont(size=14)).pack(pady=40)
            self.sched_info.configure(text="")
            return
        times = [e.scheduled_at for e in running if e.scheduled_at]
        self.sched_info.configure(text=f"сверено в {max(times):%H:%M}" if times else "")
        items = self.scheduled_items()
        if not items:
            ctk.CTkLabel(self.sched_list, text="Отложенных постов нет.", text_color="gray",
                         font=ctk.CTkFont(size=14)).pack(pady=40)
            return
        for e, it in items:
            self._sched_row(e, it)

    def _sched_row(self, e: Engine, it: dict):
        d = self.store.get(it["draft"]) if it["draft"] else None
        row = ctk.CTkFrame(self.sched_list)
        row.pack(fill="x", pady=3, padx=2)
        row.grid_columnconfigure(1, weight=1)
        when = it["date"].astimezone()
        ctk.CTkLabel(row, text=sched.fmt(when), width=130, anchor="w",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, rowspan=2, padx=12, pady=8)
        who = f"{e.cfg['name']} · " if self.multi else ""
        head = f"{who}{e.channel_title(it['channel'])}" + ("  ·  📷" if it["media"] else "") + \
               ("" if d else "  ·  добавлен вручную")
        ctk.CTkLabel(row, text=head, anchor="w", text_color="gray").grid(row=0, column=1, sticky="w", pady=(6, 0))
        text = (d.topic if d else "") or it["text"] or "(только медиа)"
        if d and it["text"]:
            text = f"{d.topic} — {it['text']}"
        ctk.CTkLabel(row, text=text.replace("\n", " ")[:160], anchor="w", justify="left",
                     wraplength=620).grid(row=1, column=1, sticky="w", pady=(0, 6))
        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, padx=8)
        ctk.CTkButton(btns, text="Перенести…", width=100, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: self.move_scheduled(e, it)).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="Опубликовать сейчас", width=160, fg_color=GREEN, hover_color=GREEN_HOVER,
                      command=lambda: self.send_scheduled_now(e, it)).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="Снять" if d else "Удалить", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: self.unschedule(e, it, d)).pack(side="left", padx=2)

    def move_scheduled(self, e: Engine, it: dict):
        dlg = ctk.CTkInputDialog(title="Перенести пост",
                                 text="Новое время: «19:30», «завтра 10:00» или «07.11 19:30»")
        s = dlg.get_input()
        if not s:
            return
        at = sched.parse_when(s)
        if not at or at <= datetime.now().astimezone() + sched.MIN_LEAD:
            messagebox.showwarning("Перенести", "Не понял время или оно уже прошло: «19:30», «завтра 10:00», «07.11 19:30»")
            return
        e.reschedule(it["channel"], it["ids"], at)

    def send_scheduled_now(self, e: Engine, it: dict):
        if messagebox.askyesno("Опубликовать сейчас", "Опубликовать этот пост в канале прямо сейчас?"):
            e.send_now(it["channel"], it["ids"])

    def unschedule(self, e: Engine, it: dict, d: Draft | None):
        text = ("Снять пост с отложенных? Он вернётся во вкладку «Проверка»." if d else
                "Этот пост добавлен в отложенные вручную. Удалить его из отложенных в Telegram? Вернуть не получится.")
        if messagebox.askyesno("Снять с отложенных", text):
            e.unschedule(it["channel"], it["ids"])

    # ------------------------------------------------------------------ опубликовано

    def _build_published_tab(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        self.pub_search = ctk.CTkEntry(top, width=320, placeholder_text="Поиск по теме и тексту")
        self.pub_search.pack(side="left")
        self.pub_search.bind("<KeyRelease>", lambda e: self.render_published())
        self.pub_info = ctk.CTkLabel(top, text="", text_color="gray")
        self.pub_info.pack(side="left", padx=12)
        self.pub_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.pub_list.pack(fill="both", expand=True)

    PUB_MAX_ROWS = 200

    def render_published(self):
        for w in self.pub_list.winfo_children():
            w.destroy()
        q = self.pub_search.get().strip().lower()
        items = [d for d in self.store.list(PUBLISHED)
                 if not q or q in d.topic.lower() or q in d.text.lower()]
        items.sort(key=lambda d: d.publish_at or d.created, reverse=True)
        self.pub_info.configure(text=f"Опубликовано: {len(items)}" + (
            f" (показаны последние {self.PUB_MAX_ROWS})" if len(items) > self.PUB_MAX_ROWS else ""))
        if not items:
            ctk.CTkLabel(self.pub_list, text="Здесь появятся опубликованные посты.", text_color="gray",
                         font=ctk.CTkFont(size=14)).pack(pady=40)
            return
        for d in items[:self.PUB_MAX_ROWS]:
            e = self.owner(d)
            row = ctk.CTkFrame(self.pub_list)
            row.pack(fill="x", pady=3, padx=2)
            row.grid_columnconfigure(1, weight=1)
            when = d.when()
            ctk.CTkLabel(row, text=when.astimezone().strftime("%d.%m.%Y %H:%M") if when else "—", width=130,
                         anchor="w").grid(row=0, column=0, rowspan=2, padx=12, pady=8)
            who = f"{e.cfg['name']} · " if e and self.multi else ""
            chan = e.channel_title(d.channel) if e else core.channel_label(d.channel)
            src = " + ".join(x for x in (d.text_source, d.image_source) if x)
            edited = "  ·  правка вручную" if d.generated and d.text.strip() != d.generated.strip() else ""
            ctk.CTkLabel(row, text=f"{who}{chan}  ·  {src}{edited}", anchor="w",
                         text_color="gray").grid(row=0, column=1, sticky="w", pady=(6, 0))
            ctk.CTkLabel(row, text=f"{d.topic} — " + core.visible_text(d.text, e.cfg if e else {}).replace("\n", " ")[:140],
                         anchor="w", justify="left", wraplength=640).grid(row=1, column=1, sticky="w", pady=(0, 6))
            btns = ctk.CTkFrame(row, fg_color="transparent")
            btns.grid(row=0, column=2, rowspan=2, padx=8)
            if d.link:
                ctk.CTkButton(btns, text="Открыть", width=90,
                              command=lambda u=d.link: webbrowser.open(u)).pack(side="left", padx=2)
            ctk.CTkButton(btns, text="Копировать текст", width=140, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=lambda t=d.text: self.copy(t)).pack(side="left", padx=2)

    def copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.statusbar.configure(text="Скопировано", text_color=GREEN)

    # ------------------------------------------------------------------ расписание

    def _build_schedule_tab(self, tab):
        sf = self.schedule_sf = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        sf.pack(fill="both", expand=True)
        ctk.CTkLabel(sf, justify="left", anchor="w", text_color="gray", wraplength=1000, text=(
            "Когда выходят посты, поставленные «в расписание». Программа берёт ближайшее свободное время: рядом "
            "с ним не должно быть других отложенных постов канала — ни своих, ни добавленных вручную в Telegram. "
            "Расписание своё у каждого аккаунта и общее для его каналов.")).pack(fill="x", pady=(0, 8))
        top = ctk.CTkFrame(sf)
        top.pack(fill="x", pady=4)
        ctk.CTkLabel(top, text="Как задавать время").grid(row=0, column=0, sticky="w", padx=14, pady=10)
        self.sched_mode = ctk.CTkSegmentedButton(top, values=list(SCHED_MODES),
                                                 command=lambda v: self.show_sched_mode())
        self.sched_mode.grid(row=0, column=1, sticky="w", padx=4)

        self.slots_frame = ctk.CTkFrame(sf)
        ctk.CTkLabel(self.slots_frame, text="Времена публикаций по дням — через запятую, например «10:00, 19:30». "
                                            "Пусто — в этот день не публиковать.",
                     text_color="gray").grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 4))
        self.slot_entries = []
        for i, day in enumerate(core.DAYS):
            ctk.CTkLabel(self.slots_frame, text=day, width=40).grid(row=i + 1, column=0, padx=14, pady=3, sticky="w")
            e = ctk.CTkEntry(self.slots_frame, width=420)
            e.grid(row=i + 1, column=1, sticky="w", padx=4, pady=3)
            e.bind("<FocusOut>", lambda ev: self.update_preview())
            self.slot_entries.append(e)
        cp = ctk.CTkFrame(self.slots_frame, fg_color="transparent")
        cp.grid(row=9, column=1, sticky="w", padx=4, pady=(4, 10))
        ctk.CTkButton(cp, text="Как в понедельник — на будни", width=210, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: self.copy_monday(range(1, 5))).pack(side="left", padx=(0, 6))
        ctk.CTkButton(cp, text="Как в понедельник — на все дни", width=230, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: self.copy_monday(range(1, 7))).pack(side="left")

        self.interval_frame = ctk.CTkFrame(sf)
        f = self.interval_frame
        ctk.CTkLabel(f, text="Каждые, часов").grid(row=0, column=0, sticky="w", padx=14, pady=(10, 4))
        self.int_hours = ctk.CTkEntry(f, width=80)
        self.int_hours.grid(row=0, column=1, sticky="w", padx=4, pady=(10, 4))
        ctk.CTkLabel(f, text="можно дробное: 1.5", text_color="gray").grid(row=0, column=2, sticky="w", padx=8)
        ctk.CTkLabel(f, text="С").grid(row=1, column=0, sticky="w", padx=14, pady=4)
        self.int_from = ctk.CTkEntry(f, width=80)
        self.int_from.grid(row=1, column=1, sticky="w", padx=4)
        ctk.CTkLabel(f, text="До").grid(row=2, column=0, sticky="w", padx=14, pady=4)
        self.int_to = ctk.CTkEntry(f, width=80)
        self.int_to.grid(row=2, column=1, sticky="w", padx=4)
        days = ctk.CTkFrame(f, fg_color="transparent")
        days.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(4, 10))
        self.int_days = []
        for i, day in enumerate(core.DAYS):
            cb = ctk.CTkCheckBox(days, text=day, width=60, command=self.update_preview)
            cb.pack(side="left", padx=4)
            self.int_days.append(cb)

        common = ctk.CTkFrame(sf)
        common.pack(fill="x", pady=4, side="bottom")
        self.sched_common = common
        ctk.CTkLabel(common, text="Случайный сдвиг, ± минут").grid(row=0, column=0, sticky="w", padx=14, pady=(10, 4))
        self.jitter = ctk.CTkEntry(common, width=80)
        self.jitter.grid(row=0, column=1, sticky="w", padx=4, pady=(10, 4))
        ctk.CTkLabel(common, text="чтобы посты не выходили секунда в секунду; 0 — точно по времени",
                     text_color="gray").grid(row=0, column=2, sticky="w", padx=8)
        ctk.CTkLabel(common, text="Минимум между постами, минут").grid(row=1, column=0, sticky="w", padx=14, pady=4)
        self.min_gap = ctk.CTkEntry(common, width=80)
        self.min_gap.grid(row=1, column=1, sticky="w", padx=4)
        ctk.CTkLabel(common, text="слот пропускается, если рядом уже стоит другой пост канала",
                     text_color="gray").grid(row=1, column=2, sticky="w", padx=8)
        pv = ctk.CTkFrame(common, fg_color="transparent")
        pv.grid(row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(8, 4))
        ctk.CTkLabel(pv, text="Ближайшие свободные:", font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.preview_chan = ctk.CTkOptionMenu(pv, values=["—"], width=200, command=lambda v: self.update_preview())
        self.preview_chan.pack(side="left", padx=8)
        self.preview_lbl = ctk.CTkLabel(common, text="", anchor="w", justify="left", wraplength=1000)
        self.preview_lbl.grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 12))
        for e in (self.int_hours, self.int_from, self.int_to, self.jitter, self.min_gap):
            e.bind("<FocusOut>", lambda ev: self.update_preview())

        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(bottom, text="Сохранить расписание", height=38, width=220,
                      command=self.apply_schedule).pack(side="right")
        self.sched_msg = ctk.CTkLabel(bottom, text="", text_color=GREEN)
        self.sched_msg.pack(side="right", padx=12)
        self.load_schedule_form()

    def show_sched_mode(self):
        if SCHED_MODES.get(self.sched_mode.get()) == "interval":
            self.slots_frame.pack_forget()
            self.interval_frame.pack(fill="x", pady=4, before=self.sched_common)
        else:
            self.interval_frame.pack_forget()
            self.slots_frame.pack(fill="x", pady=4, before=self.sched_common)
        self.update_preview()

    def copy_monday(self, days):
        value = self.slot_entries[0].get()
        for i in days:
            self.slot_entries[i].delete(0, "end")
            self.slot_entries[i].insert(0, value)
        self.update_preview()

    def load_schedule_form(self):
        s = self.cfg["schedule"]
        self.sched_mode.set(key_of(SCHED_MODES, s.get("mode")))
        for i, e in enumerate(self.slot_entries):
            e.delete(0, "end")
            e.insert(0, ", ".join(s["slots"].get(str(i), [])))
        for e, key in ((self.int_hours, "interval_hours"), (self.int_from, "window_from"),
                       (self.int_to, "window_to"), (self.jitter, "jitter_min"), (self.min_gap, "min_gap_min")):
            e.delete(0, "end")
            e.insert(0, f"{s.get(key, '')}".removesuffix(".0"))
        for i, cb in enumerate(self.int_days):
            cb.select() if i in s.get("days", []) else cb.deselect()
        self.show_sched_mode()

    def read_schedule(self) -> tuple[dict | None, str]:
        """Расписание из формы и ошибка, если что-то не так."""
        s = dict(self.cfg["schedule"])
        s["mode"] = SCHED_MODES.get(self.sched_mode.get(), "slots")
        slots, bad = {}, []
        for i, e in enumerate(self.slot_entries):
            good, wrong = sched.parse_times(e.get())
            slots[str(i)] = good
            bad += [f"{core.DAYS[i]}: {w}" for w in wrong]
        if bad and s["mode"] == "slots":
            return None, "Не понял время: " + ", ".join(bad) + " — пишите как 10:00"
        s["slots"] = slots
        try:
            s["interval_hours"] = float(self.int_hours.get().replace(",", ".") or 4)
            s["jitter_min"] = int(self.jitter.get() or 0)
            s["min_gap_min"] = int(self.min_gap.get() or 0)
        except ValueError:
            return None, "Интервал, сдвиг и промежуток — числа"
        if not 0.25 <= s["interval_hours"] <= 24:
            return None, "Интервал — от 0.25 до 24 часов"
        if s["jitter_min"] < 0 or s["min_gap_min"] < 0:
            return None, "Сдвиг и промежуток не могут быть отрицательными"
        for key, e in (("window_from", self.int_from), ("window_to", self.int_to)):
            t = sched.parse_time(e.get())
            if not t and s["mode"] == "interval":
                return None, "Окно публикаций — время вида 09:00"
            s[key] = f"{t.hour:02}:{t.minute:02}" if t else s[key]
        s["days"] = [i for i, cb in enumerate(self.int_days) if cb.get()]
        return s, ""

    def update_preview(self):
        s, err = self.read_schedule()
        if err:
            self.preview_lbl.configure(text=err, text_color=RED)
            return
        channel = self.chan_labels().get(self.preview_chan.get())
        taken = self.engine.taken_times(channel) if channel else []
        found = sched.next_free(s, taken, n=6, jitter=False)
        if not found:
            self.preview_lbl.configure(text="В расписании нет ни одного времени", text_color=RED)
            return
        note = "" if self.engine.status == "running" else \
            "  (аккаунт не подключён — отложенные посты из Telegram не учтены)"
        self.preview_lbl.configure(text="   ".join(sched.fmt(x) for x in found) + note, text_color=("gray10", "gray90"))

    def apply_schedule(self, silent=False) -> bool:
        s, err = self.read_schedule()
        if err:
            if not silent:
                self.sched_msg.configure(text=err, text_color=RED)
            else:
                messagebox.showerror("Расписание", err)
            return False
        if s != self.cfg["schedule"]:
            self.cfg["schedule"] = s
            self.save()
        self.update_preview()
        if not silent:
            self.sched_msg.configure(text="Сохранено ✓", text_color=GREEN)
            self.after(5000, lambda: self.sched_msg.configure(text=""))
            self.render_review()
        return True

    # ------------------------------------------------------------------ промпты

    def _build_prompt_tab(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        self.prompt_kind = ctk.CTkSegmentedButton(top, values=["Промпт текста", "Промпт картинки"],
                                                  command=lambda v: self.show_prompt())
        self.prompt_kind.set("Промпт текста")
        self.prompt_kind.pack(side="left")
        self.prompt_hint = ctk.CTkLabel(tab, justify="left", anchor="w", text_color="gray", wraplength=1050)
        self.prompt_hint.pack(fill="x", pady=(0, 6))
        self.prompt_boxes = {}
        for kind in ("text", "image"):
            self.prompt_boxes[kind] = box = ctk.CTkTextbox(tab, wrap="word", font=ctk.CTkFont(size=13))
            box.bind("<KeyRelease>", lambda e: self.update_prompt_msg())
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0), side="bottom")
        ctk.CTkButton(bottom, text="Сохранить промпты", height=38, width=200,
                      command=self.save_prompts).pack(side="right")
        ctk.CTkButton(bottom, text="Отменить изменения", height=38, fg_color=GRAY,
                      hover_color=GRAY_HOVER, command=self.load_prompts).pack(side="right", padx=8)
        ctk.CTkButton(bottom, text="Вставить пример", height=38, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.prompt_example).pack(side="right")
        self.prompt_msg = ctk.CTkLabel(bottom, text="", text_color="gray")
        self.prompt_msg.pack(side="left")
        self.load_prompts()

    @property
    def prompt_now(self) -> str:
        return "image" if self.prompt_kind.get() == "Промпт картинки" else "text"

    def show_prompt(self):
        kind = self.prompt_now
        for k, box in self.prompt_boxes.items():
            if k == kind:
                box.pack(fill="both", expand=True)
            else:
                box.pack_forget()
        self.prompt_hint.configure(text=(
            "Инструкция для нейросети текста. Можно вставлять: " + TEXT_PLACEHOLDERS + ". Чего в промпте нет, "
            "программа допишет в запрос сама — длина, оформление, эмодзи и хэштеги берутся из «Настройки» → «Пост». "
            "{{вариант 1 | вариант 2}} — к каждому посту подставится случайный вариант."
            if kind == "text" else
            "Запрос к нейросети картинок. Можно вставлять: " + IMAGE_PLACEHOLDERS + ". Стиль и формат — из "
            "«Настройки» → «Картинка». «Поправить эту» в «Проверке» шлёт только пожелание и саму картинку."))
        self.update_prompt_msg()

    def load_prompts(self):
        for kind, box in self.prompt_boxes.items():
            box.delete("1.0", "end")
            box.insert("1.0", core.read_prompt(self.cfg, kind))
        self.show_prompt()

    def prompt_example(self):
        example = core.RES / f"prompt_{self.prompt_now}.example.txt"
        if example.exists() and messagebox.askyesno("Пример", "Заменить текущий промпт примером из программы?"):
            box = self.prompt_boxes[self.prompt_now]
            box.delete("1.0", "end")
            box.insert("1.0", example.read_text(encoding="utf-8"))
            self.update_prompt_msg()

    def prompt_dirty(self, kind: str) -> bool:
        return self.prompt_boxes[kind].get("1.0", "end-1c") != core.read_prompt(self.cfg, kind)

    def prompts_dirty(self) -> bool:
        return any(self.prompt_dirty(k) for k in self.prompt_boxes)

    def save_prompts(self):
        for kind, box in self.prompt_boxes.items():
            core.prompt_path(self.cfg, kind).write_text(box.get("1.0", "end-1c"), encoding="utf-8")
        self.update_prompt_msg("Сохранено ✓ — применяется к следующей генерации")

    def update_prompt_msg(self, extra=""):
        n = len(self.prompt_boxes[self.prompt_now].get("1.0", "end-1c"))
        dirty = [name for k, name in (("text", "текста"), ("image", "картинки")) if self.prompt_dirty(k)]
        self.prompt_msg.configure(text=f"{n} символов" + (f"  ·  не сохранён промпт {' и '.join(dirty)}"
                                                          if dirty else "") + (f"  ·  {extra}" if extra else ""))

    # ------------------------------------------------------------------ настройки

    def _build_settings_tab(self, tab):
        sf = self.settings_sf = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        sf.pack(fill="both", expand=True)

        self.welcome = ctk.CTkFrame(sf, fg_color=("#e3f0ff", "#1d2f44"))
        ctk.CTkLabel(self.welcome, justify="left", anchor="w", wraplength=980, text=(
            "👋  Добро пожаловать! Для начала работы:\n"
            "1. «Telegram» — укажите канал, куда публиковать (аккаунт должен быть в нём админом с правом публикации)\n"
            "2. «Нейросеть для текста» — уже выбран Z.ai: бесплатно и без VPN, нужен только ключ. Или выберите "
            "Claude, ChatGPT (Codex), Gemini, свою модель…\n"
            "3. «Картинка» — чем рисовать: OpenAI API или Gemini API по ключу, ChatGPT (Codex) по подписке, "
            "картинки из своей папки или без картинки\n"
            "4. «Пост» — длина, оформление, эмодзи, хэштеги, подпись. Вкладка «Промпты» — о чём и как писать\n"
            "5. «Сохранить», затем «▶ Запустить» — войдите в Telegram. Темы постов — во вкладке «Темы»"
        )).pack(padx=14, pady=10, anchor="w")

        def section(title, hint=None):
            f = ctk.CTkFrame(sf)
            f.pack(fill="x", padx=4, pady=6)
            f.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=15, weight="bold")).grid(
                row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 0))
            if hint:
                ctk.CTkLabel(f, text=hint, text_color="gray", justify="left").grid(
                    row=1, column=0, columnspan=3, sticky="w", padx=14)
            return f

        def field(f, row, key, label, hint="", width=None, **kw):
            ctk.CTkLabel(f, text=label).grid(row=row, column=0, sticky="w", padx=14, pady=5)
            kw.pop("placeholder_text", None)   # с общей переменной подсказка в поле не показывается
            e = ctk.CTkEntry(f, width=width or 300, textvariable=self.var(key), **kw)
            e.grid(row=row, column=1, sticky="w" if width else "ew", padx=4, pady=5)
            if hint:
                ctk.CTkLabel(f, text=hint, text_color="gray").grid(row=row, column=2, sticky="w", padx=10)
            self.fields.setdefault(key, e)
            return e

        def seg(f, row, label, names: dict, hint="", command=None):
            ctk.CTkLabel(f, text=label).grid(row=row, column=0, sticky="w", padx=14, pady=5)
            w = ctk.CTkSegmentedButton(f, values=list(names), command=command)
            w.grid(row=row, column=1, columnspan=2 if not hint else 1, sticky="w", padx=4, pady=5)
            if hint:
                ctk.CTkLabel(f, text=hint, text_color="gray").grid(row=row, column=2, sticky="w", padx=10)
            return w

        tg = section("Telegram")
        self.settings_first = tg
        builtin = core.BUILTIN_TELEGRAM_APP is not None

        def key_fields(f, row):
            """API ID и Hash. Со встроенным ключом их можно не трогать — тогда они в «Продвинутых»."""
            field(f, row, "api_id", "API ID", "необязательно — пусто: встроенный ключ" if builtin else "", width=200)
            hash_e = field(f, row + 1, "api_hash", "API Hash", show="•")
            ctk.CTkButton(f, text="показать", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=lambda: hash_e.configure(show="" if hash_e.cget("show") else "•")
                          ).grid(row=row + 1, column=2, sticky="w", padx=10)
            ctk.CTkButton(f, text=("Свой ключ (по желанию) → my.telegram.org" if builtin
                                   else "Где взять API ID и Hash → my.telegram.org"), fg_color="transparent",
                          text_color=("#1f6aa5", "#5aa9e6"), hover=False, anchor="w",
                          command=lambda: webbrowser.open("https://my.telegram.org/apps")
                          ).grid(row=row + 2, column=1, sticky="w")
        if not builtin:
            key_fields(tg, 2)
        ctk.CTkLabel(tg, text="Каналы").grid(row=5, column=0, sticky="nw", padx=14, pady=8)
        self.channels_box = ctk.CTkTextbox(tg, height=96)
        self.channels_box.grid(row=5, column=1, sticky="ew", padx=4, pady=(5, 12))
        ctk.CTkLabel(tg, text="куда публиковать, по одному\nна строку: @username или\nссылка t.me/…; закрытый —\n"
                              "t.me/+… или ссылка на пост.\nАккаунт — админ канала\nс правом публикации",
                     text_color="gray", justify="left").grid(row=5, column=2, sticky="nw", padx=10, pady=5)

        ai = section("Нейросеть для текста", "Пишет текст поста по промпту текста")
        ai.grid_columnconfigure(0, minsize=LABEL_W)
        ctk.CTkLabel(ai, text="Как подключаться").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        self.backend = ctk.CTkSegmentedButton(ai, values=list(BACKENDS),
                                              command=lambda v: self.show_backend())
        self.backend.grid(row=2, column=1, columnspan=2, sticky="w", padx=4)

        def sub(hint):
            f = ctk.CTkFrame(ai, fg_color="transparent")
            f.grid_columnconfigure(1, weight=1)
            f.grid_columnconfigure(0, minsize=LABEL_W)
            ctk.CTkLabel(f, text=hint, text_color="gray", justify="left", wraplength=900).grid(
                row=0, column=0, columnspan=3, sticky="w", padx=14)
            return f

        def model_row(f, row, widget, hint):
            ctk.CTkLabel(f, text="Модель").grid(row=row, column=0, sticky="w", padx=14, pady=5)
            widget.grid(row=row, column=1, sticky="w", padx=4)
            lbl = ctk.CTkLabel(f, text=hint, text_color="gray")
            lbl.grid(row=row, column=2, sticky="w", padx=10)
            return lbl

        # --- Claude Code по подписке ---
        cc = sub("Работает через программу Claude Code и вашу подписку Pro/Max — "
                 "за запросы отдельно не платите.")
        self.model = ctk.CTkSegmentedButton(cc, values=core.MODELS)
        model_row(cc, 1, self.model, "sonnet — оптимально, opus — умнее и медленнее, haiku — быстрее")
        self.claude_block = CliBlock(self, cc, row=2, name="Claude Code", account="Claude",
                                     status=core.claude_status, install=core.install_claude,
                                     login=core.open_claude_login, describe=describe_claude, hint=(
            "Откроется браузер (или окно с ссылкой) — войдите в аккаунт Claude с подпиской "
            "Pro/Max и нажмите «Authorize».\n\n"
            "Если в чёрном окне попросят вставить код — скопируйте его со страницы в браузере, "
            "вставьте туда и нажмите Enter.\n\nПрограмма сама заметит, когда вход завершится."))

        # --- Anthropic API по ключу ---
        api = sub("Запросы идут напрямую в API Anthropic и оплачиваются с баланса "
                  "по токенам. Claude Code и подписка не нужны.")
        self.api_model = ctk.CTkOptionMenu(api, values=core.API_MODELS, width=220,
                                           command=lambda v: self.update_price())
        KeyBlock(self, api, "api_key", "sk-ant-…", "Где взять ключ → console.anthropic.com",
                 "https://console.anthropic.com/settings/keys", self.api_model, "api_model",
                 core.check_api_key)
        self.price_lbl = model_row(api, 3, self.api_model, "")

        # --- Antigravity CLI через Google-аккаунт (преемник Gemini CLI) ---
        gc = sub("Работает через Antigravity CLI — новую программу Google вместо Gemini CLI — и ваш "
                 "Google-аккаунт; ключ не нужен. Модели Gemini; лимиты зависят от аккаунта "
                 "(бесплатный, AI Pro или Ultra).")
        self.gemini_model = ctk.CTkComboBox(gc, values=[DEFAULT_MODEL], width=260)
        model_row(gc, 1, self.gemini_model, "список моделей появится после входа")
        self.gemini_block = CliBlock(self, gc, row=2, name="Antigravity CLI", account="Google",
                                     status=core.agy_status, install=core.install_agy,
                                     login=core.open_agy_login, describe=self.describe_agy, hint=(
            "Откроется окно Antigravity CLI и браузер — войдите в Google-аккаунт и разрешите доступ.\n\n"
            "Если браузер не открылся — скопируйте ссылку из чёрного окна в браузер, а полученный "
            "код вставьте обратно в окно и нажмите Enter.\n\n"
            "Когда вход завершится, окно Antigravity можно закрыть и нажать «Готово — проверить»."),
            relogin_hint=(
            "Откроется окно Antigravity — в нём ещё старый аккаунт.\n\n"
            "1. Введите /logout и нажмите Enter.\n"
            "2. Войдите в нужный Google-аккаунт (Antigravity сам предложит вход или откроет браузер).\n"
            "3. Закройте окно и нажмите в программе «Готово — проверить».\n\n"
            "Какой аккаунт сейчас активен, видно в окне Antigravity по команде /usage."))

        # --- Gemini API по ключу ---
        ga = sub("Запросы идут в Gemini API по ключу из Google AI Studio. У ключа есть бесплатный "
                 "лимит, сверх него — оплата по токенам. Gemini CLI не нужен.")
        self.gemini_api_model = ctk.CTkComboBox(ga, values=[core.GEMINI_API_DEFAULT], width=260)
        KeyBlock(self, ga, "gemini_api_key", "AIza…", "Где взять ключ → aistudio.google.com",
                 "https://aistudio.google.com/apikey", self.gemini_api_model, "gemini_api_model",
                 core.check_gemini_key)
        model_row(ga, 3, self.gemini_api_model, "список моделей подгрузится после «Проверить ключ»")

        # --- ChatGPT через Codex CLI по подписке ---
        cx = sub("Работает через Codex CLI — программу OpenAI — и ваш аккаунт ChatGPT с любым тарифом, "
                 "включая бесплатный; ключ не нужен. Расходует лимиты Codex из тарифа: на Free и Go они "
                 "маленькие, для постоянной работы лучше Plus и выше.")
        self.codex_model = ctk.CTkComboBox(cx, values=[DEFAULT_MODEL], width=260)
        model_row(cx, 1, self.codex_model, "список моделей появится после входа")
        self.codex_block = CliBlock(self, cx, row=2, name="Codex CLI", account="ChatGPT",
                                    status=core.codex_status, install=core.install_codex,
                                    login=core.open_codex_login, describe=self.describe_codex, hint=(
            "Откроется браузер — войдите в аккаунт ChatGPT и разрешите доступ для Codex.\n\n"
            "Если браузер не открылся — скопируйте ссылку из чёрного окна в браузер.\n\n"
            "Когда вход завершится, нажмите в программе «Готово — проверить»."),
            relogin_hint=(
            "Программа вышла из текущего аккаунта ChatGPT и открыла вход заново.\n\n"
            "Войдите в браузере в нужный аккаунт, затем нажмите «Готово — проверить»."))

        # --- OpenAI API по ключу ---
        oa = sub("Запросы идут напрямую в OpenAI API и оплачиваются с баланса по токенам "
                 "(отдельно от подписки ChatGPT). Codex не нужен.")
        self.openai_api_model = ctk.CTkComboBox(oa, values=[core.OPENAI_API_DEFAULT], width=260)
        KeyBlock(self, oa, "openai_api_key", "sk-…", "Где взять ключ → platform.openai.com",
                 "https://platform.openai.com/api-keys", self.openai_api_model, "openai_api_model",
                 core.check_openai_key)
        model_row(oa, 3, self.openai_api_model, "список моделей подгрузится после «Проверить ключ»")

        # --- Z.ai: бесплатные GLM, работает из России ---
        za = sub("Бесплатные нейросети GLM от Z.ai. Работают в России без VPN, карта не нужна — только "
                 "бесплатный ключ. Бесплатно "
                 "отвечают по одному запросу за раз — несколько аккаунтов встают в очередь сами.")
        self.zai_model = ctk.CTkComboBox(za, values=core.ZAI_MODELS, width=260)
        self.zai_block = KeyBlock(self, za, "zai_api_key", "вставится сам, когда скопируете",
                                  "Получить бесплатный ключ → z.ai", core.ZAI_KEYS_URL, self.zai_model,
                                  "zai_model", core.check_zai_key, autoselect=True)
        ctk.CTkLabel(za, justify="left", anchor="w", text=(
            "1. Нажмите «Получить бесплатный ключ» — откроется сайт Z.ai (на английском).\n"
            "2. Войдите через Google или зарегистрируйтесь по почте (Sign up).\n"
            "3. Нажмите «Create a new API key» (создать ключ), введите любое имя и скопируйте ключ (Copy).\n"
            "4. Вернитесь в программу — ключ вставится и проверится сам. Нажмите «Сохранить».")
        ).grid(row=3, column=1, columnspan=2, sticky="w", padx=4, pady=(4, 6))
        model_row(za, 4, self.zai_model, "менять не нужно — glm-4.7-flash бесплатная и самая новая из бесплатных")

        # --- своя модель: любой OpenAI-совместимый сервер ---
        lm = sub("Своя нейросеть на OpenAI-совместимом сервере: локально — LM Studio, Ollama, llama.cpp, vLLM "
                 "(бесплатно, без интернета и VPN), или облачные OpenRouter, DeepSeek и т. п. Наш промпт "
                 "большой — у модели должен быть контекст от 16–32 тыс. токенов; ")
        ctk.CTkLabel(lm, text="Адрес сервера").grid(row=1, column=0, sticky="w", padx=14, pady=5)
        url_e = ctk.CTkEntry(lm, textvariable=self.var("compat_base_url"))
        url_e.grid(row=1, column=1, sticky="ew", padx=4)
        self.fields["compat_base_url"] = url_e
        presets = ctk.CTkFrame(lm, fg_color="transparent")
        presets.grid(row=1, column=2, sticky="w", padx=6)

        def preset(u):
            self.var("compat_base_url").set(u)
        for name, u in core.COMPAT_PRESETS.items():
            ctk.CTkButton(presets, text=name, width=90, fg_color=GRAY, hover_color=GRAY_HOVER,
                          command=lambda u=u: preset(u)).pack(side="left", padx=2)
        ctk.CTkLabel(lm, text="API-ключ").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        key_e = ctk.CTkEntry(lm, show="•", textvariable=self.var("compat_api_key"))
        key_e.grid(row=2, column=1, sticky="ew", padx=4)
        self.fields["compat_api_key"] = key_e
        self.compat_model = ctk.CTkComboBox(lm, values=[""], width=260)
        model_row(lm, 3, self.compat_model, "список подгрузится после «Проверить подключение»")
        chk = ctk.CTkFrame(lm, fg_color="transparent")
        chk.grid(row=4, column=1, columnspan=2, sticky="w", padx=4, pady=(2, 4))
        self.compat_btn = ctk.CTkButton(chk, text="Проверить подключение", width=180, command=self.check_compat)
        self.compat_btn.pack(side="left")
        self.compat_lbl = ctk.CTkLabel(chk, text="", text_color="gray")
        self.compat_lbl.pack(side="left", padx=10)


        self.backend_frames = {"claude_code": cc, "api": api, "gemini_cli": gc, "gemini_api": ga,
                               "codex": cx, "openai_api": oa, "zai": za, "openai_compat": lm}
        self.bind("<FocusIn>", lambda e: self.zai_paste_key(), add="+")
        self.ai_common = ctk.CTkFrame(ai, fg_color="transparent")
        self.ai_common.grid_columnconfigure(0, minsize=LABEL_W)
        self.ai_common.grid(row=4, column=0, columnspan=3, sticky="ew")
        field(self.ai_common, 0, "claude_timeout_sec", "Таймаут ответа, сек", width=80)
        ctk.CTkFrame(ai, height=8, fg_color="transparent").grid(row=6, column=0)

        # --- картинка ---
        im = section("Картинка", "Рисует картинку к посту по промпту картинки — или берёт из вашей папки")
        im.grid_columnconfigure(0, minsize=LABEL_W)
        ctk.CTkLabel(im, text="Чем рисовать").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        self.image_backend = ctk.CTkSegmentedButton(im, values=list(IMAGE_BACKENDS),
                                                    command=lambda v: self.show_image_backend())
        self.image_backend.grid(row=2, column=1, columnspan=2, sticky="w", padx=4)

        def isub(hint):
            f = ctk.CTkFrame(im, fg_color="transparent")
            f.grid_columnconfigure(1, weight=1)
            f.grid_columnconfigure(0, minsize=LABEL_W)
            ctk.CTkLabel(f, text=hint, text_color="gray", justify="left", wraplength=900).grid(
                row=0, column=0, columnspan=3, sticky="w", padx=14)
            return f

        io = isub("Картинки рисует OpenAI (модели gpt-image) по API-ключу, оплата с баланса за каждую картинку. "
                  "Умеет и править готовую картинку. Ключ общий с «Нейросеть для текста» → OpenAI API.")
        self.openai_image_model = ctk.CTkComboBox(io, values=core.OPENAI_IMAGE_MODELS, width=260)
        KeyBlock(self, io, "openai_api_key", "", "Где взять ключ → platform.openai.com",
                 "https://platform.openai.com/api-keys", self.openai_image_model, "openai_image_model",
                 core.check_openai_image, autoselect=True)
        model_row(io, 3, self.openai_image_model, "gpt-image-2 — новее и лучше, mini — дешевле")
        self.image_quality = seg(io, 4, "Качество", QUALITY_NAMES, "выше качество — дороже и дольше")

        ig = isub("Картинки рисует Google: модели Gemini Image (умеют править картинку) или Imagen. Нужен ключ "
                  "Google AI Studio; у моделей картинок обычно нет бесплатного лимита. Ключ общий с текстом.")
        self.gemini_image_model = ctk.CTkComboBox(ig, values=core.GEMINI_IMAGE_MODELS, width=260)
        KeyBlock(self, ig, "gemini_api_key", "", "Где взять ключ → aistudio.google.com",
                 "https://aistudio.google.com/apikey", self.gemini_image_model, "gemini_image_model",
                 core.check_gemini_image, autoselect=True)
        model_row(ig, 3, self.gemini_image_model, "список подгрузится после «Проверить ключ»")

        ix = isub("Рисует ChatGPT через Codex CLI по вашей подписке — без ключа и оплаты за картинку. Генерация "
                  "картинок в Codex есть не на всех тарифах (на бесплатном — нет): если картинка не появится, "
                  "программа скажет об этом — тогда выберите OpenAI API или Gemini API.")
        self.codex_img_block = CliBlock(self, ix, row=1, name="Codex CLI", account="ChatGPT",
                                        status=core.codex_status, install=core.install_codex,
                                        login=core.open_codex_login, describe=self.describe_codex, hint=(
            "Откроется браузер — войдите в аккаунт ChatGPT и разрешите доступ для Codex.\n\n"
            "Когда вход завершится, нажмите в программе «Готово — проверить»."))

        iz = isub("Модели CogView от Z.ai. В отличие от текстовых, генерация картинок в Z.ai платная — "
                  "нужен баланс на z.ai. Ключ общий с «Нейросеть для текста» → Z.ai.")
        self.zai_image_model = ctk.CTkComboBox(iz, values=core.ZAI_IMAGE_MODELS, width=260)
        KeyBlock(self, iz, "zai_api_key", "", "Ключ → z.ai", core.ZAI_KEYS_URL, self.zai_image_model,
                 "zai_image_model", core.check_zai_image)
        model_row(iz, 3, self.zai_image_model, "")

        ic = isub("Своя модель на сервере с OpenAI-совместимым /v1/images/generations (например, LocalAI "
                  "или облачный сервис). Адрес и ключ — общие с «Нейросеть для текста» → «Своя модель».")
        ctk.CTkLabel(ic, text="Адрес сервера").grid(row=1, column=0, sticky="w", padx=14, pady=5)
        ctk.CTkEntry(ic, textvariable=self.var("compat_base_url")).grid(row=1, column=1, sticky="ew", padx=4)
        ctk.CTkLabel(ic, text="API-ключ").grid(row=2, column=0, sticky="w", padx=14, pady=5)
        ctk.CTkEntry(ic, show="•", textvariable=self.var("compat_api_key")).grid(row=2, column=1, sticky="ew", padx=4)
        field(ic, 3, "compat_image_model", "Модель картинок", width=260)

        ifo = isub("Нейросеть не рисует: к каждому посту берётся картинка из вашей папки — по очереди в случайном "
                   "порядке, без повторов, пока не кончится круг. «Нарисовать заново» в «Проверке» берёт другую.")
        ctk.CTkLabel(ifo, text="Папка").grid(row=1, column=0, sticky="w", padx=14, pady=5)
        e = ctk.CTkEntry(ifo, textvariable=self.var("images_dir"))
        e.grid(row=1, column=1, sticky="ew", padx=4)
        self.fields["images_dir"] = e
        btns = ctk.CTkFrame(ifo, fg_color="transparent")
        btns.grid(row=1, column=2, sticky="w", padx=6)
        ctk.CTkButton(btns, text="Выбрать…", width=90, command=self.browse_images).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="Открыть", width=80, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.open_images).pack(side="left", padx=2)
        self.img_count = ctk.CTkLabel(ifo, text="", text_color="gray")
        self.img_count.grid(row=2, column=1, sticky="w", padx=4)

        inone = isub("Посты без картинки. Свою картинку к любому посту можно добавить в «Проверке» кнопкой «Своя…».")
        self.image_frames = {"openai_api": io, "gemini_api": ig, "codex": ix, "zai": iz, "openai_compat": ic,
                             "folder": ifo, "none": inone}
        self.image_common = ctk.CTkFrame(im, fg_color="transparent")
        self.image_common.grid_columnconfigure(0, minsize=LABEL_W)
        self.image_aspect = seg(self.image_common, 0, "Формат", ASPECT_NAMES)
        field(self.image_common, 1, "image_style", "Стиль картинок",
              "подставляется в промпт: «акварель», «фото», «3D»…")
        field(self.image_common, 2, "image_timeout_sec", "Таймаут, сек", width=80)
        ctk.CTkFrame(im, height=8, fg_color="transparent").grid(row=6, column=0)

        # --- пост ---
        po = section("Пост", "Передаётся нейросети в запросе — промпт можно не трогать")
        po.grid_columnconfigure(0, minsize=LABEL_W)
        field(po, 2, "post_max_len", "Длина, знаков (до)", "до 1024 — помещается в подпись к картинке; "
                                                           "у Telegram Premium — до 4096", width=80)
        self.post_format = seg(po, 3, "Оформление", FORMAT_NAMES)
        self.post_emoji = seg(po, 4, "Эмодзи", EMOJI_NAMES)
        self.post_hashtags = seg(po, 5, "Хэштеги", HASHTAG_NAMES, command=lambda v: self.show_hashtags())
        self.hashtags_row = field(po, 6, "hashtags", "Мои хэштеги", "через пробел: #новости #кофе")
        self.hashtags_extra = po.grid_slaves(row=6, column=0) + po.grid_slaves(row=6, column=2)
        field(po, 7, "post_signature", "Подпись в конце поста",
              'добавляет программа, можно ссылку: <a href="https://t.me/канал">Канал</a>')
        self.long_text = seg(po, 8, "Если текст длиннее подписи", LONG_NAMES)
        field(po, 9, "recent_in_prompt", "Показывать нейросети прошлых постов", "чтобы не повторялась; 0 — нет",
              width=80)
        self.typo_sw = ctk.CTkSwitch(po, text="Простая типографика: «—», «…» и ёлочки — обычными «-», «...» и \"\" "
                                              "(так пишут люди с телефона)")
        self.typo_sw.grid(row=10, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 12))

        wn = section("Программа", "Общее для всех аккаунтов")
        self.tray_sw = ctk.CTkSwitch(wn, text="При закрытии окна сворачивать в трей — программа продолжает "
                                              "работать, выход через значок в трее",
                                     command=self.on_tray_switch)
        if self.conf["close_to_tray"]:
            self.tray_sw.select()
        self.tray_sw.grid(row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 4))
        self.autostart_sw = ctk.CTkSwitch(wn, text=f"Запускать вместе с Windows — сразу в трей, через "
                                                   f"{core.AUTO_RUN_DELAY} сек начинать работу "
                                                   "(подключать аккаунты с «Запускать со всеми»)",
                                          command=self.on_autostart_switch)
        if core.get_autostart():
            self.autostart_sw.select()
        self.autostart_sw.grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=4)
        self.auto_update_sw = ctk.CTkSwitch(wn, text="Устанавливать обновления автоматически — когда ничего не генерируется; "
                                                     "после обновления работа продолжится сама",
                                            command=self.on_auto_update_switch)
        if self.conf["auto_update"]:
            self.auto_update_sw.select()
        self.auto_update_sw.grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=4)
        if not core.FROZEN:
            self.auto_update_sw.configure(state="disabled",
                                          text="Автообновление — только в установленной версии (из установщика)")
        self.notify_sw = ctk.CTkSwitch(wn, text="Уведомления Windows, когда окно свёрнуто в трей: черновики готовы, "
                                                "нужен вход и т. п. (со звуком Windows)",
                                       command=self.on_notify_switch)
        if self.conf["tray_notify"]:
            self.notify_sw.select()
        self.notify_sw.grid(row=5, column=0, columnspan=3, sticky="w", padx=14, pady=4)
        sound = ctk.CTkFrame(wn, fg_color="transparent")
        sound.grid(row=6, column=0, columnspan=3, sticky="w", padx=14, pady=4)
        self.sound_sw = ctk.CTkSwitch(sound, text="Тихий звук, когда черновики готовы или нужен вход",
                                      command=self.on_sound_switch)
        if self.conf["sound"]:
            self.sound_sw.select()
        self.sound_sw.pack(side="left")
        ctk.CTkButton(sound, text="▶ Прослушать", width=120, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=play_sound).pack(side="left", padx=12)
        ver = ctk.CTkFrame(wn, fg_color="transparent")
        ver.grid(row=7, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 12))
        ctk.CTkLabel(ver, text=f"Версия {core.VERSION}").pack(side="left")
        ctk.CTkButton(ver, text="Проверить обновления", width=170, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: self.check_updates(manual=True)).pack(side="left", padx=12)
        self.update_check_lbl = ctk.CTkLabel(ver, text="", text_color="gray")
        self.update_check_lbl.pack(side="left")

        # --- продвинутые настройки: большинству не нужны, свёрнуты ---
        self.adv_btn = ctk.CTkButton(sf, text="", anchor="w", fg_color="transparent", hover=False,
                                     text_color=("#1f6aa5", "#5aa9e6"), font=ctk.CTkFont(size=14, weight="bold"),
                                     command=self.toggle_advanced)
        self.adv_btn.pack(fill="x", padx=4, pady=(6, 0))
        adv = self.adv_frame = section("Продвинутые настройки", "Обычно менять не нужно")
        adv.grid_columnconfigure(0, minsize=LABEL_W)
        if builtin:
            key_fields(adv, 2)
        field(adv, 5, "session_name", "Имя файла сессии", "менять не нужно", width=200)
        ctk.CTkLabel(adv, text="Прокси для Telegram").grid(row=6, column=0, sticky="w", padx=14, pady=5)
        self.proxy_mode = ctk.CTkSegmentedButton(adv, values=list(PROXY_MODES),
                                                 command=lambda v: self.show_proxy())
        self.proxy_mode.grid(row=6, column=1, sticky="w", padx=4)
        self.proxy_entry = ctk.CTkEntry(adv, textvariable=self.var("proxy"))
        self.fields["proxy"] = self.proxy_entry
        self.proxy_hint = ctk.CTkLabel(adv, text="", text_color="gray", justify="left", wraplength=640)
        self.proxy_hint.grid(row=8, column=1, columnspan=2, sticky="w", padx=4, pady=(0, 10))
        self.show_advanced()

        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(bottom, text="Сохранить настройки", height=38, width=220,
                      command=self.apply_settings).pack(side="right")
        self.settings_msg = ctk.CTkLabel(bottom, text="", text_color=GREEN)
        self.settings_msg.pack(side="right", padx=12)
        self._settings_msg_job = None
        self.load_settings_form()

    def show_backend(self):
        current = BACKENDS.get(self.backend.get(), "zai")
        for key, f in self.backend_frames.items():
            if key == current:
                f.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
            else:
                f.grid_forget()

    def show_image_backend(self):
        current = IMAGE_BACKENDS.get(self.image_backend.get(), "none")
        for key, f in self.image_frames.items():
            if key == current:
                f.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
            else:
                f.grid_forget()
        if current in ("none", "folder"):
            self.image_common.grid_forget()
        else:
            self.image_common.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.update_img_count()

    def show_hashtags(self):
        own = HASHTAG_NAMES.get(self.post_hashtags.get()) == "own"
        for w in (self.hashtags_row, *self.hashtags_extra):
            w.grid() if own else w.grid_remove()

    def load_settings_form(self):
        c = self.cfg
        for key, v in self.vars.items():
            v.set(str(c.get(key, "")))
        self.channels_box.delete("1.0", "end")
        self.channels_box.insert("1.0", "\n".join(core.channels(c)))
        self.proxy_mode.set(key_of(PROXY_MODES, c.get("proxy_mode", "system")))
        self.show_proxy()
        self.model.set(c["model"])
        self.api_model.set(c.get("api_model") or core.API_MODELS[0])
        self.gemini_model.set(c.get("gemini_model") or DEFAULT_MODEL)
        self.gemini_api_model.set(c.get("gemini_api_model") or core.GEMINI_API_DEFAULT)
        self.codex_model.set(c.get("codex_model") or DEFAULT_MODEL)
        self.openai_api_model.set(c.get("openai_api_model") or core.OPENAI_API_DEFAULT)
        self.compat_model.set(c.get("compat_model") or "")
        self.zai_model.set(c.get("zai_model") or core.ZAI_MODELS[0])
        self.backend.set(key_of(BACKENDS, c.get("backend", "zai")))
        self.show_backend()
        self.update_price()
        self.openai_image_model.set(c.get("openai_image_model") or core.OPENAI_IMAGE_MODELS[0])
        self.gemini_image_model.set(c.get("gemini_image_model") or core.GEMINI_IMAGE_MODELS[0])
        self.zai_image_model.set(c.get("zai_image_model") or core.ZAI_IMAGE_MODELS[0])
        self.image_quality.set(key_of(QUALITY_NAMES, c.get("openai_image_quality")))
        self.image_aspect.set(key_of(ASPECT_NAMES, c.get("image_aspect")))
        self.image_backend.set(key_of(IMAGE_BACKENDS, c.get("image_backend")))
        self.show_image_backend()
        self.post_format.set(key_of(FORMAT_NAMES, c.get("post_format")))
        self.post_emoji.set(key_of(EMOJI_NAMES, c.get("post_emoji")))
        self.post_hashtags.set(key_of(HASHTAG_NAMES, c.get("post_hashtags")))
        self.show_hashtags()
        self.long_text.set(key_of(LONG_NAMES, c.get("long_text")))
        self.typo_sw.select() if c.get("plain_typography") else self.typo_sw.deselect()

    def update_img_count(self):
        if IMAGE_BACKENDS.get(self.image_backend.get()) != "folder":
            return
        n = len(core.list_images(dict(self.cfg, images_dir=self.var("images_dir").get().strip() or "images")))
        self.img_count.configure(text=f"Найдено картинок: {n}" if n else "В папке нет подходящих картинок",
                                 text_color="gray" if n else RED)

    def browse_images(self):
        d = filedialog.askdirectory(title="Папка с картинками", initialdir=core.images_path(self.cfg))
        if d:
            try:
                d = str(Path(d).relative_to(BASE))
            except ValueError:
                pass
            self.var("images_dir").set(d)
            self.update_img_count()

    def open_images(self):
        p = core.images_path(dict(self.cfg, images_dir=self.var("images_dir").get().strip() or "images"))
        p.mkdir(parents=True, exist_ok=True)
        open_path(p)

    def apply_settings(self, silent=False) -> bool:
        f = {k: v.get().strip() for k, v in self.vars.items()}
        new = dict(self.cfg)
        try:
            for k in ("claude_timeout_sec", "image_timeout_sec", "post_max_len", "recent_in_prompt"):
                new[k] = int(f[k])
        except ValueError:
            messagebox.showerror("Настройки", "Длина поста, таймауты и число прошлых постов — целые числа")
            return False
        if not 50 <= new["post_max_len"] <= core.MESSAGE_LIMIT:
            messagebox.showerror("Настройки", f"Длина поста — от 50 до {core.MESSAGE_LIMIT} знаков")
            return False
        new["api_id"] = int(f["api_id"]) if f["api_id"].isdigit() else f["api_id"]
        new["channels"] = core.channels({"channels": self.channels_box.get("1.0", "end").splitlines()})
        new["session_name"] = f["session_name"] or "my_account"
        new["proxy_mode"] = PROXY_MODES.get(self.proxy_mode.get(), "system")
        twin = next((p for p in self.conf["profiles"]
                     if p is not self.cfg and p["session_name"] == new["session_name"]), None)
        if twin:
            messagebox.showerror("Настройки", f"Файл сессии «{new['session_name']}» уже занят профилем "
                                              f"«{twin['name']}» — у каждого аккаунта должен быть свой")
            return False
        for k in ("api_hash", "proxy", "api_key", "gemini_api_key", "openai_api_key", "compat_base_url",
                  "compat_api_key", "zai_api_key", "compat_image_model", "image_style", "hashtags",
                  "post_signature"):
            new[k] = f[k]
        new["images_dir"] = f["images_dir"] or "images"
        new["model"] = self.model.get() or "sonnet"
        new["backend"] = BACKENDS.get(self.backend.get(), "zai")
        new["api_model"] = self.api_model.get()
        gm = self.gemini_model.get().strip()
        new["gemini_model"] = "" if gm in ("", DEFAULT_MODEL) else gm
        new["gemini_api_model"] = self.gemini_api_model.get().strip() or core.GEMINI_API_DEFAULT
        cm = self.codex_model.get().strip()
        new["codex_model"] = "" if cm in ("", DEFAULT_MODEL) else cm
        new["openai_api_model"] = self.openai_api_model.get().strip() or core.OPENAI_API_DEFAULT
        new["compat_model"] = self.compat_model.get().strip()
        new["zai_model"] = self.zai_model.get().strip() or core.ZAI_MODELS[0]
        new["image_backend"] = IMAGE_BACKENDS.get(self.image_backend.get(), "none")
        new["openai_image_model"] = self.openai_image_model.get().strip() or core.OPENAI_IMAGE_MODELS[0]
        new["gemini_image_model"] = self.gemini_image_model.get().strip() or core.GEMINI_IMAGE_MODELS[0]
        new["zai_image_model"] = self.zai_image_model.get().strip() or core.ZAI_IMAGE_MODELS[0]
        new["openai_image_quality"] = QUALITY_NAMES.get(self.image_quality.get(), "auto")
        new["image_aspect"] = ASPECT_NAMES.get(self.image_aspect.get(), "square")
        new["post_format"] = FORMAT_NAMES.get(self.post_format.get(), "basic")
        new["post_emoji"] = EMOJI_NAMES.get(self.post_emoji.get(), "few")
        new["post_hashtags"] = HASHTAG_NAMES.get(self.post_hashtags.get(), "none")
        new["long_text"] = LONG_NAMES.get(self.long_text.get(), "split")
        new["plain_typography"] = bool(self.typo_sw.get())

        # Эти настройки нужны только при подключении к Telegram — на ходу их не сменить
        restart_keys = {"api_id": "API ID", "api_hash": "API hash", "session_name": "файл сессии",
                        "proxy_mode": "прокси", "proxy": "прокси"}
        needs_restart = [] if self.engine.status == "stopped" else list(dict.fromkeys(
            label for k, label in restart_keys.items() if new[k] != self.cfg[k]))
        channels_changed = new["channels"] != self.cfg["channels"]
        self.cfg.update(new)   # тот же словарь, что у движка — изменения применяются сразу
        self.save()
        if channels_changed:
            self.engine.update_channels()
        self.update_channel_menus()
        self.load_settings_form()
        self.render_review()
        if not silent:
            self.welcome.pack_forget()
            if self._settings_msg_job:
                self.after_cancel(self._settings_msg_job)
                self._settings_msg_job = None
            if needs_restart:
                self.settings_msg.configure(
                    text=f"Сохранено ✓  Нужно переподключить аккаунт («Отключить» → «Запустить»), чтобы применить: "
                         f"{', '.join(needs_restart)}", text_color=RED)
            else:
                self.settings_msg.configure(text="Сохранено ✓", text_color=GREEN)
                self._settings_msg_job = self.after(6000, lambda: self.settings_msg.configure(text=""))
        return True

    def describe_agy(self, st):
        if st.get("models"):   # заодно подставляем список моделей аккаунта
            self.gemini_model.configure(values=[DEFAULT_MODEL] + st["models"])
        n = len(st.get("models") or [])
        return (f"Вход через Google выполнен ✓  ·  доступно моделей: {n}  ·  "
                "остаток лимита — команда /usage в окне agy"), GREEN

    def describe_codex(self, st):
        if st.get("models"):
            self.codex_model.configure(values=[DEFAULT_MODEL] + st["models"])
        plan = st.get("plan") or ""
        text = f"Вход в ChatGPT выполнен ✓  {st.get('email', '')}" + (f"  ·  тариф {plan}" if plan else "")
        if st.get("authMethod") == "api":
            return text + "  ·  ⚠ вход по API-ключу — запросы оплачиваются с баланса API", "#e0a030"
        return text, GREEN

    def in_thread(self, work, done):
        """work() в фоне, done(result) — потом в окне."""
        threading.Thread(target=lambda: (r := work(), self.call_soon(lambda: done(r))), daemon=True).start()

    def call_soon(self, fn):
        """Выполнить fn в потоке окна. Можно звать из любого потока: tkinter из чужих потоков
        вызывать нельзя (до запуска mainloop это падает), поэтому кладём в очередь — её разбирает poll()."""
        self.ui_q.put(fn)

    # --- выбор способа подключения к нейросети ---

    def zai_paste_key(self):
        """Ключ Z.ai, скопированный на сайте, сам попадает в поле и проверяется, когда пользователь
        возвращается в окно. Каждое содержимое буфера — один раз: стёртый вручную ключ не вернётся.
        Уже введённый ключ без спроса не заменяем."""
        if "zai" not in (BACKENDS.get(self.backend.get()), IMAGE_BACKENDS.get(self.image_backend.get())):
            return
        try:
            clip = self.clipboard_get().strip()
        except Exception:   # в буфере не текст или он пуст
            return
        if clip == getattr(self, "_zai_clip_seen", None):
            return
        self._zai_clip_seen = clip
        e = self.var("zai_api_key")
        old = e.get().strip()
        if not core.ZAI_KEY_RE.match(clip) or clip == old:
            return
        if old and not messagebox.askyesno("Ключ Z.ai", "В буфере обмена другой ключ Z.ai. "
                                                        "Заменить им ключ этого аккаунта?"):
            return
        e.set(clip)
        log.info("Ключ Z.ai вставлен из буфера обмена")
        self.zai_block.check()

    def check_compat(self):
        cfg = dict(self.cfg, compat_base_url=self.var("compat_base_url").get().strip(),
                   compat_api_key=self.var("compat_api_key").get().strip(),
                   compat_model=self.compat_model.get().strip())
        self.compat_btn.configure(state="disabled")
        self.compat_lbl.configure(text="Проверяю…", text_color="gray")

        def done(res):
            ok, msg, models = res
            self.compat_btn.configure(state="normal")
            self.compat_lbl.configure(text=msg, text_color=GREEN if ok else RED)
            if models:
                self.compat_model.configure(values=models)
                if self.compat_model.get().strip() not in models:
                    self.compat_model.set(models[0])
        self.in_thread(lambda: core.check_compat(cfg), done)

    def on_tray_switch(self):
        self.conf["close_to_tray"] = bool(self.tray_sw.get())
        self.save()

    def on_sound_switch(self):
        self.conf["sound"] = bool(self.sound_sw.get())
        self.save()

    def on_notify_switch(self):
        self.conf["tray_notify"] = bool(self.notify_sw.get())
        self.save()

    def on_auto_update_switch(self):
        self.conf["auto_update"] = bool(self.auto_update_sw.get())
        self.save()

    def on_autostart_switch(self):
        on = bool(self.autostart_sw.get())
        try:
            core.set_autostart(on)
        except OSError as e:
            messagebox.showerror("Автозапуск", f"Не удалось изменить автозапуск Windows: {e}")
            self.autostart_sw.toggle()
            return
        log.info("Автозапуск с Windows %s", "включён" if on else "выключен")

    def show_advanced(self):
        if self.conf.get("show_advanced"):
            self.adv_frame.pack(fill="x", padx=4, pady=6, after=self.adv_btn)
            self.adv_btn.configure(text="▾  Скрыть продвинутые настройки")
        else:
            self.adv_frame.pack_forget()
            self.adv_btn.configure(text="▸  Продвинутые настройки: прокси, ключ Telegram, файл сессии")

    def toggle_advanced(self):
        self.conf["show_advanced"] = not self.conf.get("show_advanced")
        self.save()
        self.show_advanced()

    NOTICE_COLORS = {"info": ("#e3f0ff", "#1d2f44"), "warning": ("#fff4d6", "#463a17"),
                     "error": ("#ffe1e1", "#4a2020")}

    def say(self, kind: str, title: str, text: str):
        """Тихое сообщение над вкладками. Обычное само скрывается, предупреждение и ошибка — висят до ✕."""
        self.notice.configure(fg_color=self.NOTICE_COLORS[kind])
        self.notice_lbl.configure(text=f"{title}:  {text}" if title else text)
        self.notice.pack(fill="x", padx=12, pady=(0, 6), before=self.tabs)
        if self.notice_timer:
            self.after_cancel(self.notice_timer)
        self.notice_timer = self.after(10000, self.hide_notice) if kind == "info" else None

    def hide_notice(self):
        if self.notice_timer:
            self.after_cancel(self.notice_timer)
            self.notice_timer = None
        self.notice.pack_forget()

    def ask(self, title: str, text: str, yes: str, no: str) -> bool:
        """Вопрос «да/нет» своим окном — без звука Windows. Enter — да, Esc — нет."""
        dlg = ctk.CTkToplevel(self)
        dlg.title(title)
        dlg.resizable(False, False)
        dlg.transient(self)
        ctk.CTkLabel(dlg, text=text, justify="left", wraplength=460).pack(padx=20, pady=(18, 12))
        answer = [False]

        def done(value):
            answer[0] = value
            dlg.destroy()
        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(padx=20, pady=(0, 16), fill="x")
        ctk.CTkButton(btns, text=no, width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: done(False)).pack(side="right")
        ctk.CTkButton(btns, text=yes, width=110, command=lambda: done(True)).pack(side="right", padx=8)
        dlg.bind("<Return>", lambda e: done(True))
        dlg.bind("<Escape>", lambda e: done(False))
        dlg.update_idletasks()
        dlg.geometry(f"+{self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2}"
                     f"+{self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3}")
        dlg.after(100, lambda: (dlg.grab_set(), dlg.focus_set()))
        self.wait_window(dlg)
        return answer[0]

    def show_proxy(self):
        mode = PROXY_MODES.get(self.proxy_mode.get(), "system")
        if mode == "custom":
            self.proxy_entry.grid(row=7, column=1, sticky="ew", padx=4, pady=5)
            hint = ("Только для Telegram этого аккаунта: SOCKS5, HTTP или MTProxy (секрет dd…; "
                    "ee… не поддерживается). Нейросети ходят через системный прокси или VPN.")
        else:
            self.proxy_entry.grid_forget()
            sp = core.system_proxy()
            if mode == "none":
                hint = "Подключение напрямую. Подходит, если Telegram не заблокирован или VPN в режиме TUN."
            elif sp and sp.startswith("http"):
                hint = f"Найден системный прокси {sp} — через него пойдут Telegram и нейросети."
            elif sp:
                hint = (f"Найден системный прокси {sp} — через него пойдут Telegram и нейросети по API-ключу. "
                        "Claude Code, Codex и Antigravity понимают только HTTP-прокси — для них нужен VPN в режиме TUN.")
            else:
                hint = ("Системный прокси сейчас не включён — подключение напрямую. Если Telegram заблокирован: "
                        "включите VPN в режиме TUN или в режиме «системный прокси» (Proxy) "
                        "и перезапустите аккаунт.")
        self.proxy_hint.configure(text=hint)


    def update_price(self):
        m = self.api_model.get()
        self.price_lbl.configure(text=f"цена за 1M токенов (вход / выход): {core.API_PRICES.get(m, '?')}"
                                      "  ·  промпт кэшируется")

    # ------------------------------------------------------------------ журнал

    def _build_log_tab(self, tab):
        self.log_box = ctk.CTkTextbox(tab, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_config("WARNING", foreground="#e0a030")
        self.log_box.tag_config("ERROR", foreground=RED)
        self.log_box.configure(state="disabled")
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(6, 0))
        ctk.CTkButton(bottom, text="Очистить", width=110, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=self.clear_log).pack(side="right")
        ctk.CTkButton(bottom, text="Открыть log.txt", width=140, fg_color=GRAY, hover_color=GRAY_HOVER,
                      command=lambda: open_path(BASE / "log.txt")).pack(side="right", padx=8)

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def add_log(self, rec: logging.LogRecord):
        line = f"{time.strftime('%H:%M:%S', time.localtime(rec.created))}  {rec.getMessage()}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line, rec.levelname if rec.levelno >= logging.WARNING else ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.statusbar.configure(text=rec.getMessage().splitlines()[0][:160],
                                 text_color=RED if rec.levelno >= logging.ERROR else "gray")

    # ------------------------------------------------------------------ события

    def on_tab(self):
        tab = self.tabs.get()
        if tab != "Проверка":
            self.flush_text()
        if tab == "Опубликовано":
            self.render_published()
        elif tab == "Запланировано":
            self.render_scheduled()
        elif tab == "Расписание":
            self.update_preview()

    def tick_minute(self):
        """«сегодня 19:00» и ближайшие слоты устаревают со временем — раз в полминуты обновляем подписи."""
        try:
            d = self.current()
            if d and d.status == REVIEW and not d.busy:
                self.show_draft(d)
            if self.tabs.get() == "Запланировано":
                self.render_scheduled()
        except Exception:
            log.exception("Ошибка в окне программы")
        finally:
            self.after(30_000, self.tick_minute)

    def poll(self):
        """Разбирает задания из фоновых потоков, журнал и события движков. Ошибка в одном
        обработчике пишется в журнал и не останавливает остальные — иначе окно застынет."""
        try:
            while not self.ui_q.empty():
                self.guarded(self.ui_q.get_nowait())
            while not self.log_q.empty():
                self.add_log(self.log_q.get_nowait())
            drafts_changed = set()
            for e in list(self.engines.values()):
                while not e.events.empty():
                    kind, data = e.events.get_nowait()
                    if e.cfg["id"] not in self.engines:   # профиль могли удалить
                        continue
                    if kind == "draft":
                        drafts_changed.add(data)
                    else:
                        self.guarded(lambda: self.handle(e, kind, data))
            if drafts_changed:
                self.guarded(lambda: self.on_drafts(drafts_changed))
        finally:
            self.after(100, self.poll)

    def guarded(self, fn):
        try:
            fn()
        except Exception:
            log.exception("Ошибка в окне программы")

    def report_callback_exception(self, exc, val, tb):
        """Ошибки в обработчиках кнопок tkinter — в журнал и log.txt (в .exe без консоли иначе пропадают)."""
        log.error("Ошибка в окне программы", exc_info=(exc, val, tb))

    def on_drafts(self, ids: set[str]):
        finished = False
        for did in ids:
            d = self.store.get(did)
            if not d:
                continue
            if did in self.reserved and (d.status != REVIEW or d.error):
                self.reserved.pop(did, None)
            if d.status == PUBLISHED:
                self.published_count += 1
                self.counter_lbl.configure(text=f"Опубликовано за сессию: {self.published_count}")
            if d.status == REVIEW and not d.busy and d.text.strip():
                finished = True
        # Без полной перерисовки, если меняется только выбранный и список тот же — так не мигает
        listed = {d.id for d in self.review_drafts()}
        if listed == set(self.review_btns):
            for did in ids & listed:
                d = self.store.get(did)
                self.review_btns[did].configure(text=self.list_text(d))
            d = self.current()
            if d and d.id in ids:
                self.show_draft(d)
            self.update_tray()
            self.update_retry_btn()
        else:
            self.render_review()
        if self.tabs.get() == "Опубликовано":
            self.render_published()
        if finished and not any(d.busy for d in self.store.list(REVIEW)):
            if self.conf["sound"]:
                play_sound()
            if self.tray and self.state() == "withdrawn":
                self.tray.notify("Черновики готовы — ждут проверки")

    def handle(self, e: Engine, kind, data):
        own_dialog = self.login_dialog if self.login_dialog and self.login_dialog.engine is e else None
        if kind == "status":
            self.render_status()
            if data == "stopped":
                self.login_waiting = [x for x in self.login_waiting if x != e.cfg["id"]]
                if own_dialog:
                    own_dialog.close()
            self.render_review()
            self.render_scheduled()
        elif kind == "login_needed":
            if self.state() == "withdrawn":   # окно в трее — без него вход не пройти
                self.show_window()
                if self.tray:
                    self.tray.notify(f"Аккаунту «{e.cfg['name']}» нужен вход в Telegram")
            if self.conf["sound"]:
                play_sound()
            self.login_waiting.append(e.cfg["id"])
            self.next_login()
        elif kind in ("qr", "code_sent", "password_needed", "login_error"):
            if own_dialog:
                own_dialog.on_event(kind, data)
        elif kind == "me":
            if own_dialog:
                own_dialog.close()
            self.render_status()
        elif kind == "scheduled":
            self.update_channel_menus()
            self.render_scheduled()
            d = self.current()
            if d and not d.busy:
                self.show_draft(d)
            if self.tabs.get() == "Расписание":
                self.update_preview()

    def on_close(self):
        """Крестик: в трей (программа продолжает работать) или выход — по настройке."""
        if self.tray and self.conf["close_to_tray"]:
            self.flush_text()
            self.withdraw()
            if not self.tray_hinted:
                self.tray_hinted = True
                self.tray.notify("Программа продолжает работать в трее. Открыть — щелчок по значку, "
                                 "выйти — правая кнопка → «Выход».")
            return
        self.quit_app()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def quit_app(self):
        self.show_window()   # вопросы ниже должны быть видны, даже если окно было в трее
        busy = sum(1 for d in self.store.list() if d.busy)
        if busy and not messagebox.askyesno(
                "Выход", f"Нейросеть ещё работает над черновиками: {busy}. Если выйти, их придётся "
                         "перегенерировать. Выйти?"):
            return
        if self.prompts_dirty() and messagebox.askyesno("Промпты", "Сохранить изменения в промптах?"):
            self.save_prompts()
        self.shutdown()
        self.destroy()


def run():
    instance = single_instance()
    if instance is False:   # уже запущена — она сама покажет окно
        return
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    args = sys.argv[1:]
    autostart = core.AUTOSTART_ARG in args
    # После обновления установщик передаёт --run=1 (продолжить работу) и --tray=1 (окно было в трее)
    resume = "--run=1" in args
    try:
        App(hidden=autostart or "--tray=1" in args,
            run_delay=core.AUTO_RUN_DELAY if autostart else 3 if resume else None,
            updated="--updated" in args, instance=instance).mainloop()
    except Exception as e:
        logging.getLogger("autopost").exception("Сбой")
        tk_messagebox.showerror(core.APP_TITLE, f"Программа упала:\n{e}\n\nПодробности в log.txt")
        raise


if __name__ == "__main__":
    run()
