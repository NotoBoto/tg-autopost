"""
Сборка установщика: PyInstaller собирает программу в папку dist/TG Autopost,
Inno Setup упаковывает её в dist/TG-Autopost-<версия>-Setup.exe.

    pip install -r requirements.txt pyinstaller
    python build.py              # программа + установщик (нужен Inno Setup 6)
    python build.py --no-setup   # только программа, без установщика
    python build.py --key-only   # только _builtin_api.py — для запуска из исходников

Встроенный ключ Telegram (api_id/api_hash) берётся из переменных окружения TG_API_ID и TG_API_HASH
(в GitHub Actions — из секретов репозитория) и записывается в _builtin_api.py, которого нет в git.

Обычно собирает GitHub Actions при публикации тега vX.Y.Z (.github/workflows/release.yml).
"""
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")   # консоль GitHub Actions — cp1252, русский текст в ней падает
from core import VERSION  # noqa: E402

NAME = "TG Autopost"
DIST = ROOT / "dist"
KEY_FILE = ROOT / "_builtin_api.py"


def write_builtin_key():
    """Встроенный ключ Telegram из TG_API_ID / TG_API_HASH -> _builtin_api.py (в .gitignore).

    Маска XOR прячет ключ только от поиска строк в .exe: программа сама должна отдать его
    Telegram, поэтому от того, кто разбирает .exe, его не спрятать никак.
    """
    api_id, api_hash = os.environ.get("TG_API_ID", "").strip(), os.environ.get("TG_API_HASH", "").strip()
    if not api_id and not api_hash:
        if os.environ.get("GITHUB_REF", "").startswith("refs/tags/"):
            sys.exit("Релиз без встроенного ключа Telegram: добавьте секреты TG_API_ID и TG_API_HASH "
                     "в Settings → Secrets and variables → Actions")
        print("TG_API_ID/TG_API_HASH не заданы — " + ("остаётся прежний _builtin_api.py" if KEY_FILE.exists()
              else "сборка без встроенного ключа, пользователям понадобится свой"))
        return
    api_hash = api_hash.lower()   # регистр в hash не важен — на my.telegram.org он строчный
    # Сами значения в ошибках не печатаем — лог сборки GitHub Actions виден не только вам
    problems = []
    if not api_id.isdigit():
        problems.append(f"TG_API_ID должен состоять только из цифр (сейчас {len(api_id)} симв., "
                        f"не цифр: {sum(not c.isdigit() for c in api_id)})")
    if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        bad = sum(c not in "0123456789abcdef" for c in api_hash)
        problems.append(f"TG_API_HASH должен быть из 32 символов 0-9 и a-f (сейчас {len(api_hash)} симв., "
                        f"лишних символов: {bad})")
    if problems:
        sys.exit("\n".join(problems))
    mask = secrets.token_bytes(64)
    blob = bytes(b ^ mask[i % len(mask)] for i, b in enumerate(f"{api_id}:{api_hash}".encode()))
    KEY_FILE.write_text("# Создан build.py из TG_API_ID/TG_API_HASH. Не коммитить (есть в .gitignore)\n"
                        f"MASK = {mask!r}\nBLOB = {blob!r}\n", encoding="utf-8")
    print("Встроенный ключ Telegram записан в _builtin_api.py")


def find_iscc() -> str | None:
    for p in (shutil.which("iscc"),
              Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
              Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"):
        if p and Path(p).exists():
            return str(p)
    return None


def main():
    write_builtin_key()
    if "--key-only" in sys.argv:
        return
    sep = os.pathsep
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", NAME, "--icon", str(ROOT / "assets" / "icon.ico"),
        "--distpath", str(DIST), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'prompt_text.example.txt'}{sep}.",
        "--add-data", f"{ROOT / 'prompt_image.example.txt'}{sep}.",
        "--add-data", f"{ROOT / 'assets' / 'icon.ico'}{sep}assets",
        "--add-data", f"{ROOT / 'assets' / 'notify.wav'}{sep}assets",
        "--collect-data", "customtkinter",      # темы и шрифты customtkinter
        "--hidden-import", "pystray._win32",    # pystray выбирает бэкенд при запуске
        "--collect-data", "tzdata",             # часовые пояса: «resets 3pm (Europe/Moscow)» от Claude Code
        "--hidden-import", "cryptg",            # Telethon ищет его при запуске; без него шифрует медленно
        str(ROOT / "app.py"),
    ], check=True)
    print(f"Программа: {DIST / NAME / (NAME + '.exe')}")

    if "--no-setup" in sys.argv:
        return
    iscc = find_iscc()
    if not iscc:
        sys.exit("Не найден Inno Setup 6 (ISCC.exe) — установите его или запустите с --no-setup")
    subprocess.run([iscc, f"/DAppVersion={VERSION}", f"/DSourceDir={DIST / NAME}",
                    f"/O{DIST}", str(ROOT / "installer.iss")], check=True)
    print(f"Установщик: {DIST / f'TG-Autopost-{VERSION}-Setup.exe'}")


if __name__ == "__main__":
    main()
