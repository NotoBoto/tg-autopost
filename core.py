"""
Ядро автопостера: настройки, подключение к нейросетям, прокси, промпты и оформление поста.
Работа с Telegram — в engine.py, генерация текста — в textgen.py, картинок — в imagegen.py.
"""
import base64
import html
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from telethon import errors

VERSION = "0.1.0"
REPO = "NotoBoto/tg-autopost"   # отсюда берутся обновления (GitHub Releases)
APP_TITLE = "TG Автопостер"

# Установленная версия (.exe из установщика) хранит данные в %APPDATA% — обновление и
# переустановка их не трогают. Запуск из исходников — всё рядом с app.py.
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))   # файлы внутри программы
BASE = (Path(os.environ.get("APPDATA") or Path.home()) / "TG Autopost") if FROZEN else Path(__file__).parent
BASE.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE / "config.json"
DRAFTS_FILE = BASE / "drafts.json"     # черновики и история публикаций всех профилей
DRAFTS_DIR = BASE / "drafts"           # картинки черновиков: drafts/<id>/…

IMAGE_EXT = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp"}
MODELS = ["sonnet", "opus", "haiku"]          # для Claude Code
API_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
API_PRICES = {  # $ за 1M токенов: вход / выход
    "claude-opus-5-5": "$4 / $20", "claude-sonnet-5-5": "$2 / $10", "claude-haiku-4-5": "$1 / $5"}
GEMINI_API_DEFAULT = "gemini-flash-latest"

# Генерация картинок: модели по умолчанию (поле в настройках редактируемое — можно вписать свою)
OPENAI_IMAGE_MODELS = ["gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini"]
GEMINI_IMAGE_MODELS = ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"]
ZAI_IMAGE_MODELS = ["cogview-4-250304", "cogview-4"]

CAPTION_LIMIT = 1024           # подпись к картинке; у Telegram Premium — 4096
PREMIUM_CAPTION_LIMIT = 4096
MESSAGE_LIMIT = 4096

DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

DEFAULT_SCHEDULE = {
    "mode": "slots",                     # slots — времена по дням недели | interval — каждые N часов в окне
    "slots": {str(d): (["10:00", "19:00"] if d < 5 else ["12:00"]) for d in range(7)},
    "interval_hours": 4,
    "window_from": "09:00",
    "window_to": "22:00",
    "days": list(range(7)),              # дни для режима interval
    "jitter_min": 5,                     # случайный сдвиг ± минут
    "min_gap_min": 60,                   # минимум между постами в канале
}

DEFAULTS = {
    "name": "Основной",            # название профиля (аккаунта) в окне
    "enabled": True,               # подключать ли профиль общей кнопкой «▶ Запустить»
    "api_id": "",                  # своё приложение Telegram; пусто — встроенное (если есть в сборке)
    "api_hash": "",
    "session_name": "my_account",
    "proxy_mode": "system",        # прокси для Telegram: system (из VPN/Windows) | custom | none
    "proxy": "",                   # для custom: socks5://…, http://… или ссылка MTProxy
    "channels": [],                # каналы, куда публикуем
    # --- нейросеть для текста ---
    "backend": "zai",              # zai | claude_code | api (Anthropic) | gemini_cli (Antigravity) | gemini_api
                                   # | codex | openai_api | openai_compat
    "model": "sonnet",
    "api_key": "",
    "api_model": "claude-opus-5-5",
    "gemini_model": "",
    "gemini_api_key": "",
    "gemini_api_model": "gemini-flash-latest",
    "codex_model": "",
    "openai_api_key": "",
    "openai_api_model": "gpt-5.5",
    "compat_base_url": "http://localhost:1234/v1",
    "compat_api_key": "",
    "compat_model": "",
    "zai_api_key": "",
    "zai_model": "glm-4.7-flash",
    "claude_timeout_sec": 180,
    # --- нейросеть для картинки ---
    "image_backend": "openai_api", # codex | openai_api | gemini_api | zai | openai_compat | folder | none
    "openai_image_model": "gpt-image-2",
    "openai_image_quality": "medium",   # low | medium | high | auto
    "gemini_image_model": "gemini-2.5-flash-image",
    "zai_image_model": "cogview-4-250304",
    "compat_image_model": "",
    "images_dir": "images",        # для image_backend = folder
    "image_timeout_sec": 300,
    "image_aspect": "square",      # square | landscape | portrait
    "image_style": "",             # стиль картинки — подставляется в промпт картинки
    # --- промпты ---
    "text_prompt_file": "prompt_text.txt",
    "image_prompt_file": "prompt_image.txt",
    # --- пост ---
    "post_max_len": 900,           # знаков без подписи; до 1024 помещается в подпись к картинке
    "post_format": "basic",        # plain | basic (жирный, курсив) | full (ещё ссылки, цитаты…)
    "post_emoji": "few",           # none | few | many
    "post_hashtags": "none",       # none | own | ai
    "hashtags": "",                # свои хэштеги для post_hashtags = own
    "post_signature": "",          # добавляется программой в конец поста
    "long_text": "split",          # текст длиннее подписи к картинке: split — картинка и текст отдельно | shorten
    "plain_typography": False,     # «—», «…», ёлочки → обычные символы
    "recent_in_prompt": 5,         # сколько последних постов канала показать нейросети, чтобы не повторяться
    # --- расписание ---
    "schedule": DEFAULT_SCHEDULE,
}

log = logging.getLogger("autopost")


# ---------- настройки ----------
# config.json: {"profiles": [профиль, …], "current": id профиля, открытого в окне,
#               "close_to_tray", "auto_update", "tray_notify", "sound", "show_advanced", "resume_profiles"}.
# Профиль — один аккаунт Telegram со своими каналами, нейросетями, промптами и расписанием.

def load_config() -> dict:
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("config.json повреждён (%s) — взяты значения по умолчанию", e)
    profiles = [normalize_profile(p) for p in data.get("profiles") or []] or [normalize_profile({"id": "main"})]
    ids = [p["id"] for p in profiles]
    return {"profiles": profiles, "current": data.get("current") if data.get("current") in ids else ids[0],
            "close_to_tray": data.get("close_to_tray", True),
            "auto_update": data.get("auto_update", True),
            "tray_notify": data.get("tray_notify", False),
            "sound": data.get("sound", True),
            "show_advanced": data.get("show_advanced", False),
            "resume_profiles": data.get("resume_profiles", [])}


def normalize_profile(p: dict) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))   # глубокая копия: расписание — вложенный словарь
    cfg.update(p)
    sched = dict(DEFAULT_SCHEDULE)
    sched.update(cfg.get("schedule") or {})
    cfg["schedule"] = sched
    cfg["channels"] = channels(cfg)
    return cfg


def new_profile(config: dict, base: dict, name: str) -> dict:
    """Новый профиль — копия настроек base, но со своей сессией и своими промптами."""
    pid = secrets.token_hex(3)
    p = json.loads(json.dumps(base))
    p.update(id=pid, name=name, enabled=True, session_name=f"account_{pid}",
             text_prompt_file=f"prompt_text_{pid}.txt", image_prompt_file=f"prompt_image_{pid}.txt")
    for kind in ("text", "image"):
        src = prompt_path(base, kind)
        if src.exists():
            prompt_path(p, kind).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    config["profiles"].append(p)
    return p


def channels(cfg) -> list[str]:
    return list(dict.fromkeys(c for c in (normalize_channel(c) for c in cfg.get("channels") or []) if c))


def save_config(cfg: dict):
    from drafts import write_atomic
    write_atomic(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))


def normalize_channel(s: str) -> str:
    """Канал из списка в одном виде:
    публичный — 'name' (из '@name', 't.me/name', 't.me/name/123');
    закрытый — 't.me/c/<id>' (из ссылки на его пост или id -100…) или 't.me/+<hash>' (ссылка-приглашение)."""
    s = s.strip()
    m = re.search(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([A-Za-z0-9_-]+)", s)
    if m:
        return f"t.me/+{m.group(1)}"
    m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?c/(\d+)", s)
    if m:
        return f"t.me/c/{m.group(1)}"
    m = re.fullmatch(r"-100(\d+)", s)
    if m:
        return f"t.me/c/{m.group(1)}"
    m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]+)", s)
    if m:
        return m.group(1)
    return s.lstrip("@")


def channel_label(c: str) -> str:
    """Как показать канал из списка: '@name' у публичного, ссылка — у закрытого."""
    return c if c.startswith("t.me/") else "@" + c


def prompt_path(cfg, kind: str) -> Path:
    """kind — text или image."""
    return BASE / cfg[f"{kind}_prompt_file"]


def read_prompt(cfg, kind: str) -> str:
    """Промпт профиля; если его ещё нет — пример из программы."""
    p = prompt_path(cfg, kind)
    if p.exists():
        return p.read_text(encoding="utf-8")
    example = RES / f"prompt_{kind}.example.txt"
    return example.read_text(encoding="utf-8") if example.exists() else ""


def images_path(cfg) -> Path:
    p = Path(cfg["images_dir"])
    return p if p.is_absolute() else BASE / p


def list_images(cfg) -> list[Path]:
    folder = images_path(cfg)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT)


# ---------- нейросети, вход, прокси, автозапуск ----------

# Сюда ставит официальный установщик; PATH у уже запущенной программы об этом не знает
NATIVE_CLAUDE = Path.home() / ".local" / "bin" / "claude.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_claude() -> str | None:
    # На Windows предпочитаем настоящий .exe, а не обёртку .cmd
    found = shutil.which("claude.exe") or shutil.which("claude")
    if found:
        return found
    return str(NATIVE_CLAUDE) if NATIVE_CLAUDE.exists() else None


def claude_status() -> dict:
    """{'installed', 'loggedIn', 'email', 'authMethod', 'subscriptionType'}"""
    exe = find_claude()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "auth", "status", "--json"], capture_output=True,
                           timeout=30, env=cli_env(), creationflags=NO_WINDOW)
        info = json.loads(decode_any(r.stdout))
    except Exception:
        info = {"loggedIn": False}
    info["installed"] = True
    return info


def api_key(cfg) -> str | None:
    return str(cfg.get("api_key", "")).strip() or os.environ.get("ANTHROPIC_API_KEY")


def check_api_key(cfg) -> tuple[bool, str]:
    """Проверка ключа без траты токенов: запрашиваем описание выбранной модели."""
    import anthropic
    key = api_key(cfg)
    if not key:
        return False, "Ключ не указан"
    try:
        m = anthropic.Anthropic(api_key=key, timeout=20, max_retries=1).models.retrieve(
            cfg.get("api_model", API_MODELS[0]))
        return True, f"Ключ работает ✓  модель {m.display_name} доступна"
    except anthropic.AuthenticationError:
        return False, "Ключ не подходит"
    except anthropic.NotFoundError:
        return False, "Ключ работает, но модель недоступна для этого аккаунта"
    except anthropic.APIConnectionError:
        return False, "Нет связи с API — проверьте интернет"
    except anthropic.APIStatusError as e:
        return False, f"Ошибка API ({e.status_code}): {e.message}"


def install_claude() -> tuple[bool, str]:
    """Официальный установщик Claude Code для Windows (Node.js не нужен)."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "irm https://claude.ai/install.ps1 | iex"],
        capture_output=True, timeout=600, env=cli_env(), creationflags=NO_WINDOW)
    out = (decode_any(r.stdout) + "\n" + decode_any(r.stderr)).strip()
    return find_claude() is not None, out


def open_claude_login():
    """Открывает вход в отдельном окне консоли: там ссылка/браузер и, если нужно, поле для кода."""
    subprocess.Popen([find_claude(), "auth", "login", "--claudeai"],
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def _builtin_telegram_app() -> tuple[int, str] | None:
    """Встроенное приложение Telegram. Модуль _builtin_api.py создаёт build.py из секретов
    сборки; в репозитории его нет. Значения в нём замаскированы — только от поиска строк в .exe."""
    try:
        from _builtin_api import BLOB, MASK
        api_id, api_hash = bytes(b ^ MASK[i % len(MASK)] for i, b in enumerate(BLOB)).decode().split(":")
        return int(api_id), api_hash
    except Exception:
        return None


BUILTIN_TELEGRAM_APP = _builtin_telegram_app()


def telegram_app(cfg) -> tuple[int, str] | None:
    """(api_id, api_hash) для подключения: свои из настроек, если указаны, иначе встроенные."""
    own_id, own_hash = str(cfg.get("api_id", "")).strip(), str(cfg.get("api_hash", "")).strip()
    if own_id or own_hash:
        return (int(own_id), own_hash) if own_id.isdigit() and own_hash else None
    return BUILTIN_TELEGRAM_APP


# Telegram не принял ключ приложения: неверный или заблокирован (ключ попал в открытый доступ)
API_KEY_ERRORS = (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError)


def api_key_error(cfg) -> str:
    """Понятное объяснение, когда Telegram отверг API ID / API Hash, — свои или встроенные."""
    if str(cfg.get("api_id", "")).strip() or str(cfg.get("api_hash", "")).strip():
        return ("Telegram не принял ваши API ID и API Hash — проверьте их в «Настройках»"
                + (" или очистите оба поля, чтобы работать со встроенным ключом" if BUILTIN_TELEGRAM_APP else ""))
    return ("Telegram не принял встроенный ключ программы. Обновите программу («Настройки» → «Программа» → "
            "«Проверить обновления») или укажите свои API ID и API Hash с my.telegram.org")


# ---------- прокси ----------
# Где Telegram и нейросети заблокированы, нужен VPN. В режиме TUN он программе не мешает,
# а в режиме «системный прокси» его надо передать явно: Telethon и CLI нейросетей
# настройки прокси Windows сами не читают (Python-SDK API читают — через httpx).

def system_proxy() -> str | None:
    """Прокси из переменных окружения или настроек Windows (его выставляет VPN-клиент)."""
    from urllib.request import getproxies
    p = getproxies()
    return p.get("https") or p.get("http") or p.get("socks") or p.get("all")


def parse_proxy(s: str) -> dict:
    """'socks5://user:pass@host:port', 'http://host:port', 'host:port' (= http)
    или ссылка MTProxy t.me/proxy?server=…&port=…&secret=… . Ошибка — ValueError с понятным текстом."""
    from urllib.parse import parse_qs, unquote, urlsplit
    s = s.strip()
    if "proxy?" in s:
        q = {k: v[0] for k, v in parse_qs(urlsplit(s).query).items()}
        try:
            host, port, secret = q["server"], int(q["port"]), q["secret"]
        except (KeyError, ValueError):
            raise ValueError("В ссылке MTProxy нет server, port или secret")
        try:
            raw = bytes.fromhex(secret)
        except ValueError:
            raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        if raw[:1] == b"\xee":
            raise ValueError("MTProxy с секретом ee… (FakeTLS) не поддерживается — "
                             "нужен прокси с секретом dd… или SOCKS5/HTTP")
        return {"type": "mtproxy", "host": host, "port": port, "secret": raw.hex()}
    u = urlsplit(s if "://" in s else "http://" + s)
    kind = {"socks": "socks5", "socks5h": "socks5", "https": "http"}.get(u.scheme.lower(), u.scheme.lower())
    if kind not in ("socks5", "socks4", "http"):
        raise ValueError(f"Неизвестный тип прокси «{u.scheme}» — нужен socks5://, http:// или ссылка MTProxy")
    try:
        port = u.port
    except ValueError:
        port = None
    if not u.hostname or not port:
        raise ValueError("В адресе прокси нет хоста или порта (пример: socks5://127.0.0.1:10808)")
    return {"type": kind, "host": u.hostname, "port": port,
            "user": unquote(u.username) if u.username else None,
            "password": unquote(u.password) if u.password else None}


def telegram_proxy(cfg) -> dict | None:
    """Прокси для Telegram по настройке профиля; None — подключаемся напрямую."""
    mode = cfg.get("proxy_mode", "system")
    if mode == "none":
        return None
    s = str(cfg.get("proxy", "")).strip() if mode == "custom" else system_proxy()
    return parse_proxy(s) if s else None


def proxy_label(p: dict | None) -> str:
    """Адрес прокси для журнала — без логина и пароля."""
    return f"{p['type']}://{p['host']}:{p['port']}" if p else "напрямую"


def cli_env(drop=()) -> dict:
    """Окружение для CLI нейросетей и установщиков: системный HTTP-прокси передаём переменными."""
    env = {k: v for k, v in os.environ.items() if k not in drop}
    if not any(k.upper() == "HTTPS_PROXY" for k in env):
        p = system_proxy()
        if p and p.startswith("http"):
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = p
            env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")   # вход через браузер идёт на localhost
    return env


# ---------- автозапуск с Windows ----------
# Запись в HKCU\…\Run: только для текущего пользователя, прав администратора не нужно.

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "TG Autopost"
AUTOSTART_ARG = "--autostart"    # с ним окно стартует в трее и через AUTO_RUN_DELAY сек начинает работу
AUTO_RUN_DELAY = 30              # время на подключение VPN и сети после входа в Windows


def autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}" {AUTOSTART_ARG}'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")   # без чёрного окна консоли
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).parent / "app.py"}" {AUTOSTART_ARG}'


def get_autostart() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0]
    except OSError:
        return None


def set_autostart(on: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


# Так сервисы отвечают на запрос из страны, где они не работают
REGION_MARKERS = ("not currently available in your location", "location is not supported",
                  "unsupported_country", "unsupported country", "not available in your country",
                  "region, or territory not supported", "request not allowed")


def explain_ai_error(name: str, msg: str) -> str:
    """Текст ошибки нейросети для журнала и очереди; отказ по стране — с подсказкой про VPN."""
    msg = msg.removeprefix(f"{name}: ").strip()
    text = msg if msg.startswith(f"Ошибка {name}") else f"Ошибка {name}: {msg}"
    if any(m in msg.lower() for m in REGION_MARKERS):
        sp = system_proxy()
        route = f"через системный прокси {sp}" if sp else "без системного прокси (через VPN в режиме TUN или напрямую)"
        text += (f"\n{name} недоступен из страны, откуда пришёл запрос. Включите VPN с сервером там, где сервис "
                 f"работает (например, США или Европа) — в режиме TUN или «системный прокси» — и повторите. "
                 f"Сейчас запросы идут {route}. Не помогает — попробуйте другой сервер VPN или другой "
                 "способ подключения в «Настройках». Без VPN работает «Z.ai (бесплатно)».")
    return text


def decode_any(data: bytes) -> str:
    """Windows-консоль может отдавать ошибки в cp866/cp1251 — пробуем все."""
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ---------- Antigravity CLI (Google, преемник Gemini CLI) ----------
# С 18.06.2026 Gemini CLI не обслуживает личные Google-аккаунты — вместо него agy.

AGY_DEFAULT = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
AGY_AGENT = "autopost"


def find_agy() -> str | None:
    found = shutil.which("agy.exe") or shutil.which("agy")
    if found:
        return found
    return str(AGY_DEFAULT) if AGY_DEFAULT.exists() else None


def agy_status() -> dict:
    """`agy models` без входа сразу отвечает «Please sign in», после входа — списком моделей."""
    exe = find_agy()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "models"], capture_output=True, timeout=40,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        out = decode_any(r.stdout) + decode_any(r.stderr)
    except subprocess.TimeoutExpired:
        return {"installed": True, "loggedIn": False}
    if "sign in" in out.lower() or "authentication required" in out.lower():
        return {"installed": True, "loggedIn": False}
    # Строки вида «gemini-3.8-flash-medium<TAB>Gemini 3.8 Flash (Medium)»; есть и Claude, и GPT
    models = [line.split("\t")[0].strip() for line in out.splitlines() if "\t" in line]
    return {"installed": True, "loggedIn": r.returncode == 0, "models": list(dict.fromkeys(models))}


def install_agy() -> tuple[bool, str]:
    """Официальный установщик Antigravity CLI для Windows."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "irm https://antigravity.google/cli/install.ps1 | iex"],
        capture_output=True, timeout=600, env=cli_env(), creationflags=NO_WINDOW)
    out = (decode_any(r.stdout) + "\n" + decode_any(r.stderr)).strip()
    return find_agy() is not None, out


def open_agy_login():
    """Первый запуск agy без аргументов открывает браузер для входа в Google-аккаунт."""
    subprocess.Popen([find_agy()], cwd=str(Path.home()),
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


# ---------- Gemini API ----------

def gemini_key(cfg) -> str | None:
    return (str(cfg.get("gemini_api_key", "")).strip()
            or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def check_gemini_key(cfg) -> tuple[bool, str, list[str]]:
    """Проверка ключа без траты токенов: получаем список доступных моделей."""
    from google import genai
    from google.genai import errors as gerr
    key = gemini_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    try:
        client = genai.Client(api_key=key)
        models = sorted(m.name.removeprefix("models/") for m in client.models.list()
                        if "generateContent" in (m.supported_actions or []) and "gemini" in m.name)
    except gerr.APIError as e:
        if e.code in (400, 401, 403):
            return False, "Ключ не подходит", []
        return False, f"Ошибка API ({e.code}): {e.message}", []
    except Exception as e:
        return False, f"Нет связи с Gemini API: {e}", []
    model = cfg.get("gemini_api_model") or GEMINI_API_DEFAULT
    if model not in models:
        return True, f"Ключ работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Ключ работает ✓  доступно моделей: {len(models)}", models


# ---------- ChatGPT: Codex CLI по подписке и OpenAI API по ключу ----------

NPM_GLOBAL = Path(os.environ.get("APPDATA", "")) / "npm"
NODE_DEFAULT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs"
CODEX_DIR = Path.home() / ".codex"
# Лишние для комментариев возможности Codex: меньше служебного текста — меньше расход лимита
CODEX_FEATURES_OFF = ["apps", "plugins", "multi_agent", "image_generation", "browser_use",
                      "computer_use", "goals", "shell_tool", "unified_exec"]
OPENAI_API_DEFAULT = "gpt-5.5"


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    exe = NODE_DEFAULT / "node.exe"
    return str(exe) if exe.exists() else None


def find_codex() -> str | None:
    """Запускаем сам codex.exe из npm-пакета, минуя codex.cmd: cmd.exe портит кириллицу в аргументах."""
    roots = [NPM_GLOBAL / "node_modules" / "@openai"]
    if shutil.which("codex"):
        roots.append(Path(shutil.which("codex")).parent / "node_modules" / "@openai")
    for root in roots:
        for pattern in ("codex/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe",
                        "codex-win32-*/vendor/*/bin/codex.exe"):
            found = sorted(root.glob(pattern))
            if found:
                return str(found[0])
    return None


def _codex_account() -> dict:
    """Почта и тариф из id_token в ~/.codex/auth.json (сам токен никуда не передаём)."""
    try:
        auth = json.loads((CODEX_DIR / "auth.json").read_text(encoding="utf-8"))
        payload = auth["tokens"]["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        plan = claims.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type", "")
        return {"email": claims.get("email", ""), "plan": plan}
    except Exception:
        return {}


def codex_status() -> dict:
    exe = find_codex()
    if not exe:
        return {"installed": False, "loggedIn": False}
    try:
        r = subprocess.run([exe, "login", "status"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        out = (decode_any(r.stdout) + decode_any(r.stderr)).lower()
    except Exception:
        return {"installed": True, "loggedIn": False}
    if r.returncode != 0 or "not logged in" in out:
        return {"installed": True, "loggedIn": False}
    st = {"installed": True, "loggedIn": True,
          "authMethod": "chatgpt" if "chatgpt" in out else "api", **_codex_account()}
    try:   # модели, доступные этому аккаунту
        r = subprocess.run([exe, "debug", "models"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=cli_env(), creationflags=NO_WINDOW)
        data = json.loads(decode_any(r.stdout))
        st["models"] = [m["slug"] for m in data.get("models", data)
                        if m.get("visibility") == "list" and "image" in (m.get("input_modalities") or [])]
    except Exception:
        pass
    return st


def install_codex() -> tuple[bool, str]:
    """Node.js (если нет) через winget, затем Codex CLI через npm."""
    out = ""
    if not find_node():
        r = subprocess.run(["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"],
                           capture_output=True, timeout=900, env=cli_env(), creationflags=NO_WINDOW)
        out += decode_any(r.stdout) + decode_any(r.stderr)
        if not find_node():
            return False, out + "\nНе удалось установить Node.js"
    npm = Path(find_node()).parent / "npm.cmd"
    r = subprocess.run([str(npm), "install", "-g", "@openai/codex"], capture_output=True,
                       timeout=900, env=cli_env(), creationflags=NO_WINDOW)
    out += decode_any(r.stdout) + decode_any(r.stderr)
    return find_codex() is not None, out.strip()


def open_codex_login():
    """Вход через аккаунт ChatGPT. Если уже вошли — сначала выходим, чтобы сменить аккаунт."""
    exe = find_codex()
    if codex_status().get("loggedIn"):
        subprocess.run([exe, "logout"], capture_output=True, timeout=30, env=cli_env(), creationflags=NO_WINDOW)
    subprocess.Popen([exe, "login"], cwd=str(Path.home()),
                     env=cli_env(), creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def openai_key(cfg) -> str | None:
    return str(cfg.get("openai_api_key", "")).strip() or os.environ.get("OPENAI_API_KEY")


def check_openai_key(cfg) -> tuple[bool, str, list[str]]:
    """Проверка ключа без траты токенов: получаем список доступных моделей."""
    import openai
    key = openai_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    try:
        client = openai.OpenAI(api_key=key, timeout=20, max_retries=1)
        models = sorted(m.id for m in client.models.list()
                        if m.id.startswith(("gpt-", "o")) and not any(
                            x in m.id for x in ("audio", "realtime", "tts", "transcribe", "image", "embedding", "search")))
    except openai.AuthenticationError:
        return False, "Ключ не подходит", []
    except openai.APIConnectionError:
        return False, "Нет связи с OpenAI API — проверьте интернет", []
    except openai.APIStatusError as e:
        return False, f"Ошибка API ({e.status_code}): {e.message}", []
    model = cfg.get("openai_api_model") or OPENAI_API_DEFAULT
    if model not in models:
        return True, f"Ключ работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Ключ работает ✓  доступно моделей: {len(models)}", models


# ---------- своя модель: любой OpenAI-совместимый сервер ----------
# LM Studio, Ollama, llama.cpp, vLLM — локально; OpenRouter, DeepSeek и т. п. — в облаке.
# Запросы — классическим chat/completions: его понимают все такие серверы.

COMPAT_PRESETS = {"LM Studio": "http://localhost:1234/v1", "Ollama": "http://localhost:11434/v1"}


def is_local_url(url: str) -> bool:
    """Сервер на этом компьютере или в домашней сети — к нему ходим мимо системного прокси (VPN)."""
    import ipaddress
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def compat_client(cfg, asynchronous=False):
    import httpx
    import openai
    url = str(cfg.get("compat_base_url", "")).strip().rstrip("/")
    key = str(cfg.get("compat_api_key", "")).strip() or "not-needed"   # локальным серверам ключ не нужен
    local = is_local_url(url)
    if asynchronous:
        http = httpx.AsyncClient(trust_env=False) if local else None
        return openai.AsyncOpenAI(base_url=url, api_key=key, http_client=http, max_retries=1)
    http = httpx.Client(trust_env=False) if local else None
    return openai.OpenAI(base_url=url, api_key=key, http_client=http, timeout=20, max_retries=0)


def chat_content(user_msg: str, images: list[Path]) -> str | list:
    """Сообщение для chat/completions: текст, а с картинками — текст и картинки в data:-URL."""
    if not images:
        return user_msg
    content = [{"type": "text", "text": user_msg + "\n\nК посту приложены картинки — учти, что на них. "
                                                   "Выведи только сам комментарий."}]
    for img in images:
        mime = mimetypes.guess_type(img.name)[0] or "image/jpeg"
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"}})
    return content


def compat_connection_hint(url: str) -> str:
    if is_local_url(url):
        return (f"Нет связи с {url} — запущен ли сервер? LM Studio: вкладка Developer → Start Server; "
                "Ollama: программа должна быть запущена")
    return f"Нет связи с {url} — проверьте адрес и интернет (или VPN)"


def check_compat(cfg) -> tuple[bool, str, list[str]]:
    """Проверка подключения без генерации: запрашиваем список моделей сервера."""
    import openai
    url = str(cfg.get("compat_base_url", "")).strip()
    if not url:
        return False, "Адрес сервера не указан", []
    try:
        with compat_client(cfg) as client:
            models = sorted(m.id for m in client.models.list())
    except openai.APIConnectionError:
        return False, compat_connection_hint(url), []
    except openai.AuthenticationError:
        return False, "Сервер требует API-ключ (или ключ не подходит)", []
    except openai.NotFoundError:
        return False, "По этому адресу нет OpenAI-совместимого API — адрес обычно оканчивается на /v1", []
    except openai.APIStatusError as e:
        return False, f"Ошибка сервера ({e.status_code}): {e.message}", []
    except Exception as e:
        return False, f"Не удалось подключиться: {e}", []
    if not models:
        return True, "Сервер работает ✓, но моделей нет — загрузите модель (в LM Studio — «Load model»)", []
    model = str(cfg.get("compat_model", "")).strip()
    if model and model not in models:
        return True, f"Сервер работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Сервер работает ✓  моделей: {len(models)}", models


# ---------- Z.ai (GLM): бесплатные модели, работает из России без VPN и без карты ----------
# API OpenAI-совместимый. Бесплатные модели отвечают по одному запросу за раз на ключ.

ZAI_URL = "https://api.z.ai/api/paas/v4"
ZAI_KEYS_URL = "https://z.ai/manage-apikey/apikey-list"
ZAI_MODELS = ["glm-4.7-flash", "glm-4.5-flash"]   # бесплатные текстовые — по порядку предпочтения
ZAI_VISION_MODEL = "glm-4.6v-flash"              # бесплатная, видит картинки поста
ZAI_KEY_RE = re.compile(r"^[0-9A-Za-z]{24,64}\.[0-9A-Za-z]{8,32}$")   # так выглядит ключ: «a1b2…f9.XyZ…»
ZAI_RETRY_DELAYS = (5, 15, 30)   # паузы перед повтором, когда Z.ai занят или сбоит
ZAI_CHECK_WAIT = 120             # сколько проверка ключа ждёт, пока аккаунты допишут свои запросы, сек
_zai_locks: dict[str, threading.Lock] = {}   # по ключу: профили работают каждый в своём потоке
_zai_locks_guard = threading.Lock()
_zai_gone: set[str] = set()          # модели, которые Z.ai перестал узнавать (код 1211) — дальше их пропускаем
_zai_no_thinking: set[str] = set()   # модели, которые не приняли параметр thinking — шлём без него


def zai_key(cfg) -> str | None:
    return str(cfg.get("zai_api_key", "")).strip() or os.environ.get("ZAI_API_KEY")


def zai_lock(key: str) -> threading.Lock:
    with _zai_locks_guard:
        return _zai_locks.setdefault(key, threading.Lock())


def zai_client(key: str, asynchronous=False):
    import openai
    if asynchronous:
        return openai.AsyncOpenAI(base_url=ZAI_URL, api_key=key, max_retries=0)   # повторы — свои, с паузами
    return openai.OpenAI(base_url=ZAI_URL, api_key=key, timeout=30, max_retries=0)


def zai_extra(model: str) -> dict | None:
    """Для комментария думать незачем — без «размышлений» быстрее. Не принявшим параметр — не шлём."""
    return None if model in _zai_no_thinking else {"thinking": {"type": "disabled"}}


def zai_thinking_rejected(model: str, status: int, msg: str) -> bool:
    """Модель не приняла thinking — запоминаем, чтобы повторить без него. True — стоит повторить."""
    if status == 400 and "thinking" in msg.lower() and model not in _zai_no_thinking:
        _zai_no_thinking.add(model)
        return True
    return False


def zai_error(e) -> tuple[str, str]:
    """Код и текст ошибки Z.ai: {"error": {"code": "1302", "message": "…"}}."""
    body = getattr(e, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    if not isinstance(body, dict):
        body = {}
    return str(body.get("code", "")), str(body.get("message") or getattr(e, "message", "") or e)


ZAI_BAD_KEY = "Ключ Z.ai не подходит — скопируйте его заново на z.ai («Получить бесплатный ключ»)"
ZAI_BUSY_CODES = {"1302", "1305", "1313"}            # «слишком часто» и «перегружен» — подождать и повторить
ZAI_LIMIT_CODES = {"1308", "1310", "1316", "1317"}   # исчерпан лимит за период — повтор не поможет


def zai_busy(status: int, code: str) -> bool:
    """Временная занятость или сбой Z.ai — стоит подождать и повторить."""
    return code in ZAI_BUSY_CODES or status >= 500 or (status == 429 and not code)


def zai_error_text(status: int, code: str, msg: str, model: str) -> str:
    if code in ZAI_LIMIT_CODES:
        return f"Исчерпан бесплатный лимит Z.ai — {msg}"
    choose = (f"выберите бесплатную {ZAI_MODELS[0]} в «Настройках»" if model not in ZAI_MODELS
              else "похоже, бесплатной она больше не является — выберите другую модель в «Настройках»")
    if code == "1113":
        return f"Z.ai просит пополнить баланс для модели {model} — {choose}"
    if code == "1311":
        return f"Модель {model} не входит в ваш тариф Z.ai — {choose}"
    if code == "1211":
        return f"Z.ai не знает модель {model} — выберите другую в «Настройках»"
    if code == "1301":
        return "Z.ai отказался отвечать: посчитал пост или промпт недопустимым"
    return f"Ошибка Z.ai ({status}): {msg}"


def zai_models_order(model: str) -> list[str]:
    """Выбранная модель, за ней остальные бесплатные — на случай, если Z.ai какую-то отключил."""
    return [m for m in dict.fromkeys([model, *ZAI_MODELS]) if m not in _zai_gone] or [model]


def check_zai_key(cfg) -> tuple[bool, str, list[str]]:
    """Проверка ключа крошечным запросом к модели — для бесплатной ничего не стоит.
    Возвращает список моделей, первой — рабочую (её и выберет окно)."""
    import openai
    key = zai_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    model = str(cfg.get("zai_model", "")).strip() or ZAI_MODELS[0]
    models = zai_models_order(model)
    lock = zai_lock(key)   # бесплатно — по одному запросу на ключ: не мешаем работающим аккаунтам
    if not lock.acquire(timeout=ZAI_CHECK_WAIT):
        return False, "Z.ai занят запросами аккаунтов — проверьте ключ чуть позже", []
    try:
        with zai_client(key) as client:
            while True:
                try:
                    client.chat.completions.create(
                        model=models[0], max_tokens=1, messages=[{"role": "user", "content": "Привет"}],
                        extra_body=zai_extra(models[0]))
                    break
                except openai.BadRequestError as e:
                    code, msg = zai_error(e)
                    if code == "1211" and len(models) > 1:
                        _zai_gone.add(models.pop(0))
                    elif not zai_thinking_rejected(models[0], e.status_code, msg):
                        raise
    except openai.AuthenticationError:
        return False, ZAI_BAD_KEY, []
    except openai.APIConnectionError:
        return False, "Нет связи с Z.ai — проверьте интернет", []
    except openai.APIStatusError as e:
        code, msg = zai_error(e)
        if code in ZAI_BUSY_CODES:   # до лимита дошли — значит, ключ рабочий
            return True, "Ключ работает ✓ (Z.ai сейчас занят) — нажмите «Сохранить»", models
        return False, zai_error_text(e.status_code, code, msg, models[0]), []
    finally:
        lock.release()
    if models[0] != model:
        return True, f"Ключ работает ✓, но модели {model} больше нет — выбрана {models[0]}. " \
                     "Нажмите «Сохранить»", models
    return True, "Ключ работает ✓ — нажмите «Сохранить»", models



VARIANTS_RE = re.compile(r"\{\{(.+?)\}\}", re.S)


def pick_variants(prompt: str) -> str:
    """{{вариант 1 | вариант 2 | …}} в промпте — к каждому запросу случайный вариант. Слабые модели
    из списка приёмов всегда берут один и тот же, а так комментарии получаются разными."""
    return VARIANTS_RE.sub(lambda m: random.choice(m.group(1).split("|")).strip(), prompt)


# Символы, которых нет на клавиатуре телефона, — сразу выдают нейросеть. Меняем на то, что напечатал бы человек.
HUMAN_TYPOGRAPHY = str.maketrans({"—": "-", "–": "-", "−": "-", "«": '"', "»": '"', "„": '"', "“": '"',
                                  "”": '"', "’": "'", "‘": "'", "…": "...", "×": "x",
                                  "\u00a0": " ", "\u202f": " ", "\u2009": " "})


def human_typography(text: str) -> str:
    """Длинные тире, ёлочки, многоточие одним знаком, × и неразрывные пробелы — в обычные символы;
    жирный markdown (**…**) Telegram показал бы звёздочками — убираем."""
    return text.translate(HUMAN_TYPOGRAPHY).replace("**", "") if text else text



THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)   # «размышления» DeepSeek-R1, Qwen3 и т. п.


def strip_reasoning(text: str) -> str:
    """Убирает «размышления» модели из ответа. Незакрытый <think> — ответ обрезан, публиковать нельзя."""
    text = THINK_RE.sub("", text)
    low = text.lower()
    if "</think>" in low:   # открывающий тег бывает в шаблоне модели, а в ответе только закрывающий
        text = text[low.rindex("</think>") + len("</think>"):]
    elif "<think>" in low:
        raise RuntimeError("Модель не закончила размышлять — ответ обрезан. Увеличьте лимит длины ответа "
                           "и контекст модели или возьмите модель без режима размышлений")
    return text.strip()


# ---------- что мешает работе ----------

TEXT_BACKEND_NAMES = {"zai": "Z.ai", "claude_code": "Claude Code", "api": "Claude API", "gemini_cli": "Antigravity",
                      "gemini_api": "Gemini API", "codex": "Codex", "openai_api": "OpenAI API",
                      "openai_compat": "Своя модель"}
IMAGE_BACKEND_NAMES = {"openai_api": "OpenAI API", "gemini_api": "Gemini API", "codex": "ChatGPT (Codex)",
                       "zai": "Z.ai", "openai_compat": "Своя модель", "folder": "Из папки",
                       "none": "Без картинки"}
BACKEND_NAMES = TEXT_BACKEND_NAMES   # для explain_ai_error и журнала


def validate_telegram(cfg) -> list[str]:
    """Что мешает подключиться к Telegram и публиковать."""
    problems = []
    if not telegram_app(cfg):
        own_id, own_hash = str(cfg.get("api_id", "")).strip(), str(cfg.get("api_hash", "")).strip()
        if own_id or own_hash:
            problems.append("Укажите и API ID (число), и API Hash — или очистите оба поля, "
                            "чтобы работать со встроенным ключом" if BUILTIN_TELEGRAM_APP else
                            "Укажите и API ID (число), и API Hash с my.telegram.org")
        else:
            problems.append("Не указаны API ID и API Hash (с my.telegram.org) — «Настройки» → «Продвинутые»")
    if not channels(cfg):
        problems.append("Не указан канал, куда публиковать — «Настройки» → «Telegram»")
    if cfg.get("proxy_mode") == "custom" and not str(cfg.get("proxy", "")).strip():
        problems.append("Выбран свой прокси, но адрес не указан")
    try:
        telegram_proxy(cfg)
    except ValueError as e:
        problems.append(f"Прокси: {e}")
    return problems


def validate_text(cfg) -> list[str]:
    """Что мешает писать текст поста. Для CLI запускает их проверку — до десятков секунд, звать в фоне."""
    problems = []
    if not read_prompt(cfg, "text").strip():
        problems.append("Промпт текста пустой — вкладка «Промпты»")
    backend = cfg.get("backend")
    if backend == "api":
        if not api_key(cfg):
            problems.append("Не указан API-ключ Anthropic — «Настройки» → «Нейросеть для текста»")
    elif backend == "gemini_api":
        if not gemini_key(cfg):
            problems.append("Не указан API-ключ Gemini — «Настройки» → «Нейросеть для текста»")
    elif backend == "openai_api":
        if not openai_key(cfg):
            problems.append("Не указан API-ключ OpenAI — «Настройки» → «Нейросеть для текста»")
    elif backend == "zai":
        if not zai_key(cfg):
            problems.append("Не указан ключ Z.ai — «Настройки» → «Нейросеть для текста» → «Получить бесплатный ключ»")
    elif backend == "openai_compat":
        if not str(cfg.get("compat_base_url", "")).strip():
            problems.append("Не указан адрес сервера своей модели — «Настройки» → «Нейросеть для текста»")
        if not str(cfg.get("compat_model", "")).strip():
            problems.append("Не выбрана модель — «Настройки» → «Нейросеть для текста» → «Проверить подключение»")
    elif backend == "codex":
        problems += _cli_problems(codex_status(), "Codex CLI", "ChatGPT")
    elif backend == "gemini_cli":
        problems += _cli_problems(agy_status(), "Antigravity CLI", "Google")
    else:
        problems += _cli_problems(claude_status(), "Claude Code", "Claude")
    return problems


def _cli_problems(st: dict, name: str, account: str) -> list[str]:
    if not st["installed"]:
        return [f"Не установлен {name} — «Настройки» → «Установить и войти»"]
    if not st.get("loggedIn"):
        return [f"Не выполнен вход в {account} — «Настройки» → «Войти в {account}»"]
    return []


def validate_image(cfg) -> list[str]:
    """Что мешает получить картинку к посту."""
    backend = cfg.get("image_backend")
    if backend == "none":
        return []
    if backend == "folder":
        return [] if list_images(cfg) else ["В папке картинок пусто — «Настройки» → «Картинка»"]
    problems = []
    if not read_prompt(cfg, "image").strip():
        problems.append("Промпт картинки пустой — вкладка «Промпты»")
    if backend == "openai_api" and not openai_key(cfg):
        problems.append("Для картинок нужен API-ключ OpenAI — «Настройки» → «Картинка»")
    elif backend == "gemini_api" and not gemini_key(cfg):
        problems.append("Для картинок нужен API-ключ Gemini — «Настройки» → «Картинка»")
    elif backend == "zai" and not zai_key(cfg):
        problems.append("Для картинок нужен ключ Z.ai — «Настройки» → «Картинка»")
    elif backend == "openai_compat":
        if not str(cfg.get("compat_base_url", "")).strip():
            problems.append("Не указан адрес сервера своей модели — «Настройки» → «Картинка»")
        if not str(cfg.get("compat_image_model", "")).strip():
            problems.append("Не указана модель картинок своего сервера — «Настройки» → «Картинка»")
    elif backend == "codex":
        problems += _cli_problems(codex_status(), "Codex CLI", "ChatGPT")
    return problems


def check_openai_image(cfg) -> tuple[bool, str, list[str]]:
    """Ключ OpenAI и модели картинок, доступные ему (без генерации — бесплатно)."""
    import openai
    key = openai_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    try:
        client = openai.OpenAI(api_key=key, timeout=20, max_retries=1)
        models = sorted(m.id for m in client.models.list() if "image" in m.id and not m.id.startswith("chatgpt"))
    except openai.AuthenticationError:
        return False, "Ключ не подходит", []
    except openai.APIConnectionError:
        return False, "Нет связи с OpenAI API — проверьте интернет", []
    except openai.APIStatusError as e:
        return False, f"Ошибка API ({e.status_code}): {e.message}", []
    model = cfg.get("openai_image_model") or OPENAI_IMAGE_MODELS[0]
    if models and model not in models:
        return True, f"Ключ работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Ключ работает ✓  моделей картинок: {len(models)}", models or OPENAI_IMAGE_MODELS


def check_gemini_image(cfg) -> tuple[bool, str, list[str]]:
    from google import genai
    from google.genai import errors as gerr
    key = gemini_key(cfg)
    if not key:
        return False, "Ключ не указан", []
    try:
        models = sorted(m.name.removeprefix("models/") for m in genai.Client(api_key=key).models.list()
                        if "image" in m.name or "imagen" in m.name)
    except gerr.APIError as e:
        return (False, "Ключ не подходит", []) if e.code in (400, 401, 403) else \
               (False, f"Ошибка API ({e.code}): {e.message}", [])
    except Exception as e:
        return False, f"Нет связи с Gemini API: {e}", []
    model = cfg.get("gemini_image_model") or GEMINI_IMAGE_MODELS[0]
    if models and model not in models:
        return True, f"Ключ работает ✓, но модели {model} нет — выберите из списка", models
    return True, f"Ключ работает ✓  моделей картинок: {len(models)}", models or GEMINI_IMAGE_MODELS


def check_zai_image(cfg) -> tuple[bool, str, list[str]]:
    ok, msg, _ = check_zai_key(cfg)
    return ok, msg.replace(" — нажмите «Сохранить»", "") + (" · картинки в Z.ai платные" if ok else ""), []


# ---------- промпты ----------
# В промпте можно писать {тема}, {пожелание}, {длина}, {оформление}, {эмодзи}, {хэштеги}, {прошлые посты}
# (текст) и {тема}, {текст поста}, {пожелание}, {стиль}, {формат} (картинка). Чего в промпте нет —
# программа сама допишет в запрос, так что настройки поста работают с любым промптом.

FORMAT_RULES = {
    "plain": "Без форматирования: обычный текст. Не используй Markdown (**, __, #) и HTML-теги.",
    "basic": ("Важное можно выделять HTML-тегами Telegram: <b>жирный</b> и <i>курсив</i> — умеренно. "
              "Другие теги и Markdown (**, __, #) не используй."),
    "full": ("Можно использовать HTML-теги Telegram: <b>жирный</b>, <i>курсив</i>, <u>подчёркнутый</u>, "
             "<s>зачёркнутый</s>, <a href=\"https://…\">ссылка</a>, <blockquote>цитата</blockquote>, "
             "<code>код</code>. Markdown (**, __, #) не используй. "
             "Ссылки — только настоящие, не выдумывай адреса."),
}
EMOJI_RULES = {"none": "Без эмодзи.", "few": "1–3 уместных эмодзи на весь пост.",
               "many": "Много эмодзи: в заголовке, в начале абзацев и пунктов списка."}
ASPECTS = {   # подпись для промпта, размер для OpenAI, соотношение для Gemini
    "square": ("квадратная, 1:1", "1024x1024", "1:1"),
    "landscape": ("горизонтальная, 3:2", "1536x1024", "3:2"),
    "portrait": ("вертикальная, 2:3", "1024x1536", "2:3"),
}


def hashtag_rule(cfg) -> str:
    mode = cfg.get("post_hashtags", "none")
    if mode == "own":
        tags = " ".join(t if t.startswith("#") else "#" + t for t in str(cfg.get("hashtags", "")).split() if t)
        return (f"В самом конце 1–3 подходящих хэштега — только из этого списка: {tags}."
                if tags else "Без хэштегов.")
    if mode == "ai":
        return "В самом конце 2–4 тематических хэштега."
    return "Без хэштегов."


def length_rule(cfg) -> str:
    return f"не длиннее {int(cfg.get('post_max_len', 900))} знаков с пробелами"


def _fill(template: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Подставляет {ключ} в шаблон. Возвращает текст и ключи, которых в шаблоне не было."""
    missing = []
    for k, v in values.items():
        token = "{" + k + "}"
        if token in template:
            template = template.replace(token, v)
        else:
            missing.append(k)
    return template, missing


def build_text_request(cfg, topic: str, wish: str = "", current: str = "",
                       recent: list[str] | None = None) -> tuple[str, str]:
    """(system, user) для нейросети текста. current — текущий вариант, если его надо поправить по wish."""
    wish = wish.strip()
    recent_text = "\n\n".join(f"— {visible_text(r, cfg)[:300]}" for r in recent or []) or "(пока нет)"
    values = {"тема": topic.strip(), "пожелание": f"ПОЖЕЛАНИЯ: {wish}" if wish else "",
              "длина": length_rule(cfg), "оформление": FORMAT_RULES.get(cfg.get("post_format"), FORMAT_RULES["plain"]),
              "эмодзи": EMOJI_RULES.get(cfg.get("post_emoji"), EMOJI_RULES["few"]),
              "хэштеги": hashtag_rule(cfg), "прошлые посты": recent_text}
    system, missing = _fill(pick_variants(read_prompt(cfg, "text")), values)
    user = [f"Тема поста: {topic.strip()}"]
    rules = [f"- {name.capitalize()}: {values[key]}" for key, name in
             (("длина", "длина"), ("оформление", "оформление"), ("эмодзи", "эмодзи"), ("хэштеги", "хэштеги"))
             if key in missing]
    if rules:
        user.append("Требования к посту:\n" + "\n".join(rules))
    if "прошлые посты" in missing and recent:
        user.append("Недавние посты канала — не повторяй их темы, заходы и формулировки:\n" + recent_text)
    if current.strip() and wish:
        user.append(f"Текущий вариант поста:\n<<<\n{current.strip()}\n>>>\n"
                    "Перепиши его с учётом пожеланий; что пожелания не касаются — сохрани.")
    if wish and "пожелание" in missing:
        user.append(f"ПОЖЕЛАНИЯ: {wish}")
    user.append("Выведи только текст поста — без пояснений, кавычек вокруг и подписи канала.")
    return system.strip(), "\n\n".join(user)


def build_image_prompt(cfg, topic: str, text: str, wish: str = "") -> str:
    wish = wish.strip()
    values = {"тема": topic.strip(), "текст поста": visible_text(text, cfg)[:1500],
              "пожелание": f"Пожелания: {wish}" if wish else "",
              "стиль": str(cfg.get("image_style", "")).strip() or "на усмотрение художника, современный и чистый",
              "формат": ASPECTS.get(cfg.get("image_aspect"), ASPECTS["square"])[0]}
    prompt, missing = _fill(pick_variants(read_prompt(cfg, "image")), values)
    extra = []
    if "формат" in missing:
        extra.append(f"Формат: {values['формат']}.")
    if wish and "пожелание" in missing:
        extra.append(values["пожелание"])
    return "\n".join([prompt.strip(), *extra]).strip()


def edit_image_prompt(wish: str) -> str:
    return (f"Измени приложенную картинку: {wish.strip()}. Всё остальное сохрани как есть — "
            "композицию, стиль, объекты и цвета.")


# ---------- оформление поста для Telegram ----------
# Текст поста хранится так, как его видит человек в редакторе: в режимах basic/full — с HTML-тегами
# Telegram, в plain — обычный текст. Перед публикацией программа приводит его к разрешённым тегам.

ALLOWED_TAGS = {
    "plain": set(),
    "basic": {"b", "strong", "i", "em", "u", "s"},
    "full": {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "blockquote", "code", "pre"},
}
TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)((?:\s[^<>]*)?)\s*/?>")
ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")
HREF_RE = re.compile(r"""href\s*=\s*["']?([^"'\s>]+)""", re.I)
MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
MD_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)


def _escape_text(s: str) -> str:
    """Экранирует < > и одиночные &, не трогая готовые сущности вроде &amp;."""
    out, pos = [], 0
    for m in ENTITY_RE.finditer(s):
        out.append(html.escape(s[pos:m.start()], quote=False))
        out.append(m.group(0))
        pos = m.end()
    out.append(html.escape(s[pos:], quote=False))
    return "".join(out)


def clean_post(text: str, cfg) -> str:
    """Ответ нейросети → текст поста в режиме оформления профиля: Markdown превращается в теги,
    лишние теги убираются, незакрытые закрываются."""
    mode = cfg.get("post_format", "plain")
    text = text.strip()
    if cfg.get("plain_typography"):
        text = text.translate(HUMAN_TYPOGRAPHY)
    if mode == "plain":
        text = MD_LINK.sub(r"\1 (\2)", text)
        text = MD_BOLD.sub(r"\1", text)
        text = MD_HEADING.sub(r"\1", text)
        return visible_text(text, {"post_format": "basic"})   # теги, если модель их всё же вставила, — прочь
    text = MD_LINK.sub(lambda m: f'<a href="{m[2]}">{m[1]}</a>' if mode == "full" else f"{m[1]} ({m[2]})", text)
    text = MD_BOLD.sub(r"<b>\1</b>", text)
    text = MD_HEADING.sub(r"<b>\1</b>", text)
    return sanitize_html(text, mode)


def sanitize_html(text: str, mode: str) -> str:
    allowed = ALLOWED_TAGS.get(mode, set())
    out, stack, pos = [], [], 0
    for m in TAG_RE.finditer(text):
        out.append(_escape_text(text[pos:m.start()]))
        pos = m.end()
        closing, name, attrs = m.group(1) == "/", m.group(2).lower(), m.group(3) or ""
        if name not in allowed:
            continue
        if closing:
            if name in stack:   # закрываем и всё, что открыто внутри
                while stack:
                    top = stack.pop()
                    out.append(f"</{top}>")
                    if top == name:
                        break
            continue
        if name == "a":
            href = HREF_RE.search(attrs)
            if not href:
                continue
            out.append(f'<a href="{html.escape(href.group(1), quote=True)}">')
        else:
            out.append(f"<{name}>")
        stack.append(name)
    out.append(_escape_text(text[pos:]))
    out += [f"</{t}>" for t in reversed(stack)]
    return "".join(out)


def visible_text(text: str, cfg) -> str:
    """Что увидит читатель: без тегов и с раскрытыми &amp;."""
    if cfg.get("post_format", "plain") == "plain":
        return text
    return html.unescape(TAG_RE.sub("", text))


def signature_html(cfg) -> str:
    """Подпись в конце поста: в ней можно ставить ссылки при любом оформлении поста."""
    return sanitize_html(str(cfg.get("post_signature", "")).strip(), "full")


def post_html(text: str, cfg) -> str:
    """Готовый к отправке HTML (parse_mode='html') — текст поста с подписью."""
    if cfg.get("post_format", "plain") == "plain":
        body = html.escape(text.rstrip(), quote=False)
    else:
        body = sanitize_html(text.rstrip(), cfg["post_format"])
    sig = signature_html(cfg)
    return f"{body}\n\n{sig}" if sig else body


def post_length(text: str, cfg) -> int:
    """Длина поста с подписью — так её считает Telegram."""
    sig = html.unescape(TAG_RE.sub("", signature_html(cfg)))
    return len(visible_text(text.rstrip(), cfg)) + (len(sig) + 2 if sig else 0)
