"""UDP-сервис обнаружения компьютеров в локальной сети.

Логика:
  * каждые 2 секунды рассылаем broadcast-пакет со своим ником, IP и TCP-портом;
  * слушаем тот же UDP-порт и запоминаем ответы других устройств;
  * если устройство молчит дольше 6 секунд — удаляем его из списка.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass

UDP_PORT = 50505
BROADCAST_INTERVAL = 2.0     # период рассылки, сек
PEER_TIMEOUT = 6.0           # через сколько секунд без ответа пир удаляется
MAGIC = "LANSHARE1"
MAX_PACKET = 2048


@dataclass
class Peer:
    """Найденное в сети устройство."""

    device_id: str
    name: str
    ip: str
    tcp_port: int
    last_seen: float

    @property
    def age(self) -> float:
        return time.time() - self.last_seen


def local_ip_addresses() -> set:
    """Набор IPv4-адресов текущего компьютера (без 127.0.0.1)."""
    addresses = set()
    # Определяем исходящий интерфейс через «пустое» UDP-подключение.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            addresses.add(probe.getsockname()[0])
        finally:
            probe.close()
    except Exception:
        pass
    # Добавляем адреса по имени хоста.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except Exception:
        pass
    return {ip for ip in addresses if ip and not ip.startswith("127.")}


def broadcast_addresses() -> set:
    """Адреса для broadcast: 255.255.255.255 + x.y.z.255 для каждого локального IP."""
    targets = {"255.255.255.255"}
    for ip in local_ip_addresses():
        parts = ip.split(".")
        if len(parts) == 4:
            targets.add(".".join(parts[:3] + ["255"]))
    return targets


class DiscoveryService(threading.Thread):
    """Фоновый поток: рассылка анонсов + приём анонсов других устройств."""

    def __init__(
        self,
        get_nickname,
        tcp_port: int,
        device_id: str,
        on_peers_changed=None,
        on_log=None,
        port: int = UDP_PORT,
    ) -> None:
        super().__init__(daemon=True, name="LANShare-Discovery")
        self.port = port
        self.tcp_port = tcp_port
        self.device_id = device_id
        self.get_nickname = get_nickname
        self.on_peers_changed = on_peers_changed
        self.on_log = on_log

        self._peers: dict = {}
        self._lock = threading.Lock()
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

    def peers(self) -> list:
        """Текущий список пиров, отсортированный по IP-адресу."""
        with self._lock:
            return sorted(self._peers.values(), key=lambda p: _ip_sort_key(p.ip))

    # ---------------------------------------------------------------- поток

    def run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", self.port))
            sock.settimeout(0.5)
        except OSError as exc:
            self._log(f"Не удалось открыть UDP-порт {self.port}: {exc}")
            return

        self._sock = sock
        self._log(f"UDP-обнаружение запущено на порту {self.port}")
        last_announce = 0.0

        try:
            while not self._stop_event.is_set():
                now = time.time()
                if now - last_announce >= BROADCAST_INTERVAL:
                    last_announce = now
                    self._announce(sock)
                try:
                    data, addr = sock.recvfrom(MAX_PACKET)
                except socket.timeout:
                    data, addr = None, None
                except OSError:
                    break
                if data:
                    if self._handle_packet(data, addr):
                        self._notify()
                if self._prune():
                    self._notify()
        finally:
            try:
                sock.close()
            except Exception:
                pass
            self._sock = None

    # ------------------------------------------------------------- внутреннее

    def _announce(self, sock: socket.socket) -> None:
        payload = {
            "magic": MAGIC,
            "type": "hello",
            "device_id": self.device_id,
            "name": self.get_nickname(),
            "tcp_port": self.tcp_port,
            "version": 1,
            "ts": time.time(),
        }
        try:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except Exception:
            return
        for target in broadcast_addresses():
            try:
                sock.sendto(raw, (target, self.port))
            except OSError:
                continue

    def _handle_packet(self, data: bytes, addr) -> bool:
        """Обрабатывает входящий пакет. True — список пиров изменился."""
        try:
            payload = json.loads(data.decode("utf-8", errors="ignore"))
        except Exception:
            return False
        if not isinstance(payload, dict) or payload.get("magic") != MAGIC:
            return False
        if payload.get("type") != "hello":
            return False

        device_id = str(payload.get("device_id") or "").strip()
        if not device_id or device_id == self.device_id:
            return False

        ip = addr[0] if addr else payload.get("ip", "")
        name = str(payload.get("name") or ip).strip() or ip
        try:
            tcp_port = int(payload.get("tcp_port") or 0)
        except (TypeError, ValueError):
            tcp_port = 0

        changed = False
        with self._lock:
            existing = self._peers.get(device_id)
            if existing is None:
                self._peers[device_id] = Peer(device_id, name, ip, tcp_port, time.time())
                changed = True
            else:
                if (existing.name, existing.ip, existing.tcp_port) != (name, ip, tcp_port):
                    changed = True
                existing.name = name
                existing.ip = ip
                existing.tcp_port = tcp_port
                existing.last_seen = time.time()
        return changed

    def _prune(self) -> bool:
        """Удаляет пиров, молчащих дольше PEER_TIMEOUT. True — список изменился."""
        now = time.time()
        with self._lock:
            stale = [pid for pid, peer in self._peers.items()
                     if now - peer.last_seen > PEER_TIMEOUT]
            for pid in stale:
                del self._peers[pid]
        if stale:
            self._log(f"Устройства отключились: {len(stale)}")
        return bool(stale)

    def _notify(self) -> None:
        if not self.on_peers_changed:
            return
        try:
            self.on_peers_changed(self.peers())
        except Exception:
            pass

    def _log(self, message: str) -> None:
        if self.on_log:
            try:
                self.on_log(message)
            except Exception:
                pass


def _ip_sort_key(ip: str):
    """Ключ сортировки для IP-адресов вида 192.168.1.10."""
    try:
        return tuple(int(part) for part in ip.split("."))
    except (TypeError, ValueError):
        return (999, 999, 999, 999)
