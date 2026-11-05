"""
Черновики постов и история публикаций. Всё хранится в drafts.json (общий для всех профилей),
картинки — в drafts/<id>/. Переживает перезапуск и обновление программы.

Статусы черновика:
  review     — на проверке (в том числе пока пишется текст или рисуется картинка — см. busy)
  scheduled  — стоит в отложенных постах Telegram
  published  — опубликован
  cancelled  — отменён (можно вернуть на проверку)
"""
import json
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from core import DRAFTS_DIR, DRAFTS_FILE, log

REVIEW, SCHEDULED, PUBLISHED, CANCELLED = "review", "scheduled", "published", "cancelled"


@dataclass
class Draft:
    id: str
    profile: str                 # id профиля
    topic: str
    channel: str = ""            # запись из списка каналов профиля (core.normalize_channel)
    text: str = ""
    image: str = ""              # путь к картинке; пусто — пост без картинки
    images: list[str] = field(default_factory=list)   # все варианты картинки (последний — новый)
    status: str = REVIEW
    created: str = ""
    text_source: str = ""        # кто написал текст: имя нейросети
    image_source: str = ""
    generated: str = ""          # текст, выданный нейросетью, — отличается от text, значит, правили вручную
    error: str = ""
    publish_at: str = ""         # когда выйдет / вышел (ISO с часовым поясом)
    msg_ids: list[int] = field(default_factory=list)   # сообщения в Telegram (картинка и текст отдельно — два)
    chat_id: int = 0
    link: str = ""
    # Во время работы, на диск не пишутся
    text_busy: bool = False
    image_busy: bool = False

    RUNTIME = ("text_busy", "image_busy")

    @property
    def busy(self) -> bool:
        return self.text_busy or self.image_busy

    @property
    def image_path(self) -> Path | None:
        return Path(self.image) if self.image and Path(self.image).exists() else None

    @property
    def folder(self) -> Path:
        return DRAFTS_DIR / self.id

    def when(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.publish_at) if self.publish_at else None
        except ValueError:
            return None

    def to_json(self) -> dict:
        d = asdict(self)
        for k in self.RUNTIME:
            d.pop(k, None)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Draft | None":
        names = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in d.items() if k in names and k not in cls.RUNTIME})
        except TypeError:
            return None


def write_atomic(path: Path, text: str, tries: int = 20):
    """Файл целиком или никак: пишем во временный и подменяем. В Windows подмену иногда на миг
    не дают антивирус или индексатор («Отказано в доступе») — тогда повторяем."""
    tmp = path.with_name(path.name + f".{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if i == tries - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (i + 1))


class DraftStore:
    """Черновики всех профилей. Меняют их и окно, и движки (каждый в своём потоке) —
    сохранение под замком, файл пишется целиком через временный."""

    def __init__(self, path: Path = DRAFTS_FILE):
        self.path = path
        self.lock = threading.RLock()
        self.items: dict[str, Draft] = {}
        self.load()

    def load(self):
        data = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.error("drafts.json не прочитан (%s) — черновики начнутся заново", e)
                backup = self.path.with_suffix(".broken.json")
                shutil.copy(self.path, backup)
        with self.lock:
            self.items = {}
            for d in data if isinstance(data, list) else []:
                dr = Draft.from_json(d) if isinstance(d, dict) else None
                if dr:
                    self.items[dr.id] = dr

    def save(self):
        with self.lock:
            data = [d.to_json() for d in self.items.values()]
            write_atomic(self.path, json.dumps(data, ensure_ascii=False, indent=1))

    def new(self, profile: str, topic: str, channel: str) -> Draft:
        d = Draft(id=datetime.now().strftime("%y%m%d%H%M%S") + secrets.token_hex(2), profile=profile,
                  topic=topic.strip(), channel=channel, created=datetime.now().astimezone().isoformat(timespec="seconds"))
        with self.lock:
            self.items[d.id] = d
        self.save()
        return d

    def get(self, did: str) -> Draft | None:
        return self.items.get(did)

    def list(self, status: str | None = None, profile: str | None = None) -> list[Draft]:
        with self.lock:
            return [d for d in self.items.values()
                    if (status is None or d.status == status) and (profile is None or d.profile == profile)]

    def recent_texts(self, profile: str, channel: str, n: int) -> list[str]:
        """Последние опубликованные и запланированные посты канала — нейросеть их не повторит."""
        done = [d for d in self.list(profile=profile) if d.channel == channel and d.status in (SCHEDULED, PUBLISHED)
                and d.text.strip()]
        done.sort(key=lambda d: d.publish_at or d.created)
        return [d.text for d in done[-n:]] if n > 0 else []

    def delete(self, did: str):
        with self.lock:
            d = self.items.pop(did, None)
        if d:
            shutil.rmtree(d.folder, ignore_errors=True)
            self.save()

    def remove_profile(self, profile: str):
        for d in self.list(profile=profile):
            self.delete(d.id)
