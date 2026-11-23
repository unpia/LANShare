"""TCP-сервер и TCP-клиент передачи файлов и папок.

Протокол (одно TCP-соединение = один файл или одна папка):

    1. Клиент -> Сервер: JSON-заголовок + b"\\n"
       {"magic":"LANSHARE1","version":1,"type":"file"|"folder",
        "transfer_id":"...","name":"report.pdf","size":12345,
        "sender_name":"PC-A","sender_id":"..."}
       Для папки name — имя папки, size — размер временного .zip.

    2. Сервер -> Клиент: JSON-ответ + b"\\n"
       {"status":"ok","mode":"new"|"overwrite"|"copy","save_name":"...",
        "name":"PC-B"}                      — можно передавать
       {"status":"reject","reason":"..."}   — отказ

    3. Клиент -> Сервер: ровно `size` байт полезных данных (буфер 1 МБ).

    4. Сервер -> Клиент: JSON-подтверждение + b"\\n"
       {"status":"done","path":"C:\\\\...\\\\report.pdf"}

Передача не ограничивается по скорости: TCP-соединение без искусственных
задержек, крупный буфер чтения/записи 1 МБ.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

from utils.archiver import remove_path, unique_path, unzip_archive, zip_folder

TCP_PORT = 50506
BUFFER_SIZE = 1024 * 1024          # 1 МБ — крупный буфер для максимальной скорости
HEADER_LIMIT = 32 * 1024
MAGIC = "LANSHARE1"
PROTOCOL_VERSION = 1
PROGRESS_INTERVAL = 0.25           # период обновления скорости, сек
HANDSHAKE_TIMEOUT = 120.0          # ожидание решения пользователя на приём
DATA_TIMEOUT = 120.0               # таймаут на один сетевой блок данных
ACK_TIMEOUT = 600.0                # ожидание подтверждения (папка распаковывается)

# Итоговые статусы передачи
STATUS_COMPLETED = "completed"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"


# ----------------------------------------------------------------- утилиты

def sanitize_filename(name: str) -> str:
    """Убирает из имени файла пути и недопустимые символы."""
    name = str(name or "").replace("\\", "/").split("/")[-1].strip()
    for ch in '<>:"|?*\0':
        name = name.replace(ch, "_")
    name = name.strip(" .")
    return name or "file"


def _send_json(sock: socket.socket, payload: dict) -> None:
    sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")


def _recv_line(sock: socket.socket, limit: int = HEADER_LIMIT) -> bytes:
    """Читает строку до b'\\n' (заголовки небольшие, читаем побайтово)."""
    buffer = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("соединение закрыто до получения заголовка")
        if chunk == b"\n":
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise ValueError("заголовок превышает допустимый размер")
    return bytes(buffer)


def _recv_json(sock: socket.socket) -> dict:
    payload = json.loads(_recv_line(sock).decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("ожидался JSON-объект")
    return payload


# ------------------------------------------------------------------ сервер

class TransferServer(threading.Thread):
    """Принимает входящие TCP-соединения и сохраняет данные на диск."""

    def __init__(
        self,
        port: int = TCP_PORT,
        get_save_dir=None,
        get_nickname=None,
        on_offer=None,
        on_progress=None,
        on_finished=None,
        on_log=None,
    ) -> None:
        super().__init__(daemon=True, name="LANShare-TCP")
        self.port = port
        self.get_save_dir = get_save_dir or (lambda: str(Path.home() / "Downloads"))
        self.get_nickname = get_nickname or (lambda: "LANShare")
        self.on_offer = on_offer
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.on_log = on_log

        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------ управление

    def stop(self) -> None:
        self._stop_event.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", self.port))
            sock.listen(16)
            sock.settimeout(0.5)
        except OSError as exc:
            self._log(f"Не удалось открыть TCP-порт {self.port}: {exc}")
            return

        self._sock = sock
        self._log(f"TCP-сервер приёма запущен на порту {self.port}")

        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                worker = threading.Thread(
                    target=self._handle, args=(conn, addr),
                    daemon=True, name=f"LANShare-Recv-{addr[0]}",
                )
                worker.start()
        finally:
            try:
                sock.close()
            except Exception:
                pass
            self._sock = None

    # ---------------------------------------------------------- обработчик

    def _handle(self, conn: socket.socket, addr) -> None:
        peer_ip = addr[0]
        transfer_id = uuid.uuid4().hex[:12]
        target_path = None
        tmp_zip = None
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            conn.settimeout(HANDSHAKE_TIMEOUT)

            header = _recv_json(conn)
            if header.get("magic") != MAGIC:
                _send_json(conn, {"status": "error", "reason": "неизвестный протокол"})
                return
            item_type = header.get("type")
            if item_type not in ("file", "folder"):
                _send_json(conn, {"status": "error", "reason": "неизвестный тип передачи"})
                return

            transfer_id = str(header.get("transfer_id") or transfer_id)
            is_folder = item_type == "folder"
            name = sanitize_filename(header.get("name") or "file")
            try:
                size = max(int(header.get("size") or 0), 0)
            except (TypeError, ValueError):
                size = 0

            offer = {
                "id": transfer_id,
                "type": item_type,
                "name": name,
                "size": size,
                "is_folder": is_folder,
                "sender_name": str(header.get("sender_name") or peer_ip),
                "sender_id": str(header.get("sender_id") or ""),
                "peer_ip": peer_ip,
            }
            self._emit_log(f"Входящая передача: {name} ({size} байт) от {peer_ip}")

            decision = self._ask_user(offer, peer_ip)
            decision.setdefault("name", self.get_nickname())

            if decision.get("status") != "ok":
                _send_json(conn, decision)
                self._finish(transfer_id, STATUS_REJECTED, "",
                             str(decision.get("reason") or "отклонено пользователем"))
                return

            save_dir = Path(self.get_save_dir())
            save_dir.mkdir(parents=True, exist_ok=True)
            target_path, mode = self._resolve_destination(save_dir, name, is_folder,
                                                          decision.get("mode", "new"))
            _send_json(conn, {"status": "ok", "mode": mode,
                              "save_name": target_path.name,
                              "name": self.get_nickname()})

            if is_folder:
                tmp_zip = save_dir / f".lanshare_{transfer_id}.zip"
                write_target = tmp_zip
            else:
                write_target = target_path

            conn.settimeout(DATA_TIMEOUT)
            self._receive_payload(conn, write_target, size, transfer_id,
                                  offer["sender_name"], peer_ip, is_folder)

            if is_folder:
                unzip_archive(tmp_zip, target_path)
                remove_path(tmp_zip)
                tmp_zip = None

            conn.settimeout(30.0)
            try:
                _send_json(conn, {"status": "done", "path": str(target_path)})
            except OSError:
                pass
            self._finish(transfer_id, STATUS_COMPLETED, str(target_path), "")

        except (ConnectionError, socket.timeout) as exc:
            self._cleanup_partial(tmp_zip, target_path)
            self._finish(transfer_id, STATUS_FAILED, "", f"соединение прервано ({exc})")
        except Exception as exc:  # noqa: BLE001 — сервер не должен падать
            self._cleanup_partial(tmp_zip, target_path)
            self._finish(transfer_id, STATUS_FAILED, "", f"ошибка приёма: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _ask_user(self, offer: dict, peer_ip: str) -> dict:
        """Спрашивает UI о решении (может блокироваться, пока пользователь думает)."""
        if not self.on_offer:
            return {"status": "reject", "reason": "приём недоступен"}
        try:
            decision = self.on_offer(offer, peer_ip)
        except Exception as exc:  # noqa: BLE001
            return {"status": "reject", "reason": f"ошибка подтверждения: {exc}"}
        if not isinstance(decision, dict):
            return {"status": "reject", "reason": "приём отменён"}
        return decision

    @staticmethod
    def _resolve_destination(save_dir: Path, name: str, is_folder: bool, mode: str):
        """Определяет итоговый путь с учётом защиты от перезаписи."""
        target = save_dir / name
        mode = mode if mode in ("new", "overwrite", "copy") else "new"
        if target.exists():
            if mode == "overwrite":
                remove_path(target)
                mode = "overwrite"
            elif mode == "new":
                # Пользователь подтвердил приём нового файла, но файл появился —
                # стараемся ничего не потерять и сохраняем копией.
                mode = "copy"
        if mode == "copy":
            target = unique_path(target, is_dir=is_folder)
        return target, mode

    def _receive_payload(self, conn, write_target: Path, size: int, transfer_id: str,
                         sender_name: str, peer_ip: str, is_folder: bool) -> None:
        """Читает ровно size байт и пишет на диск, считая скорость в МБ/с."""
        write_target.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        started = time.monotonic()
        last_emit = 0.0

        with open(write_target, "wb", buffering=BUFFER_SIZE) as out:
            while received < size:
                chunk = conn.recv(min(BUFFER_SIZE, size - received))
                if not chunk:
                    raise ConnectionError("поток данных прерван")
                out.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_emit >= PROGRESS_INTERVAL:
                    last_emit = now
                    speed = received / max(now - started, 1e-6) / (1024 * 1024)
                    self._emit_progress(transfer_id, received, size, speed)

        elapsed = max(time.monotonic() - started, 1e-6)
        self._emit_progress(transfer_id, size, size, size / elapsed / (1024 * 1024))

    @staticmethod
    def _cleanup_partial(tmp_zip, target_path) -> None:
        if tmp_zip:
            remove_path(tmp_zip)
        if target_path:
            remove_path(target_path)

    # ------------------------------------------------------------- колбэки

    def _emit_progress(self, transfer_id: str, transferred: int, total: int, speed: float) -> None:
        if self.on_progress:
            try:
                self.on_progress(transfer_id, transferred, total, speed)
            except Exception:
                pass

    def _finish(self, transfer_id: str, status: str, path: str, error: str) -> None:
        if self.on_finished:
            try:
                self.on_finished(transfer_id, status, path, error)
            except Exception:
                pass

    def _emit_log(self, message: str) -> None:
        self._log(message)

    def _log(self, message: str) -> None:
        if self.on_log:
            try:
                self.on_log(message)
            except Exception:
                pass


# ------------------------------------------------------------------ клиент

class TransferClient:
    """Отправляет файлы и папки другому устройству (по одному элементу за раз)."""

    def __init__(self, sender_name: str, sender_id: str, port: int = TCP_PORT) -> None:
        self.sender_name = sender_name
        self.sender_id = sender_id
        self.port = port

    def send_path(self, peer_ip: str, path, transfer_id: str,
                  on_progress=None, on_stage=None, peer_port: int | None = None) -> dict:
        """Отправляет файл или папку (папка предварительно упаковывается в .zip).

        :param on_progress: колбэк (transferred, total, speed_mb_s)
        :param on_stage:    колбэк (stage_str) — 'packing' | 'connecting' | 'sending'
        :returns: {"status", "path", "error", "peer_name"}
        """
        src = Path(path)
        if not src.exists():
            return {"status": STATUS_FAILED, "path": "", "error": "путь не найден"}

        is_folder = src.is_dir()
        tmp_zip = None
        try:
            if is_folder:
                self._stage(on_stage, "packing")
                tmp_zip = zip_folder(
                    src,
                    progress_cb=lambda done, total: self._progress(on_progress, done, total, 0.0),
                )
                send_path = Path(tmp_zip)
                display_name = src.name
            else:
                send_path = src
                display_name = src.name

            size = os.path.getsize(send_path)
            self._stage(on_stage, "connecting")

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                except OSError:
                    pass
                sock.settimeout(10.0)
                sock.connect((peer_ip, self.port or TCP_PORT))

                sock.settimeout(HANDSHAKE_TIMEOUT)
                _send_json(sock, {
                    "magic": MAGIC,
                    "version": PROTOCOL_VERSION,
                    "type": "folder" if is_folder else "file",
                    "transfer_id": transfer_id,
                    "name": display_name,
                    "size": size,
                    "sender_name": self.sender_name,
                    "sender_id": self.sender_id,
                })

                answer = _recv_json(sock)
                peer_name = str(answer.get("name") or "")
                if answer.get("status") != "ok":
                    return {
                        "status": STATUS_REJECTED,
                        "path": "",
                        "error": str(answer.get("reason") or "получатель отклонил передачу"),
                        "peer_name": peer_name,
                    }

                self._stage(on_stage, "sending")
                sock.settimeout(DATA_TIMEOUT)
                self._send_payload(sock, send_path, size, on_progress)

                sock.settimeout(ACK_TIMEOUT)
                try:
                    ack = _recv_json(sock)
                    remote_path = str(ack.get("path") or "")
                except (socket.timeout, ConnectionError, ValueError):
                    remote_path = ""

            return {"status": STATUS_COMPLETED, "path": remote_path,
                    "error": "", "peer_name": peer_name}
        except (ConnectionError, socket.timeout) as exc:
            return {"status": STATUS_FAILED, "path": "", "error": f"сеть недоступна ({exc})"}
        except Exception as exc:  # noqa: BLE001
            return {"status": STATUS_FAILED, "path": "", "error": str(exc)}
        finally:
            if tmp_zip:
                remove_path(tmp_zip)

    # ------------------------------------------------------------- служебные

    def _send_payload(self, sock: socket.socket, path: Path, size: int, on_progress) -> None:
        """Отправляет файл блоками по 1 МБ, считая скорость в МБ/с."""
        sent = 0
        started = time.monotonic()
        last_emit = 0.0

        with open(path, "rb", buffering=BUFFER_SIZE) as src:
            while sent < size:
                chunk = src.read(BUFFER_SIZE)
                if not chunk:
                    break
                sock.sendall(chunk)
                sent += len(chunk)
                now = time.monotonic()
                if now - last_emit >= PROGRESS_INTERVAL:
                    last_emit = now
                    speed = sent / max(now - started, 1e-6) / (1024 * 1024)
                    self._progress(on_progress, sent, size, speed)

        elapsed = max(time.monotonic() - started, 1e-6)
        self._progress(on_progress, size, size, size / elapsed / (1024 * 1024))

    @staticmethod
    def _progress(callback, transferred: int, total: int, speed: float) -> None:
        if callback:
            try:
                callback(transferred, total, speed)
            except Exception:
                pass

    @staticmethod
    def _stage(callback, stage: str) -> None:
        if callback:
            try:
                callback(stage)
            except Exception:
                pass
