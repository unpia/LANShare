"""Главное окно LANShare: список устройств, отправка, история передач,
а также диалоги подтверждения приёма и защиты от перезаписи файлов."""

from __future__ import annotations

import queue
import threading
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from network.discovery import DiscoveryService, UDP_PORT, local_ip_addresses
from network.transfer import (
    TCP_PORT,
    TransferClient,
    TransferServer,
    sanitize_filename,
)
from ui import widgets as W
from utils.archiver import folder_total_size
from utils.config import Config


class MainWindow(ctk.CTk):
    """Главное окно приложения."""

    def __init__(self) -> None:
        super().__init__()

        self.config_store = Config()
        self._ui_queue: "queue.Queue" = queue.Queue()

        # Копии настроек для чтения из сетевых потоков (без обращения к tkinter).
        self._nickname = self.config_store.nickname
        self._save_dir = self.config_store.save_dir
        self._auto_accept = self.config_store.auto_accept

        self._devices: dict = {}          # device_id -> Peer
        self._device_cards: dict = {}     # device_id -> DeviceCard
        self._selected_device_id: str | None = None
        self._rows: dict = {}             # transfer_id -> TransferRow

        self.title("LANShare — локальный файлообменник")
        self.geometry("1080x700")
        self.minsize(960, 620)
        self.configure(fg_color=W.BG)

        self._build_layout()
        self._start_services()

        self.after(80, self._drain_ui_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # =================================================================== UI

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=310)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)

        left = ctk.CTkFrame(self, fg_color=W.PANEL, corner_radius=0)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(3, weight=1)

        right = ctk.CTkFrame(self, fg_color=W.BG, corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=1)

        self._build_left_panel(left)
        self._build_right_panel(right)
        self._build_status_bar()

    # --------------------------------------------------------------- левая

    def _build_left_panel(self, parent) -> None:
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(16, 6))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="LANShare", font=W.font(18, bold=True),
                     text_color=W.TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="Передача файлов по локальной сети без интернета",
                     font=W.font(10), text_color=W.TEXT_DIM, anchor="w",
                     wraplength=270, justify="left").grid(row=1, column=0, sticky="w",
                                                          pady=(2, 0))

        # ---- ник
        nick_box = ctk.CTkFrame(parent, fg_color="transparent")
        nick_box.grid(row=1, column=0, sticky="ew", padx=14, pady=(8, 0))
        nick_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(nick_box, text="ВАШ НИК", font=W.font(10, bold=True),
                     text_color=W.TEXT_DIM, anchor="w").grid(row=0, column=0,
                                                             columnspan=2, sticky="w")
        self.nick_entry = ctk.CTkEntry(nick_box, height=30, corner_radius=4,
                                       fg_color="#1f1f1f", border_color=W.BORDER,
                                       text_color=W.TEXT, font=W.font(12))
        self.nick_entry.insert(0, self._nickname)
        self.nick_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ctk.CTkButton(nick_box, text="Применить", width=92, height=30, corner_radius=4,
                      font=W.font(11), fg_color=W.ACCENT, hover_color=W.ACCENT_HOVER,
                      command=self._apply_nickname).grid(row=1, column=1, padx=(6, 0),
                                                         pady=(4, 0))

        # ---- устройства
        dev_header = ctk.CTkFrame(parent, fg_color="transparent")
        dev_header.grid(row=2, column=0, sticky="ew", padx=14, pady=(16, 4))
        dev_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(dev_header, text="УСТРОЙСТВА В СЕТИ", font=W.font(10, bold=True),
                     text_color=W.TEXT_DIM, anchor="w").grid(row=0, column=0, sticky="w")
        self.dev_count_lbl = ctk.CTkLabel(dev_header, text="0", font=W.font(10, bold=True),
                                          text_color=W.ACCENT, anchor="e")
        self.dev_count_lbl.grid(row=0, column=1, sticky="e")

        self.device_list = ctk.CTkScrollableFrame(parent, fg_color=W.PANEL,
                                                  corner_radius=0)
        self.device_list.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 6))
        self.device_list.grid_columnconfigure(0, weight=1)

        self.dev_empty_lbl = ctk.CTkLabel(
            self.device_list,
            text="Поиск устройств…\nУбедитесь, что на другом ПК\nзапущен LANShare",
            font=W.font(11), text_color=W.TEXT_DIM, justify="center")
        self.dev_empty_lbl.grid(row=0, column=0, sticky="ew", pady=24)

        # ---- папка сохранения
        save_box = ctk.CTkFrame(parent, fg_color="transparent")
        save_box.grid(row=4, column=0, sticky="ew", padx=14, pady=(8, 0))
        save_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(save_box, text="ПАПКА СОХРАНЕНИЯ", font=W.font(10, bold=True),
                     text_color=W.TEXT_DIM, anchor="w").grid(row=0, column=0, sticky="w")
        self.save_dir_lbl = ctk.CTkLabel(save_box, text=self._save_dir, font=W.font(10),
                                         text_color=W.TEXT, anchor="w", justify="left",
                                         wraplength=280)
        self.save_dir_lbl.grid(row=1, column=0, sticky="ew", pady=(4, 4))
        ctk.CTkButton(save_box, text="Выбрать папку", height=30, corner_radius=4,
                      font=W.font(11), fg_color=W.CARD, hover_color=W.CARD_HOVER,
                      border_width=1, border_color=W.BORDER, text_color=W.TEXT,
                      command=self._choose_save_dir).grid(row=2, column=0, sticky="ew")

        self.auto_accept_var = ctk.BooleanVar(value=self._auto_accept)
        ctk.CTkCheckBox(parent, text="Принимать все файлы автоматически",
                        variable=self.auto_accept_var, font=W.font(11),
                        text_color=W.TEXT, fg_color=W.ACCENT, hover_color=W.ACCENT_HOVER,
                        border_color=W.BORDER, checkbox_width=18, checkbox_height=18,
                        command=self._toggle_auto_accept).grid(row=5, column=0, sticky="w",
                                                               padx=14, pady=(12, 4))

        self.local_ip_lbl = ctk.CTkLabel(
            parent, text=f"Мой IP: {', '.join(sorted(local_ip_addresses())) or '—'}",
            font=W.font(10), text_color=W.TEXT_DIM, anchor="w", wraplength=280,
            justify="left")
        self.local_ip_lbl.grid(row=6, column=0, sticky="ew", padx=14, pady=(6, 14))

    # -------------------------------------------------------------- правая

    def _build_right_panel(self, parent) -> None:
        toolbar = ctk.CTkFrame(parent, fg_color=W.PANEL, corner_radius=0)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(2, weight=1)

        ctk.CTkButton(toolbar, text="Отправить файлы", width=150, height=34,
                      corner_radius=4, font=W.font(12, bold=True),
                      fg_color=W.ACCENT, hover_color=W.ACCENT_HOVER,
                      command=self._send_files).grid(row=0, column=0, padx=(14, 8),
                                                     pady=12, sticky="w")
        ctk.CTkButton(toolbar, text="Отправить папку", width=150, height=34,
                      corner_radius=4, font=W.font(12, bold=True),
                      fg_color=W.CARD, hover_color=W.CARD_HOVER, text_color=W.TEXT,
                      border_width=1, border_color=W.BORDER,
                      command=self._send_folder).grid(row=0, column=1, padx=(0, 8),
                                                      pady=12, sticky="w")
        self.target_lbl = ctk.CTkLabel(toolbar, text="Получатель не выбран",
                                       font=W.font(11), text_color=W.WARN, anchor="e")
        self.target_lbl.grid(row=0, column=2, sticky="e", padx=(8, 8), pady=12)

        ctk.CTkButton(toolbar, text="Очистить историю", width=130, height=34,
                      corner_radius=4, font=W.font(11), fg_color=W.CARD,
                      hover_color=W.CARD_HOVER, text_color=W.TEXT,
                      border_width=1, border_color=W.BORDER,
                      command=self._clear_history).grid(row=0, column=3, padx=(0, 14),
                                                        pady=12, sticky="e")

        hist_header = ctk.CTkFrame(parent, fg_color="transparent")
        hist_header.grid(row=1, column=0, sticky="ew", padx=14, pady=(12, 4))
        hist_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hist_header, text="ИСТОРИЯ ПЕРЕДАЧ", font=W.font(10, bold=True),
                     text_color=W.TEXT_DIM, anchor="w").grid(row=0, column=0, sticky="w")

        self.history_list = ctk.CTkScrollableFrame(parent, fg_color=W.BG, corner_radius=0)
        self.history_list.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.history_list.grid_columnconfigure(0, weight=1)

        self.hist_empty_lbl = ctk.CTkLabel(
            self.history_list,
            text="История пуста. Выберите устройство слева и отправьте файлы.",
            font=W.font(11), text_color=W.TEXT_DIM)
        self.hist_empty_lbl.grid(row=0, column=0, sticky="ew", pady=20)

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=W.PANEL, corner_radius=0, height=26)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.status_lbl = ctk.CTkLabel(bar, text=self._startup_status(),
                                       font=W.font(10), text_color=W.TEXT_DIM,
                                       anchor="w")
        self.status_lbl.grid(row=0, column=0, sticky="ew", padx=14, pady=4)

    def _startup_status(self) -> str:
        return (f"UDP-поиск: порт {UDP_PORT} · TCP-приём: порт {TCP_PORT} · "
                f"ник: {self._nickname}")

    # ============================================================== сервисы

    def _start_services(self) -> None:
        self.server = TransferServer(
            port=TCP_PORT,
            get_save_dir=lambda: self._save_dir,
            get_nickname=lambda: self._nickname,
            on_offer=self._handle_offer,
            on_progress=self._on_incoming_progress,
            on_finished=self._on_incoming_finished,
            on_log=self._log,
        )
        self.server.start()

        self.discovery = DiscoveryService(
            get_nickname=lambda: self._nickname,
            tcp_port=TCP_PORT,
            device_id=self.config_store.device_id,
            on_peers_changed=lambda peers: self._post(
                lambda: self._refresh_devices(peers)),
            on_log=self._log,
        )
        self.discovery.start()

    def _on_close(self) -> None:
        try:
            self.discovery.stop()
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            pass
        self.destroy()

    # ==================================================== очередь UI-задач

    def _post(self, callback) -> None:
        """Выполнить callback в главном потоке (можно вызывать из потоков)."""
        self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                task = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                task()
            except Exception:
                pass
        self.after(80, self._drain_ui_queue)

    def _log(self, message: str) -> None:
        text = str(message)
        self._post(lambda: self.status_lbl.configure(text=text))

    # ===================================================== список устройств

    def _refresh_devices(self, peers) -> None:
        seen = set()
        for index, peer in enumerate(peers):
            seen.add(peer.device_id)
            card = self._device_cards.get(peer.device_id)
            if card is None:
                card = W.DeviceCard(self.device_list, peer, on_select=self._select_device)
                self._device_cards[peer.device_id] = card
            else:
                card.set_name_and_ip(peer.name, peer.ip, peer.tcp_port)
            card.grid(row=index, column=0, sticky="ew", padx=4, pady=3)

        for device_id in list(self._device_cards):
            if device_id not in seen:
                self._device_cards.pop(device_id).destroy()
                if self._selected_device_id == device_id:
                    self._selected_device_id = None
                    self._update_target_label()

        self._devices = {peer.device_id: peer for peer in peers}
        self.dev_count_lbl.configure(text=str(len(peers)))
        if peers:
            self.dev_empty_lbl.grid_forget()
        else:
            self.dev_empty_lbl.grid(row=0, column=0, sticky="ew", pady=24)
        self._update_target_label()

    def _select_device(self, peer) -> None:
        self._selected_device_id = peer.device_id
        for device_id, card in self._device_cards.items():
            card.set_selected(device_id == peer.device_id)
        self._update_target_label()

    def _selected_peer(self):
        if not self._selected_device_id:
            return None
        peer = self._devices.get(self._selected_device_id)
        if peer is None:
            self._selected_device_id = None
            self._update_target_label()
        return peer

    def _update_target_label(self) -> None:
        peer = self._devices.get(self._selected_device_id) if self._selected_device_id else None
        if peer:
            self.target_lbl.configure(text=f"Получатель: {peer.name} ({peer.ip})",
                                      text_color=W.OK)
        else:
            self.target_lbl.configure(text="Получатель не выбран", text_color=W.WARN)

    # ============================================================ настройки

    def _apply_nickname(self) -> None:
        name = self.nick_entry.get().strip()
        if not name:
            messagebox.showwarning("Ник", "Ник не может быть пустым.", parent=self)
            return
        self._nickname = name
        self.config_store.nickname = name
        self.status_lbl.configure(text=self._startup_status())

    def _choose_save_dir(self) -> None:
        initial = self._save_dir if Path(self._save_dir).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(title="Выберите папку для сохранения файлов",
                                         initialdir=initial, parent=self)
        if chosen:
            self._save_dir = chosen
            self.config_store.save_dir = chosen
            self.save_dir_lbl.configure(text=chosen)

    def _toggle_auto_accept(self) -> None:
        self._auto_accept = bool(self.auto_accept_var.get())
        self.config_store.auto_accept = self._auto_accept

    def _clear_history(self) -> None:
        if not messagebox.askyesno("История", "Очистить историю передач?", parent=self):
            return
        self.config_store.clear_history()
        for row in self._rows.values():
            row.destroy()
        self._rows.clear()
        self.hist_empty_lbl.grid(row=0, column=0, sticky="ew", pady=20)

    # ============================================================= отправка

    def _send_files(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            messagebox.showwarning("Получатель", "Выберите устройство в списке слева.",
                                   parent=self)
            return
        paths = filedialog.askopenfilenames(title="Выберите файлы для отправки", parent=self)
        if not paths:
            return
        self._start_send(peer, [Path(p) for p in paths])

    def _send_folder(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            messagebox.showwarning("Получатель", "Выберите устройство в списке слева.",
                                   parent=self)
            return
        chosen = filedialog.askdirectory(title="Выберите папку для отправки", parent=self)
        if not chosen:
            return
        self._start_send(peer, [Path(chosen)])

    def _start_send(self, peer, paths) -> None:
        for path in paths:
            transfer_id = uuid.uuid4().hex[:12]
            is_folder = path.is_dir()
            try:
                size = folder_total_size(path) if is_folder else path.stat().st_size
            except OSError:
                size = 0
            self._add_transfer_row(
                transfer_id=transfer_id,
                direction="out",
                name=path.name,
                size=size,
                peer_text=f"{self._nickname} → {peer.name} ({peer.ip})",
                is_folder=is_folder,
            )
            self._register_history(transfer_id, "out", path.name, size,
                                   peer.name, peer.ip, is_folder, "sending", str(path))
            threading.Thread(
                target=self._send_worker,
                args=(peer, path, transfer_id),
                daemon=True,
                name=f"LANShare-Send-{transfer_id}",
            ).start()

    def _send_worker(self, peer, path: Path, transfer_id: str) -> None:
        client = TransferClient(self._nickname, self.config_store.device_id,
                                port=peer.tcp_port or TCP_PORT)
        result = client.send_path(
            peer.ip,
            path,
            transfer_id,
            on_progress=lambda transferred, total, speed, tid=transfer_id: self._post(
                lambda: self._update_row_progress(tid, transferred, total, speed)),
            on_stage=lambda stage, tid=transfer_id: self._post(
                lambda: self._apply_stage(tid, stage)),
        )
        self._post(lambda: self._finish_outgoing(transfer_id, path, result))

    def _apply_stage(self, transfer_id: str, stage: str) -> None:
        row = self._rows.get(transfer_id)
        if row is None or row.finished:
            return
        if stage == "packing":
            row.set_packing(True)
            row.set_status("Упаковка папки в .zip…", W.WARN)
        elif stage == "connecting":
            row.set_packing(False)
            row.set_status("Подключение к получателю…", W.WARN)
        elif stage == "sending":
            row.set_packing(False)
            row.set_status("Передача…", W.WARN)

    def _finish_outgoing(self, transfer_id: str, path: Path, result: dict) -> None:
        row = self._rows.get(transfer_id)
        status = result.get("status", "failed")
        error = str(result.get("error") or "")
        if row is not None:
            row.set_finished(status, str(path), error)
        stored_status = {"completed": "completed",
                         "rejected": "rejected"}.get(status, "failed")
        self.config_store.update_history(transfer_id, status=stored_status,
                                         path=str(path))
        if stored_status == "completed":
            self._log(f"Отправлено: {path.name} → {result.get('peer_name') or 'устройство'}")
        elif stored_status == "rejected":
            self._log(f"Отклонено получателем: {path.name} ({error})")
        else:
            self._log(f"Ошибка отправки {path.name}: {error}")

    # ============================================================== приём

    def _handle_offer(self, offer: dict, peer_ip: str) -> dict:
        """Вызывается из сетевого потока при входящем файле/папке (блокирующе)."""
        transfer_id = offer["id"]
        self._post(lambda: self._add_transfer_row(
            transfer_id=transfer_id,
            direction="in",
            name=offer["name"],
            size=offer["size"],
            peer_text=f"{offer['sender_name']} ({peer_ip})",
            is_folder=offer["is_folder"],
        ))
        self._post(lambda: self._register_history(
            transfer_id, "in", offer["name"], offer["size"], offer["sender_name"],
            peer_ip, offer["is_folder"], "waiting", ""))

        # Автоприём: без диалога, дубликаты сохраняются как копия.
        if self._auto_accept:
            mode = "copy" if self._incoming_target_exists(offer) else "new"
            self._post(lambda: self._set_row_status(
                transfer_id, "Принято автоматически", W.WARN))
            return {"status": "ok", "mode": mode}

        holder: dict = {}
        done = threading.Event()

        def show_dialog() -> None:
            try:
                holder["decision"] = self._prompt_incoming(offer, peer_ip)
            finally:
                done.set()

        self._post(show_dialog)
        if not done.wait(timeout=180):
            self._post(lambda: self._set_row_status(transfer_id, "Таймаут ожидания", W.ERR))
            return {"status": "reject", "reason": "получатель не ответил"}
        return holder.get("decision") or {"status": "reject", "reason": "отменено"}

    def _incoming_target_exists(self, offer: dict) -> bool:
        name = sanitize_filename(offer["name"])
        try:
            return (Path(self._save_dir) / name).exists()
        except OSError:
            return False

    def _prompt_incoming(self, offer: dict, peer_ip: str) -> dict:
        """Диалог подтверждения приёма + защита от перезаписи (в потоке UI)."""
        transfer_id = offer["id"]
        dialog = IncomingDialog(self, offer["sender_name"], peer_ip, offer["name"],
                                offer["size"], offer["is_folder"])
        accepted = dialog.show()
        if not accepted:
            self._set_row_status(transfer_id, "Отклонено", W.ERR)
            self.config_store.update_history(transfer_id, status="rejected")
            return {"status": "reject", "reason": "пользователь отклонил передачу"}

        mode = "new"
        if self._incoming_target_exists(offer):
            dup = DuplicateDialog(self, offer["name"], offer["is_folder"])
            mode = dup.show()
            if mode == "cancel":
                self._set_row_status(transfer_id, "Отменено", W.ERR)
                self.config_store.update_history(transfer_id, status="rejected")
                return {"status": "reject", "reason": "пользователь отменил приём"}
        self._set_row_status(transfer_id, "Принято, ожидание данных…", W.WARN)
        return {"status": "ok", "mode": mode}

    def _on_incoming_progress(self, transfer_id: str, transferred: int, total: int,
                              speed: float) -> None:
        self._post(lambda: self._update_row_progress(transfer_id, transferred, total,
                                                     speed))

    def _on_incoming_finished(self, transfer_id: str, status: str, path: str,
                              error: str) -> None:
        self._post(lambda: self._finish_incoming(transfer_id, status, path, error))

    def _finish_incoming(self, transfer_id: str, status: str, path: str,
                         error: str) -> None:
        row = self._rows.get(transfer_id)
        if row is not None:
            row.set_finished(status, path, error)
        stored = status if status in ("completed", "rejected") else "failed"
        self.config_store.update_history(transfer_id, status=stored, path=path)
        if stored == "completed":
            self._log(f"Получено: {path}")
        elif stored == "rejected":
            self._log(f"Входящая передача отклонена ({error})")
        else:
            self._log(f"Ошибка приёма: {error}")

    # ======================================================= история/строки

    def _add_transfer_row(self, transfer_id: str, direction: str, name: str, size: int,
                          peer_text: str, is_folder: bool) -> None:
        if transfer_id in self._rows:
            return
        row = W.TransferRow(self.history_list, transfer_id, direction, name, size,
                            peer_text, is_folder)
        row.grid(row=len(self._rows), column=0, sticky="ew", padx=4, pady=3)
        self._rows[transfer_id] = row
        if self._rows:
            self.hist_empty_lbl.grid_forget()

    def _update_row_progress(self, transfer_id: str, transferred: int, total: int,
                             speed: float) -> None:
        row = self._rows.get(transfer_id)
        if row is not None and not row.finished:
            row.update_progress(transferred, total, speed)

    def _set_row_status(self, transfer_id: str, text: str, color: str = W.TEXT_DIM) -> None:
        row = self._rows.get(transfer_id)
        if row is not None and not row.finished:
            row.set_status(text, color)

    def _register_history(self, transfer_id: str, direction: str, name: str, size: int,
                          peer_name: str, peer_ip: str, is_folder: bool,
                          status: str, path: str) -> None:
        self.config_store.add_history({
            "id": transfer_id,
            "direction": direction,
            "name": name,
            "size": size,
            "is_folder": is_folder,
            "peer_name": peer_name,
            "peer_ip": peer_ip,
            "status": status,
            "path": path,
        })


# ================================================================= диалоги

class _BaseDialog(ctk.CTkToplevel):
    """Общая часть модальных диалогов: центрирование и ожидание закрытия."""

    def __init__(self, master, title: str, width: int, height: int) -> None:
        super().__init__(master, fg_color=W.BG)
        self.result = None
        self.title(title)
        self.resizable(False, False)
        self.geometry(f"{width}x{height}")
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda _e: self._on_cancel())
        self._parent = master

    def _on_cancel(self) -> None:
        self.result = self.result if self.result is not None else False
        self.destroy()

    def show(self):
        """Показывает диалог модально и возвращает результат."""
        self.update_idletasks()
        try:
            px, py = self._parent.winfo_rootx(), self._parent.winfo_rooty()
            pw, ph = self._parent.winfo_width(), self._parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            x = px + max((pw - w) // 2, 0)
            y = py + max((ph - h) // 3, 0)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass
        self.grab_set()
        self.focus_force()
        self.wait_window()
        return self.result


class IncomingDialog(_BaseDialog):
    """Окно подтверждения приёма: «Пользователь X хочет отправить …»."""

    def __init__(self, master, sender_name: str, sender_ip: str, item_name: str,
                 item_size: int, is_folder: bool) -> None:
        super().__init__(master, "Входящая передача", 460, 250)
        self.result = False

        ctk.CTkLabel(self, text="Входящая передача", font=W.font(16, bold=True),
                     text_color=W.TEXT).pack(pady=(20, 10))

        what = "папку" if is_folder else "файл"
        text = (f"Пользователь {sender_name} ({sender_ip})\n"
                f"хочет отправить {what}:\n\n"
                f"{item_name}\n"
                f"Размер: {W.human_size(item_size)}")
        ctk.CTkLabel(self, text=text, font=W.font(12), text_color=W.TEXT,
                     justify="center", wraplength=410).pack(pady=(0, 18), padx=20)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(pady=(0, 18))
        ctk.CTkButton(buttons, text="Принять", width=140, height=34, corner_radius=4,
                      font=W.font(12, bold=True), fg_color=W.ACCENT,
                      hover_color=W.ACCENT_HOVER,
                      command=self._accept).grid(row=0, column=0, padx=6)
        ctk.CTkButton(buttons, text="Отклонить", width=140, height=34, corner_radius=4,
                      font=W.font(12), fg_color=W.CARD, hover_color=W.CARD_HOVER,
                      text_color=W.TEXT, border_width=1, border_color=W.BORDER,
                      command=self._reject).grid(row=0, column=1, padx=6)

    def _accept(self) -> None:
        self.result = True
        self.destroy()

    def _reject(self) -> None:
        self.result = False
        self.destroy()


class DuplicateDialog(_BaseDialog):
    """Диалог защиты от перезаписи уже существующего файла/папки."""

    def __init__(self, master, item_name: str, is_folder: bool) -> None:
        super().__init__(master, "Файл уже существует", 520, 230)
        self.result = "cancel"

        what = "Папка" if is_folder else "Файл"
        ctk.CTkLabel(self, text=f"{what} уже существует", font=W.font(16, bold=True),
                     text_color=W.WARN).pack(pady=(20, 8))
        ctk.CTkLabel(self, text=f"{what} «{item_name}» уже существует.\n"
                                f"Вы точно хотите скачать его ещё раз?",
                     font=W.font(12), text_color=W.TEXT, justify="center",
                     wraplength=460).pack(pady=(0, 18), padx=20)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(pady=(0, 18))
        ctk.CTkButton(buttons, text="Перезаписать", width=150, height=34, corner_radius=4,
                      font=W.font(12, bold=True), fg_color=W.ERR, hover_color="#c9433f",
                      command=lambda: self._choose("overwrite")).grid(row=0, column=0,
                                                                      padx=6)
        ctk.CTkButton(buttons, text="Сохранить как копию", width=170, height=34,
                      corner_radius=4, font=W.font(12), fg_color=W.ACCENT,
                      hover_color=W.ACCENT_HOVER,
                      command=lambda: self._choose("copy")).grid(row=0, column=1, padx=6)
        ctk.CTkButton(buttons, text="Отмена", width=110, height=34, corner_radius=4,
                      font=W.font(12), fg_color=W.CARD, hover_color=W.CARD_HOVER,
                      text_color=W.TEXT, border_width=1, border_color=W.BORDER,
                      command=lambda: self._choose("cancel")).grid(row=0, column=2, padx=6)

    def _choose(self, mode: str) -> None:
        self.result = mode
        self.destroy()
