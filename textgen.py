"""
Генерация текста поста: все способы подключения к нейросетям (перенесены из автокомментатора).
TextGen.generate(system, user) — асинхронно, в цикле движка профиля.
"""
import asyncio
import base64
import json
import mimetypes
import shutil
from pathlib import Path

from core import (AGY_AGENT, API_MODELS, BASE, CODEX_FEATURES_OFF, GEMINI_API_DEFAULT, NO_WINDOW,
                  OPENAI_API_DEFAULT, TEXT_BACKEND_NAMES, ZAI_BAD_KEY, ZAI_MODELS, ZAI_RETRY_DELAYS,
                  ZAI_VISION_MODEL, _zai_gone, api_key, chat_content, cli_env, compat_client,
                  compat_connection_hint, decode_any, find_agy, find_claude, find_codex, gemini_key,
                  openai_key, strip_reasoning, zai_busy, zai_client, zai_error, zai_error_text, zai_extra,
                  zai_key, zai_lock, zai_models_order, zai_thinking_rejected)


class TextGen:
    """Нейросеть для текста одного профиля. cfg — словарь профиля (живой: правки настроек видны сразу)."""

    def __init__(self, cfg: dict, log):
        self.cfg, self.log = cfg, log
        pid = cfg["id"]
        # Промпт передаём файлом: многострочный текст в аргументах ломается в cmd.exe
        self.system_prompt_file = BASE / f"_system_prompt_{pid}.txt"
        # Пустые рабочие папки: agy и Codex не видят файлы проекта
        self.agy_workspace = BASE / f"_agy_workspace_{pid}"
        self.codex_workspace = BASE / f"_codex_workspace_{pid}"
        self._api_client = self._api_client_key = None
        self._gemini_client = self._gemini_client_key = None
        self._openai_client = self._openai_client_key = None
        self.lock = asyncio.Lock()   # по одному запросу за раз

    @property
    def name(self) -> str:
        return TEXT_BACKEND_NAMES.get(self.cfg.get("backend"), "Claude Code")

    async def generate(self, system: str, user_msg: str) -> str:
        gen = {"api": self._gen_api, "gemini_cli": self._gen_gemini_cli,
               "gemini_api": self._gen_gemini_api, "codex": self._gen_codex,
               "openai_api": self._gen_openai_api, "zai": self._gen_zai,
               "openai_compat": self._gen_compat}.get(self.cfg.get("backend"), self._gen_claude_code)
        async with self.lock:
            text = (await gen(system, user_msg, [])).strip()
        if not text:
            raise RuntimeError("нейросеть вернула пустой ответ — попробуйте ещё раз или смените модель")
        return text

    async def _gen_claude_code(self, system: str, user_msg: str, images: list[Path]) -> str:
        cfg = self.cfg
        self.system_prompt_file.write_text(system, encoding="utf-8")
        args = [find_claude(), "-p",
                "--system-prompt-file", str(self.system_prompt_file),
                "--model", cfg.get("model", "sonnet"),
                "--output-format", "text"]
        cwd = None
        if images:
            names = ", ".join(p.name for p in images)
            user_msg += (f"\n\nК запросу приложены картинки: {names}. Открой каждую "
                         "инструментом Read, посмотри, что на них, и учти это. "
                         "В ответе выведи только сам текст поста.")
            args += ["--allowedTools", "Read", "--max-turns", str(len(images) + 2)]
            cwd = str(images[0].parent)
        else:
            args += ["--max-turns", "1"]

        # Чтобы Claude Code не ушёл на платный API вместо подписки
        env = cli_env(drop=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env, cwd=cwd,
            creationflags=NO_WINDOW,  # без чёрного окна
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(user_msg.encode("utf-8")),
                timeout=cfg.get("claude_timeout_sec", 120) + 30 * len(images),
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Claude Code не ответил вовремя")
        if proc.returncode != 0:
            msg = (decode_any(err) or decode_any(out)).strip()
            raise RuntimeError(msg or f"код выхода {proc.returncode}")
        return decode_any(out).strip()

    async def _gen_gemini_cli(self, system: str, user_msg: str, images: list[Path]) -> str:
        """Antigravity CLI: наш промпт кладём как отдельного агента в пустую рабочую папку."""
        cfg = self.cfg
        ws = self.agy_workspace
        shutil.rmtree(ws, ignore_errors=True)
        agent_dir = ws / ".agents" / "agents" / AGY_AGENT
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.md").write_text(
            f"---\nname: {AGY_AGENT}\ndescription: Пишет пост для Telegram-канала\n---\n"
            + system, encoding="utf-8")
        prompt = user_msg
        if images:
            for img in images:
                shutil.copy(img, ws / img.name)
            prompt += ("\n\nК запросу приложены картинки — файлы " + ", ".join(p.name for p in images)
                       + " в текущей папке. Посмотри их и учти.")
        prompt += "\n\nВыведи только сам текст поста, без пояснений."

        timeout = cfg.get("claude_timeout_sec", 120) + 30 * len(images)
        args = [find_agy(), "--agent", AGY_AGENT, "--output-format", "json",
                "--disable-slash-commands", "--print-timeout", f"{timeout}s"]
        if cfg.get("gemini_model"):
            args += ["--model", cfg["gemini_model"]]
        args += ["-p", prompt]
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=cli_env(), cwd=str(ws), creationflags=NO_WINDOW)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 15)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Antigravity не ответил вовремя")
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        text_out, text_err = decode_any(out), decode_any(err)
        if "sign in" in (text_out + text_err).lower() or "authentication required" in (text_out + text_err).lower():
            raise RuntimeError("Не выполнен вход в Google для Antigravity — «Настройки» → «Войти в Google»")
        try:
            data = json.loads(text_out[text_out.index("{"):])
        except ValueError:
            raise RuntimeError((text_err or text_out).strip()[-500:] or f"код выхода {proc.returncode}")
        if data.get("status") != "SUCCESS":
            raise RuntimeError(f"Antigravity: {data.get('error') or data.get('status')}")
        return (data.get("response") or "").strip()

    async def _gen_gemini_api(self, system: str, user_msg: str, images: list[Path]) -> str:
        from google import genai
        from google.genai import errors as gerr, types
        cfg = self.cfg
        key = gemini_key(cfg)
        if self._gemini_client is None or self._gemini_client_key != key:
            self._gemini_client = genai.Client(api_key=key)
            self._gemini_client_key = key
        parts = [types.Part.from_bytes(data=img.read_bytes(),
                                       mime_type=mimetypes.guess_type(img.name)[0] or "image/jpeg")
                 for img in images]
        if images:
            user_msg += "\n\nК запросу приложены картинки (выше) — учти, что на них. Выведи только сам текст поста."
        parts.append(user_msg)
        model = cfg.get("gemini_api_model") or GEMINI_API_DEFAULT
        try:
            resp = await asyncio.wait_for(
                self._gemini_client.aio.models.generate_content(
                    model=model, contents=parts,
                    config=types.GenerateContentConfig(
                        system_instruction=system, max_output_tokens=8192,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))),
                timeout=cfg.get("claude_timeout_sec", 120))
        except asyncio.TimeoutError:
            raise RuntimeError("Gemini API не ответил вовремя")
        except gerr.APIError as e:
            if e.code in (401, 403) or "API key" in str(e.message):
                raise RuntimeError("Ключ Gemini не подходит — проверьте его в «Настройках»")
            if e.code == 429:
                raise RuntimeError("Исчерпан лимит запросов Gemini API — подождите или проверьте тариф")
            if e.code == 404:
                raise RuntimeError(f"Модель {model} не найдена — выберите другую в «Настройках»")
            raise RuntimeError(f"Ошибка Gemini API ({e.code}): {e.message}")
        except Exception as e:
            raise RuntimeError(f"Нет связи с Gemini API: {e}")
        u = resp.usage_metadata
        if u:
            self.log.info("Gemini %s: вход %s ток., выход %s ток.", model,
                     u.prompt_token_count, (u.candidates_token_count or 0) + (u.thoughts_token_count or 0))
        if not resp.text:
            self.log.info("Gemini не вернул текст (сработал фильтр безопасности или пустой ответ)")
            return ""
        return resp.text.strip()

    async def _gen_codex(self, system: str, user_msg: str, images: list[Path]) -> str:
        """Codex CLI по подписке ChatGPT: наш промпт заменяет встроенные инструкции Codex."""
        cfg = self.cfg
        ws = self.codex_workspace
        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True)
        instr, last = ws / "instructions.md", ws / "last_message.txt"
        instr.write_text(system, encoding="utf-8")
        args = [find_codex(), "exec", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "read-only", "--color", "never", "-C", str(ws),
                "-c", f"model_instructions_file='{instr}'",
                "-c", "project_doc_max_bytes=0",          # не читать AGENTS.md
                "-c", "skills.include_instructions=false",
                "-c", 'web_search="disabled"',
                "-c", 'forced_login_method="chatgpt"',    # только подписка, не API-ключ
                "-o", str(last)]
        for feature in CODEX_FEATURES_OFF:
            args += ["--disable", feature]
        if cfg.get("codex_model"):
            args += ["-m", cfg["codex_model"]]
        args += [f"--image={img}" for img in images]
        if images:
            user_msg += "\n\nК запросу приложены картинки — учти, что на них."
        user_msg += "\n\nВыведи только сам текст поста, без пояснений."
        env = cli_env(drop=("OPENAI_API_KEY", "CODEX_API_KEY"))
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=str(ws), creationflags=NO_WINDOW)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(user_msg.encode("utf-8")),
                timeout=cfg.get("claude_timeout_sec", 120) + 30 * len(images))
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Codex не ответил вовремя")
        try:
            text = last.read_text(encoding="utf-8").strip() if last.exists() else ""
        finally:
            shutil.rmtree(ws, ignore_errors=True)
        if proc.returncode != 0 or not text:
            msg = (decode_any(err) + decode_any(out)).strip()
            low = msg.lower()
            if "not logged in" in low or "401 unauthorized" in low:
                raise RuntimeError("Не выполнен вход в ChatGPT — «Настройки» → «Войти в ChatGPT»")
            if "usage limit" in low or "rate limit" in low:
                raise RuntimeError("Исчерпан лимит подписки ChatGPT для Codex — подождите сброса")
            lines = [l for l in msg.splitlines() if "error" in l.lower()]
            raise RuntimeError("Codex: " + (lines[-1] if lines else msg[-400:] or f"код выхода {proc.returncode}"))
        return text

    async def _gen_compat(self, system: str, user_msg: str, images: list[Path]) -> str:
        """Своя модель на OpenAI-совместимом сервере (LM Studio, Ollama…) через chat/completions."""
        import openai
        cfg = self.cfg
        url = str(cfg.get("compat_base_url", "")).strip().rstrip("/")
        sig = (url, cfg.get("compat_api_key", ""))
        if getattr(self, "_compat_sig", None) != sig:
            self._compat_client, self._compat_sig = compat_client(cfg, asynchronous=True), sig
        content = chat_content(user_msg, images)   # картинки понимают только модели с vision
        model = str(cfg.get("compat_model", "")).strip()
        try:
            resp = await self._compat_client.with_options(
                timeout=float(cfg.get("claude_timeout_sec", 120))).chat.completions.create(
                model=model, messages=[{"role": "system", "content": system},
                                       {"role": "user", "content": content}])
        except openai.APIConnectionError:
            raise RuntimeError(compat_connection_hint(url))
        except openai.APITimeoutError:
            raise RuntimeError("Модель не ответила вовремя — увеличьте «Таймаут ответа» или возьмите модель поменьше")
        except openai.AuthenticationError:
            raise RuntimeError("Сервер требует API-ключ (или ключ не подходит)")
        except openai.NotFoundError:
            raise RuntimeError(f"Модель {model} не найдена или не загружена — «Проверить подключение» в «Настройках»")
        except openai.APIStatusError as e:
            low = str(e.message).lower()
            if "context" in low and any(w in low for w in ("length", "window", "overflow", "exceed", "too long")):
                raise RuntimeError("Промпт не помещается в контекст модели — увеличьте длину контекста "
                                   "(LM Studio: настройки модели → Context Length, нужно 16–32 тыс. токенов и больше) "
                                   "или сократите промпт")
            raise RuntimeError(f"Ошибка сервера ({e.status_code}): {e.message}")
        u = resp.usage
        if u:
            self.log.info("Своя модель %s: вход %s ток., выход %s ток.", model, u.prompt_tokens, u.completion_tokens)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        return strip_reasoning(text)

    async def _gen_zai(self, system: str, user_msg: str, images: list[Path]) -> str:
        """Z.ai: бесплатные GLM. Запросы всех профилей с одним ключом — по очереди, занятость — пауза и повтор."""
        import openai
        cfg = self.cfg
        key = zai_key(cfg)
        if not key:
            raise RuntimeError("Не указан ключ Z.ai — «Настройки» → «Нейросеть»")
        if getattr(self, "_zai_client_key", None) != key:
            self._zai_client, self._zai_client_key = zai_client(key, asynchronous=True), key
        # Попытки по порядку: пост с картинками — сперва модели, которая их видит; если её отключили —
        # текстовой по одному тексту. Дальше — выбранная текстовая и запасные бесплатные.
        attempts = [(m, user_msg) for m in zai_models_order(str(cfg.get("zai_model", "")).strip() or ZAI_MODELS[0])]
        if images and ZAI_VISION_MODEL not in _zai_gone:
            attempts.insert(0, (ZAI_VISION_MODEL, chat_content(user_msg, images)))
        lock = zai_lock(key)
        if not lock.acquire(blocking=False):
            self.log.info("Z.ai: жду, пока закончится запрос другого аккаунта (бесплатно — по одному за раз)")
            while not lock.acquire(blocking=False):   # не to_thread: при отмене задачи замок не повиснет
                await asyncio.sleep(0.3)
        try:
            (model, content), resp, delays = attempts.pop(0), None, list(ZAI_RETRY_DELAYS)
            while resp is None:
                try:
                    resp = await self._zai_client.with_options(
                        timeout=float(cfg.get("claude_timeout_sec", 120))).chat.completions.create(
                        model=model, messages=[{"role": "system", "content": system},
                                               {"role": "user", "content": content}],
                        extra_body=zai_extra(model))
                    continue
                except openai.AuthenticationError:
                    raise RuntimeError(ZAI_BAD_KEY)
                except openai.APITimeoutError:   # до APIConnectionError: это её подкласс
                    raise RuntimeError("Z.ai не ответил вовремя — увеличьте «Таймаут ответа» в «Настройках»")
                except openai.APIConnectionError:
                    why = "нет связи"
                except openai.APIStatusError as e:
                    code, msg = zai_error(e)
                    if code == "1211" and attempts:   # модель отключили — берём следующую
                        _zai_gone.add(model)
                        nxt, content = attempts.pop(0)
                        self.log.warning("Z.ai больше не знает модель %s — %s", model,
                                         f"пишу по тексту поста, без картинок ({nxt})" if model == ZAI_VISION_MODEL
                                         else f"перехожу на {nxt}. Выберите её в «Настройках», "
                                              "чтобы не видеть это сообщение")
                        model = nxt
                        continue
                    if zai_thinking_rejected(model, e.status_code, msg):
                        continue
                    if not zai_busy(e.status_code, code):
                        raise RuntimeError(zai_error_text(e.status_code, code, msg, model))
                    why = msg
                if not delays:
                    raise RuntimeError(f"Z.ai перегружен или недоступен ({why}) — попробуйте позже")
                d = delays.pop(0)
                self.log.info("Z.ai занят (%s) — повторю через %s с", why, d)
                await asyncio.sleep(d)
        finally:
            lock.release()
        u = resp.usage
        if u:
            self.log.info("Z.ai %s: вход %s ток., выход %s ток.", model, u.prompt_tokens, u.completion_tokens)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        return strip_reasoning(text)

    async def _gen_openai_api(self, system: str, user_msg: str, images: list[Path]) -> str:
        import openai
        cfg = self.cfg
        key = openai_key(cfg)
        if self._openai_client is None or self._openai_client_key != key:
            self._openai_client = openai.AsyncOpenAI(api_key=key)
            self._openai_client_key = key
        if images:
            user_msg += "\n\nК запросу приложены картинки — учти, что на них. Выведи только сам текст поста."
        content = [{"type": "input_text", "text": user_msg}]
        for img in images:
            mime = mimetypes.guess_type(img.name)[0] or "image/jpeg"
            content.append({"type": "input_image",
                            "image_url": f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"})
        model = cfg.get("openai_api_model") or OPENAI_API_DEFAULT
        try:
            resp = await self._openai_client.with_options(
                timeout=float(cfg.get("claude_timeout_sec", 120))).responses.create(
                model=model, instructions=system, input=[{"role": "user", "content": content}])
        except openai.AuthenticationError:
            raise RuntimeError("Ключ OpenAI не подходит — проверьте его в «Настройках»")
        except openai.RateLimitError as e:
            raise RuntimeError(f"OpenAI: лимит или нет средств на балансе — {e.message}")
        except openai.NotFoundError:
            raise RuntimeError(f"Модель {model} недоступна — выберите другую в «Настройках»")
        except openai.APITimeoutError:
            raise RuntimeError("OpenAI API не ответил вовремя")
        except openai.APIConnectionError:
            raise RuntimeError("Нет связи с OpenAI API — проверьте интернет")
        except openai.APIStatusError as e:
            raise RuntimeError(f"Ошибка OpenAI API ({e.status_code}): {e.message}")
        u = resp.usage
        if u:
            cached = getattr(u.input_tokens_details, "cached_tokens", 0) if u.input_tokens_details else 0
            self.log.info("OpenAI %s: вход %s ток. (из кэша %s), выход %s ток.", model,
                     u.input_tokens, cached, u.output_tokens)
        return (resp.output_text or "").strip()

    async def _gen_api(self, system: str, user_msg: str, images: list[Path]) -> str:
        import anthropic
        cfg = self.cfg
        key = api_key(cfg)
        if self._api_client is None or self._api_client_key != key:
            self._api_client = anthropic.AsyncAnthropic(api_key=key)
            self._api_client_key = key
        content = []
        for img in images:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": mimetypes.guess_type(img.name)[0] or "image/jpeg",
                "data": base64.standard_b64encode(img.read_bytes()).decode()}})
        if images:
            user_msg += "\n\nК запросу приложены картинки (выше) — учти, что на них. Выведи только сам текст поста."
        content.append({"type": "text", "text": user_msg})
        model = cfg.get("api_model", API_MODELS[0])
        kw = dict(
            model=model, max_tokens=16000,
            # Промпт большой и одинаковый — кэшируем, повторные запросы в разы дешевле
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
        )
        client = self._api_client.with_options(timeout=float(cfg.get("claude_timeout_sec", 120)))
        try:
            if model.startswith("claude-haiku"):
                resp = await client.messages.create(**kw)
            else:
                kw["output_config"] = {"effort": "medium"}
                try:
                    # Если фильтр модели откажет, API сам повторит запрос на подходящей модели
                    resp = await client.beta.messages.create(
                        **kw, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                except anthropic.BadRequestError as e:
                    if "fallback" not in str(e.message).lower():
                        raise
                    resp = await client.messages.create(**kw)
        except anthropic.AuthenticationError:
            raise RuntimeError("API-ключ не подходит — проверьте его в «Настройках»")
        except anthropic.PermissionDeniedError as e:
            raise RuntimeError(f"Нет доступа к модели {model}: {e.message}")
        except anthropic.RateLimitError:
            raise RuntimeError("Слишком много запросов к API — попробуйте чуть позже")
        except anthropic.BadRequestError as e:
            raise RuntimeError(f"API отклонил запрос: {e.message}")
        except anthropic.APITimeoutError:
            raise RuntimeError("API не ответил вовремя")
        except anthropic.APIConnectionError:
            raise RuntimeError("Нет связи с API Anthropic — проверьте интернет")
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Ошибка API ({e.status_code}): {e.message}")

        u = resp.usage
        self.log.info("API %s: вход %s ток. (из кэша %s), выход %s ток.", resp.model,
                 u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0),
                 u.cache_read_input_tokens or 0, u.output_tokens)
        if resp.stop_reason == "refusal":
            self.log.info("Модель отказалась отвечать на этот пост")
            return ""
        return "".join(b.text for b in resp.content if b.type == "text").strip()
