"""
Автообновление через GitHub Releases: узнать последнюю версию, скачать её установщик,
запустить его в тихом режиме и закрыть программу — установщик сам запустит новую версию.
Обновлять сам себя умеет только установленный .exe; при запуске из исходников —
только сообщаем о новой версии.
"""
import hashlib
import json
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import certifi

import core

API = f"https://api.github.com/repos/{core.REPO}/releases/latest"
ASSET_SUFFIX = "-Setup.exe"   # TG-Autopost-0.1.0-Setup.exe
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": f"tg-autopost/{core.VERSION}"}

# Корневые сертификаты certifi (идут с программой) плюс хранилище Windows. Одного хранилища мало:
# Windows докачивает корневые сертификаты по мере надобности, и на части компьютеров нужного там нет —
# GitHub тогда не открывается с CERTIFICATE_VERIFY_FAILED. Хранилище оставляем ради антивирусов и прокси,
# которые подменяют сертификаты своим
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
SSL_CONTEXT.load_default_certs()


@dataclass
class Update:
    version: str
    notes: str             # описание релиза
    page: str              # страница релиза на GitHub
    url: str | None        # установщик; None — в релизе его нет
    size: int = 0
    sha256: str | None = None


def parse_version(s: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in s.strip().lstrip("vV").split("-")[0].split("."))
    except ValueError:
        return (0,)


def check() -> Update | None:
    """Новая версия или None. Ошибки сети — исключением (urllib.error.URLError и т. п.)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(API, headers=HEADERS), timeout=20,
                                    context=SSL_CONTEXT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:   # релизов ещё нет
            return None
        raise
    version = data["tag_name"].lstrip("vV")
    if parse_version(version) <= parse_version(core.VERSION):
        return None
    asset = next((a for a in data.get("assets", []) if a["name"].endswith(ASSET_SUFFIX)), None)
    digest = (asset or {}).get("digest") or ""   # «sha256:…» — GitHub считает сам при загрузке
    return Update(version=version, notes=(data.get("body") or "").strip(), page=data["html_url"],
                  url=asset["browser_download_url"] if asset else None,
                  size=asset["size"] if asset else 0,
                  sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else None)


def download(u: Update, progress=None) -> Path:
    """Качает установщик во временную папку и сверяет размер и SHA-256. progress(доля 0..1)."""
    target = Path(tempfile.gettempdir()) / f"TG-Autopost-{u.version}-Setup.exe"
    part = target.with_suffix(".part")
    h, done = hashlib.sha256(), 0
    req = urllib.request.Request(u.url, headers={"User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as r, part.open("wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and u.size:
                progress(done / u.size)
    if (u.size and done != u.size) or (u.sha256 and h.hexdigest() != u.sha256.lower()):
        part.unlink(missing_ok=True)
        raise RuntimeError("Файл обновления скачался с ошибкой (не совпала контрольная сумма) — попробуйте ещё раз")
    part.replace(target)
    return target


def install(setup: Path, run: bool, tray: bool):
    """Запускает установщик отдельно от программы; программа после этого должна сразу закрыться.
    /UPDATED=1 — установщик сам запустит новую версию; RUN — продолжить работу, TRAY — сразу в трей."""
    subprocess.Popen(
        [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
         "/UPDATED=1", f"/RUN={int(run)}", f"/TRAY={int(tray)}"],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True)
