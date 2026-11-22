"""Настройки приложения и история передач (config.json).

Файл настроек хранится в домашней папке пользователя:
    ~/.lanshare/config.json

Структура:
    {
        "nickname":    "PC-NAME",
        "save_dir":    "C:\\Users\\user\\Downloads",
        "auto_accept": false,
        "device_id":   "<hex>",
        "history":     [ {...}, {...} ]
    }
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

APP_DIR_NAME = ".lanshare"
CONFIG_FILE_NAME = "config.json"

# Максимальное количество записей в истории передач.
MAX_HISTORY_ITEMS = 300


def default_download_dir() -> str:
    """Папка загрузок текущего пользователя (с откатом на домашнюю папку)."""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return str(downloads)
    return str(Path.home())


def default_nickname() -> str:
    """Ник по умолчанию: имя компьютера, а если пусто — имя пользователя."""
    try:
        host = socket.gethostname()
    except Exception:
        host = ""
    if host:
        return host
    try:
        return getpass.getuser()
    except Exception:
        return "LANShare-PC"


def default_config_path() -> Path:
    """Путь к config.json (можно переопределить переменной окружения LANSHARE_HOME)."""
    override = os.environ.get("LANSHARE_HOME")
    if override:
        base = Path(override)
    else:
        base = Path.home() / APP_DIR_NAME
    return base / CONFIG_FILE_NAME


def _defaults() -> dict:
    return {
        "nickname": default_nickname(),
        "save_dir": default_download_dir(),
        "auto_accept": False,
        "device_id": uuid.uuid4().hex,
        "history": [],
    }


class Config:
    """Потокобезопасные настройки с отложенной записью на диск."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else default_config_path()
        self._lock = threading.RLock()
        self._data = _defaults()
        self.load()

    # ------------------------------------------------------------------ I/O

    def load(self) -> dict:
        """Читает config.json; при ошибке/отсутствии использует значения по умолчанию."""
        with self._lock:
            data = _defaults()
            try:
                if self.path.is_file():
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for key in data:
                            if key in raw:
                                data[key] = raw[key]
            except Exception:
                # Повреждённый конфиг не должен ломать запуск приложения.
                data = _defaults()

            if not isinstance(data.get("device_id"), str) or not data["device_id"]:
                data["device_id"] = uuid.uuid4().hex
            if not isinstance(data.get("nickname"), str) or not data["nickname"].strip():
                data["nickname"] = default_nickname()
            if not isinstance(data.get("save_dir"), str) or not data["save_dir"]:
                data["save_dir"] = default_download_dir()
            data["auto_accept"] = bool(data.get("auto_accept", False))
            if not isinstance(data.get("history"), list):
                data["history"] = []

            self._data = data
            return data

    def save(self) -> None:
        """Атомарно сохраняет настройки на диск."""
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            except Exception:
                pass

    # ------------------------------------------------------------ примитивы

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value, autosave: bool = True) -> None:
        with self._lock:
            self._data[key] = value
        if autosave:
            self.save()

    # ------------------------------------------------------------- свойства

    @property
    def nickname(self) -> str:
        return self.get("nickname", default_nickname())

    @nickname.setter
    def nickname(self, value: str) -> None:
        value = (value or "").strip() or default_nickname()
        self.set("nickname", value)

    @property
    def save_dir(self) -> str:
        return self.get("save_dir", default_download_dir())

    @save_dir.setter
    def save_dir(self, value: str) -> None:
        self.set("save_dir", str(value))

    @property
    def auto_accept(self) -> bool:
        return bool(self.get("auto_accept", False))

    @auto_accept.setter
    def auto_accept(self, value: bool) -> None:
        self.set("auto_accept", bool(value))

    @property
    def device_id(self) -> str:
        return self.get("device_id", "")

    # -------------------------------------------------------------- история

    def history(self) -> list:
        with self._lock:
            return list(self._data.get("history", []))

    def add_history(self, entry: dict) -> dict:
        """Добавляет запись истории (новые — сверху) и возвращает её."""
        record = {
            "id": entry.get("id") or uuid.uuid4().hex[:12],
            "direction": entry.get("direction", "in"),
            "name": entry.get("name", ""),
            "size": int(entry.get("size", 0)),
            "is_folder": bool(entry.get("is_folder", False)),
            "peer_name": entry.get("peer_name", ""),
            "peer_ip": entry.get("peer_ip", ""),
            "status": entry.get("status", "waiting"),
            "path": entry.get("path", ""),
            "timestamp": float(entry.get("timestamp") or time.time()),
        }
        with self._lock:
            items = self._data.setdefault("history", [])
            items.insert(0, record)
            del items[MAX_HISTORY_ITEMS:]
        self.save()
        return record

    def update_history(self, entry_id: str, autosave: bool = True, **fields) -> None:
        """Обновляет поля записи истории по её id."""
        with self._lock:
            for item in self._data.get("history", []):
                if item.get("id") == entry_id:
                    item.update(fields)
                    break
        if autosave:
            self.save()

    def clear_history(self) -> None:
        with self._lock:
            self._data["history"] = []
        self.save()
