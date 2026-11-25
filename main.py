"""LANShare — локальный файлообменник для Windows.

Точка входа приложения: настраивает тему customtkinter и запускает главное окно.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _log_fatal(error_text: str) -> None:
    """Пишет аварийный стек в ~/.lanshare/error.log (важно для сборки --noconsole)."""
    try:
        log_dir = Path.home() / ".lanshare"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "error.log", "a", encoding="utf-8") as handle:
            handle.write(error_text + "\n" + "-" * 70 + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        import customtkinter as ctk

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        from ui.main_window import MainWindow

        app = MainWindow()
        app.mainloop()
        return 0
    except Exception:  # noqa: BLE001 — показываем ошибку, а не молча падаем
        traceback_text = traceback.format_exc()
        _log_fatal(traceback_text)
        try:
            import tkinter.messagebox as messagebox

            messagebox.showerror(
                "LANShare — ошибка запуска",
                "Приложение не смогло запуститься:\n\n"
                + traceback_text.strip().splitlines()[-1]
                + "\n\nПодробности: ~/.lanshare/error.log",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
