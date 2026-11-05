"""
Картинка к посту: генерация и правка через нейросети или картинка из своей папки.
ImageGen.generate(prompt, out_dir, base=None) — асинхронно; base — картинка, которую надо поправить.
"""
import asyncio
import base64
import mimetypes
import random
import shutil
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

from core import (ASPECTS, BASE, CODEX_DIR, IMAGE_BACKEND_NAMES, NO_WINDOW, ZAI_URL, cli_env, compat_client,
                  decode_any, find_codex, gemini_key, list_images, openai_key, zai_key)
from limits import LimitHit

ZAI_SIZES = {"square": "1024x1024", "landscape": "1344x768", "portrait": "768x1344"}
# Codex: лишнее для одной картинки отключаем, генерацию — нет
CODEX_IMAGE_FEATURES_OFF = ["apps", "plugins", "multi_agent", "browser_use", "computer_use", "goals"]
EDITABLE = {"openai_api", "gemini_api", "codex"}


class ImageGen:
    def __init__(self, cfg: dict, log):
        self.cfg, self.log = cfg, log
        self.lock = asyncio.Lock()
        self._bag: list[Path] = []
        self.workspace = BASE / f"_image_work_{cfg['id']}"

    @property
    def backend(self) -> str:
        return self.cfg.get("image_backend", "none")

    @property
    def name(self) -> str:
        return IMAGE_BACKEND_NAMES.get(self.backend, self.backend)

    @property
    def can_edit(self) -> bool:
        return self.backend in EDITABLE

    @property
    def generates(self) -> bool:
        """Рисует ли нейросеть (а не берёт из папки / без картинки)."""
        return self.backend not in ("folder", "none")

    @property
    def timeout(self) -> float:
        return float(self.cfg.get("image_timeout_sec", 300))

    async def generate(self, prompt: str, out_dir: Path, base: Path | None = None) -> Path | None:
        """Новая картинка в out_dir. None — режим «без картинки»."""
        b = self.backend
        if b == "none":
            return None
        if b == "folder":
            return self._from_folder(out_dir)
        if base and not self.can_edit:
            raise RuntimeError(f"{self.name} не умеет править картинку — нарисуйте заново с пожеланием")
        gen = {"openai_api": self._openai, "gemini_api": self._gemini, "codex": self._codex,
               "zai": self._zai, "openai_compat": self._compat}.get(b)
        if not gen:
            raise RuntimeError(f"неизвестный способ получить картинку: {b}")
        async with self.lock:
            data = await gen(prompt, base)
        return save_image(data, out_dir)

    # --- своя папка ---

    def _from_folder(self, out_dir: Path) -> Path:
        """Перемешанная «колода»: каждая картинка по разу, потом новый круг."""
        files = list_images(self.cfg)
        if not files:
            raise RuntimeError("в папке картинок пусто — «Настройки» → «Картинка»")
        self._bag = [p for p in self._bag if p in files] or random.sample(files, len(files))
        src = self._bag.pop()
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = next_name(out_dir, src.suffix.lower())
        shutil.copy(src, dst)
        return dst

    # --- OpenAI Images API ---

    async def _openai(self, prompt: str, base: Path | None) -> bytes:
        import openai
        key = openai_key(self.cfg)
        if not key:
            raise RuntimeError("не указан API-ключ OpenAI — «Настройки» → «Картинка»")
        model = self.cfg.get("openai_image_model") or "gpt-image-2"
        size = ASPECTS.get(self.cfg.get("image_aspect"), ASPECTS["square"])[1]
        quality = self.cfg.get("openai_image_quality") or "auto"
        client = openai.AsyncOpenAI(api_key=key, timeout=self.timeout, max_retries=1)
        try:
            if base:
                with open(base, "rb") as f:
                    resp = await client.images.edit(model=model, image=f, prompt=prompt, size=size, quality=quality)
            else:
                resp = await client.images.generate(model=model, prompt=prompt, size=size, quality=quality, n=1)
        except openai.AuthenticationError:
            raise RuntimeError("ключ OpenAI не подходит — проверьте его в «Настройках»")
        except openai.PermissionDeniedError as e:
            raise RuntimeError(f"OpenAI не даёт доступ к {model}: {e.message} "
                               "(для моделей картинок OpenAI может требовать подтвердить организацию)")
        except openai.NotFoundError:
            raise RuntimeError(f"модель {model} недоступна — выберите другую в «Настройках» → «Картинка»")
        except openai.RateLimitError as e:
            raise RuntimeError(f"OpenAI: лимит или нет средств на балансе — {e.message}")
        except openai.BadRequestError as e:
            if "safety" in str(e.message).lower() or "moderation" in str(e.message).lower():
                raise RuntimeError("OpenAI отклонил запрос фильтром безопасности — измените пожелание или тему")
            raise RuntimeError(f"OpenAI отклонил запрос: {e.message}")
        except openai.APITimeoutError:
            raise RuntimeError("OpenAI не прислал картинку вовремя — увеличьте таймаут в «Настройках»")
        except openai.APIConnectionError:
            raise RuntimeError("нет связи с OpenAI API — проверьте интернет или VPN")
        except openai.APIStatusError as e:
            raise RuntimeError(f"ошибка OpenAI API ({e.status_code}): {e.message}")
        finally:
            await client.close()
        return await image_bytes(resp)

    # --- Gemini (Gemini Image и Imagen) ---

    async def _gemini(self, prompt: str, base: Path | None) -> bytes:
        from google import genai
        from google.genai import errors as gerr, types
        key = gemini_key(self.cfg)
        if not key:
            raise RuntimeError("не указан API-ключ Gemini — «Настройки» → «Картинка»")
        model = self.cfg.get("gemini_image_model") or "gemini-2.5-flash-image"
        ratio = ASPECTS.get(self.cfg.get("image_aspect"), ASPECTS["square"])[2]
        client = genai.Client(api_key=key)
        try:
            if model.startswith("imagen"):
                if base:
                    raise RuntimeError(f"{model} не умеет править картинку — выберите модель Gemini Image")
                resp = await asyncio.wait_for(client.aio.models.generate_images(
                    model=model, prompt=prompt,
                    config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio=ratio)), self.timeout)
                if not resp.generated_images:
                    raise RuntimeError("Imagen не вернул картинку — возможно, сработал фильтр безопасности")
                return resp.generated_images[0].image.image_bytes
            contents = [prompt]
            if base:
                contents.insert(0, types.Part.from_bytes(
                    data=base.read_bytes(), mime_type=mimetypes.guess_type(base.name)[0] or "image/png"))
            resp = await asyncio.wait_for(client.aio.models.generate_content(
                model=model, contents=contents,
                config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"],
                                                   image_config=types.ImageConfig(aspect_ratio=ratio))),
                self.timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Gemini не прислал картинку вовремя — увеличьте таймаут в «Настройках»")
        except gerr.APIError as e:
            if e.code in (401, 403) or "API key" in str(e.message):
                raise RuntimeError("ключ Gemini не подходит или нет доступа к модели картинок")
            if e.code == 429:
                raise RuntimeError("исчерпан лимит Gemini API — для моделей картинок обычно нужен платный тариф")
            if e.code == 404:
                raise RuntimeError(f"модель {model} не найдена — выберите другую в «Настройках» → «Картинка»")
            raise RuntimeError(f"ошибка Gemini API ({e.code}): {e.message}")
        texts = []
        for cand in resp.candidates or []:
            for part in (cand.content.parts if cand.content else None) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
                if part.text:
                    texts.append(part.text)
        raise RuntimeError("Gemini не вернул картинку" + (f": {' '.join(texts)[:300]}" if texts else
                                                          " — возможно, сработал фильтр безопасности"))

    # --- Z.ai (CogView) ---

    async def _zai(self, prompt: str, base: Path | None) -> bytes:
        import openai
        key = zai_key(self.cfg)
        if not key:
            raise RuntimeError("не указан ключ Z.ai — «Настройки» → «Картинка»")
        model = self.cfg.get("zai_image_model") or "cogview-4-250304"
        client = openai.AsyncOpenAI(base_url=ZAI_URL, api_key=key, timeout=self.timeout, max_retries=1)
        try:
            resp = await client.images.generate(model=model, prompt=prompt,
                                                size=ZAI_SIZES.get(self.cfg.get("image_aspect"), "1024x1024"))
        except openai.AuthenticationError:
            raise RuntimeError("ключ Z.ai не подходит")
        except openai.APIStatusError as e:
            raise RuntimeError(f"ошибка Z.ai ({e.status_code}): {e.message} — генерация картинок в Z.ai платная, "
                               "проверьте баланс или выберите другой способ")
        except openai.APIConnectionError:
            raise RuntimeError("нет связи с Z.ai")
        finally:
            await client.close()
        return await image_bytes(resp)

    # --- своя модель: OpenAI-совместимый /images/generations ---

    async def _compat(self, prompt: str, base: Path | None) -> bytes:
        import openai
        model = str(self.cfg.get("compat_image_model", "")).strip()
        size = ASPECTS.get(self.cfg.get("image_aspect"), ASPECTS["square"])[1]
        client = compat_client(self.cfg, asynchronous=True)
        try:
            resp = await client.with_options(timeout=self.timeout).images.generate(model=model, prompt=prompt, size=size)
        except openai.NotFoundError:
            raise RuntimeError("сервер не умеет генерировать картинки (нет /images/generations) или модели нет")
        except openai.APIStatusError as e:
            raise RuntimeError(f"ошибка сервера ({e.status_code}): {e.message}")
        except openai.APIConnectionError:
            raise RuntimeError("нет связи с сервером своей модели")
        finally:
            await client.close()
        return await image_bytes(resp)

    # --- ChatGPT через Codex CLI (по подписке) ---

    async def _codex(self, prompt: str, base: Path | None) -> bytes:
        """Codex рисует встроенным инструментом image_gen и кладёт файл в ~/.codex/generated_images.
        Инструмент есть не на всех тарифах ChatGPT — тогда картинки не будет и мы скажем об этом."""
        exe = find_codex()
        if not exe:
            raise RuntimeError("не установлен Codex CLI — «Настройки» → «Картинка» → «Установить и войти»")
        ws = self.workspace
        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True)
        last = ws / "last_message.txt"
        task = ("Edit the attached image with your built-in image generation tool (image_gen). "
                if base else "Generate one image with your built-in image generation tool (image_gen). ")
        task += ("Call the tool exactly once, do not run shell commands, do not read or write files. "
                 "After the tool returns, reply with the single word DONE.\n\nImage request:\n" + prompt)
        args = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                "--color", "never", "-C", str(ws), "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',
                "-c", 'forced_login_method="chatgpt"', "--enable", "image_generation", "-o", str(last)]
        for feature in CODEX_IMAGE_FEATURES_OFF:
            args += ["--disable", feature]
        if base:
            args.append(f"--image={base}")
        started = time.time() - 2
        env = cli_env(drop=("OPENAI_API_KEY", "CODEX_API_KEY"))
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(ws), creationflags=NO_WINDOW)
        try:
            out, err = await asyncio.wait_for(proc.communicate(task.encode("utf-8")), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Codex не прислал картинку вовремя — увеличьте таймаут в «Настройках»")
        answer = last.read_text(encoding="utf-8").strip() if last.exists() else ""
        shutil.rmtree(ws, ignore_errors=True)
        gen_dir = CODEX_DIR / "generated_images"
        made = sorted((p for p in (gen_dir.rglob("*") if gen_dir.is_dir() else [])
                       if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                       and p.stat().st_mtime >= started), key=lambda p: p.stat().st_mtime)
        if made:
            return made[-1].read_bytes()
        raw = decode_any(err) + decode_any(out)
        msg = raw.lower()
        if "not logged in" in msg or "401 unauthorized" in msg:
            raise RuntimeError("не выполнен вход в ChatGPT — «Настройки» → «Картинка» → «Войти в ChatGPT»")
        if "usage limit" in msg or "rate limit" in msg:
            raise LimitHit("исчерпан лимит подписки ChatGPT", raw=raw[-1500:])
        raise RuntimeError("Codex не создал картинку. Похоже, генерация картинок в Codex недоступна для "
                           "вашего тарифа ChatGPT (на бесплатном её нет) — выберите в «Настройках» → «Картинка» "
                           "OpenAI API или Gemini API" + (f". Ответ Codex: {answer[:200]}" if answer else ""))


async def image_bytes(resp) -> bytes:
    """Картинка из ответа Images API: base64 или ссылка."""
    if not getattr(resp, "data", None):
        raise RuntimeError("нейросеть не вернула картинку")
    item = resp.data[0]
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    if getattr(item, "url", None):
        import httpx
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
            r = await http.get(item.url)
            r.raise_for_status()
            return r.content
    raise RuntimeError("нейросеть не вернула картинку")


def next_name(out_dir: Path, ext: str) -> Path:
    n = 1 + sum(1 for p in out_dir.glob("img_*") if p.is_file())
    while (out_dir / f"img_{n}{ext}").exists():
        n += 1
    return out_dir / f"img_{n}{ext}"


def save_image(data: bytes, out_dir: Path) -> Path:
    """Сохраняет картинку; проверяет, что это картинка. Неподходящие для Telegram форматы → PNG."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception:
        raise RuntimeError("нейросеть прислала не картинку")
    fmt = (img.format or "").upper()
    if fmt in ("PNG", "JPEG"):
        path = next_name(out_dir, ".png" if fmt == "PNG" else ".jpg")
        path.write_bytes(data)
    else:
        path = next_name(out_dir, ".png")
        img.save(path, "PNG")
    return path
