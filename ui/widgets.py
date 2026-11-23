"""Виджеты интерфейса: карточка устройства и строка передачи файла."""

from __future__ import annotations

import os
from pathlib import Path

import customtkinter as ctk

# ------------------------------------------------------------------ палитра

BG = "#1e1e1e"            # фон окна
PANEL = "#2b2b2b"         # панели
CARD = "#252525"          # карточка устройства / строки передачи
CARD_HOVER = "#303030"
ROW_ALT = "#292929"
SELECTED = "#2f4f6f"      # выбранное устройство
BORDER = "#3a3a3a"
BORDER_HOVER = "#4d4d4d"
TEXT = "#e6e6e6"
TEXT_DIM = "#9a9a9a"
ACCENT = "#3a6ea5"        # спокойный синий (в духе qBittorrent)
ACCENT_HOVER = "#4a7fb5"
OK = "#4caf50"            # успех
ERR = "#d9534f"           # ошибка
WARN = "#d9a441"          # предупреждение / в процессе

FONT_FAMILY = "Segoe UI"


def font(size: int = 12, bold: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight="bold" if bold else "normal")


def human_size(num_bytes: int) -> str:
    """Человекочитаемый размер: 12.4 МБ, 1.02 ГБ и т.п."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def human_speed(mb_per_second: float) -> str:
    """Скорость в МБ/с (или КБ/с для медленных передач)."""
    try:
        speed = float(mb_per_second)
    except (TypeError, ValueError):
        return ""
    if speed >= 1:
        return f"{speed:.1f} МБ/с"
    if speed > 0:
        return f"{speed * 1024:.0f} КБ/с"
    return ""


# ------------------------------------------------------------- устройство

class DeviceCard(ctk.CTkFrame):
    """Карточка найденного устройства в сети (Имя + IP)."""

    def __init__(self, master, peer, on_select=None, **kwargs):
        super().__init__(master, fg_color=CARD, corner_radius=4,
                         border_width=1, border_color=BORDER, **kwargs)
        self.peer = peer
        self.on_select = on_select
        self._selected = False

        self.grid_columnconfigure(1, weight=1)

        self.dot = ctk.CTkLabel(self, text="●", width=14, text_color=OK, font=font(11))
        self.dot.grid(row=0, column=0, rowspan=2, padx=(8, 4), pady=6, sticky="w")

        self.name_lbl = ctk.CTkLabel(self, text=peer.name, anchor="w",
                                     text_color=TEXT, font=font(12, bold=True))
        self.name_lbl.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(6, 0))

        self.ip_lbl = ctk.CTkLabel(self, text=f"{peer.ip}:{peer.tcp_port}", anchor="w",
                                   text_color=TEXT_DIM, font=font(10))
        self.ip_lbl.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(0, 6))

        for widget in (self, self.dot, self.name_lbl, self.ip_lbl):
            widget.bind("<Button-1>", self._clicked)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    # ------------------------------------------------------------------ API

    def set_name_and_ip(self, name: str, ip: str, port: int) -> None:
        self.name_lbl.configure(text=name)
        self.ip_lbl.configure(text=f"{ip}:{port}")

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.configure(fg_color=SELECTED if selected else CARD,
                       border_color=ACCENT if selected else BORDER)

    @property
    def selected(self) -> bool:
        return self._selected

    # ------------------------------------------------------------- события

    def _clicked(self, _event=None) -> None:
        if self.on_select:
            self.on_select(self.peer)

    def _enter(self, _event=None) -> None:
        if not self._selected:
            self.configure(fg_color=CARD_HOVER, border_color=BORDER_HOVER)

    def _leave(self, _event=None) -> None:
        if not self._selected:
            self.configure(fg_color=CARD, border_color=BORDER)


# -------------------------------------------------------------- передача

class TransferRow(ctk.CTkFrame):
    """Строка истории передач: прогресс-бар, скорость, статус, «Открыть папку»."""

    def __init__(self, master, transfer_id: str, direction: str, name: str,
                 size: int, peer_text: str, is_folder: bool = False,
                 on_open=None, **kwargs):
        super().__init__(master, fg_color=CARD, corner_radius=4,
                         border_width=1, border_color=BORDER, **kwargs)
        self.transfer_id = transfer_id
        self.direction = direction          # "in" (получение) / "out" (отправка)
        self.name = name
        self.size = size
        self.is_folder = is_folder
        self.on_open = on_open
        self.open_path = ""
        self.finished = False
        self._packing = False

        self.grid_columnconfigure(1, weight=1)

        arrow = "↓" if direction == "in" else "↑"
        arrow_color = OK if direction == "in" else ACCENT
        self.arrow = ctk.CTkLabel(self, text=arrow, width=16, text_color=arrow_color,
                                  font=font(16, bold=True))
        self.arrow.grid(row=0, column=0, rowspan=2, padx=(8, 6), pady=8, sticky="w")

        display_name = f"{name}{' (папка)' if is_folder else ''}"
        self.name_lbl = ctk.CTkLabel(self, text=display_name, anchor="w",
                                     text_color=TEXT, font=font(12, bold=True))
        self.name_lbl.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(8, 0))

        self.size_lbl = ctk.CTkLabel(self, text=human_size(size), anchor="e",
                                     text_color=TEXT_DIM, font=font(10))
        self.size_lbl.grid(row=0, column=2, sticky="e", padx=(0, 10), pady=(8, 0))

        self.peer_lbl = ctk.CTkLabel(self, text=peer_text, anchor="w",
                                     text_color=TEXT_DIM, font=font(10))
        self.peer_lbl.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 10),
                           pady=(0, 2))

        self.progress = ctk.CTkProgressBar(self, height=8, corner_radius=3,
                                           progress_color=ACCENT, fg_color="#1a1a1a")
        self.progress.set(0)
        self.progress.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(8, 8),
                           pady=(0, 4))

        self.speed_lbl = ctk.CTkLabel(self, text="", anchor="e", width=78,
                                      text_color=TEXT_DIM, font=font(10))
        self.speed_lbl.grid(row=2, column=2, sticky="e", padx=(0, 10), pady=(0, 4))

        self.status_lbl = ctk.CTkLabel(self, text="Ожидание…", anchor="w",
                                       text_color=WARN, font=font(10))
        self.status_lbl.grid(row=3, column=0, columnspan=2, sticky="w", padx=(8, 0),
                             pady=(0, 8))

        self.open_btn = ctk.CTkButton(self, text="Открыть папку", width=112, height=26,
                                      corner_radius=4, font=font(11),
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                      command=self._open_folder, state="disabled")
        self.open_btn.grid(row=3, column=2, sticky="e", padx=(0, 10), pady=(0, 8))

    # ------------------------------------------------------------------ API

    def set_status(self, text: str, color: str = TEXT_DIM, progress=None) -> None:
        self.status_lbl.configure(text=text, text_color=color)
        if progress is not None:
            self.progress.set(max(0.0, min(1.0, progress)))

    def update_progress(self, transferred: int, total: int, speed: float) -> None:
        """Обновление прогресса: байты, проценты и скорость в МБ/с."""
        if total > 0:
            ratio = min(max(transferred / total, 0.0), 1.0)
            self.progress.set(ratio)
            percent = f"{ratio * 100:.0f}%"
        else:
            percent = ""
        speed_text = human_speed(speed)
        self.speed_lbl.configure(text=speed_text)
        prefix = "Упаковка" if self._packing else "Передача"
        parts = [prefix, percent, f"({human_size(transferred)} / {human_size(total)})"]
        self.set_status(" · ".join(p for p in parts if p), WARN)

    def set_packing(self, packing: bool) -> None:
        """Помечает, что идёт упаковка папки в .zip (или передача данных)."""
        self._packing = packing
        if packing:
            self.progress.configure(progress_color=WARN)
        else:
            self.progress.configure(progress_color=ACCENT)

    def set_finished(self, status: str, path: str = "", error: str = "") -> None:
        """Финализирует строку: 'completed' | 'rejected' | 'failed'."""
        self.finished = True
        if status == "completed":
            self.progress.set(1.0)
            self.progress.configure(progress_color=OK)
            suffix = "Получено в" if self.direction == "in" else "Отправлено в"
            self.speed_lbl.configure(text="")
            self.set_status(f"Готово · {suffix} {path}" if path else "Готово", OK)
            if path:
                self.open_path = path
                self.open_btn.configure(state="normal")
        elif status == "rejected":
            self.progress.configure(progress_color=ERR)
            self.set_status(error or "Отклонено", ERR)
        else:
            self.progress.configure(progress_color=ERR)
            self.set_status(error or "Ошибка передачи", ERR)

    def mark_local_error(self, error: str) -> None:
        self.progress.configure(progress_color=ERR)
        self.set_status(error, ERR)

    # ------------------------------------------------------------- действие

    def _open_folder(self) -> None:
        target = self.open_path
        if not target:
            return
        path = Path(target)
        folder = path if (self.is_folder or path.is_dir()) else path.parent
        try:
            os.startfile(str(folder))  # noqa: S606 — Проводник Windows
        except Exception:
            try:
                os.startfile(str(Path.home()))
            except Exception:
                pass
