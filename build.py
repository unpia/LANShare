"""Сборка LANShare в один .exe файл через PyInstaller.

Запуск:  python build.py

Что делает скрипт:
  * сам находит путь к установленному пакету customtkinter;
  * добавляет его в сборку через --add-data (иначе .exe падает без темы и шрифтов,
    так как customtkinter читает свои .json-темы и assets из папки пакета);
  * собирает один исполняемый файл с флагом --noconsole (запуск двойным кликом
    без окна консоли и без установленного Python).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
ENTRY_POINT = PROJECT_DIR / "main.py"
APP_NAME = "LANShare"


def customtkinter_data_arg() -> str:
    """Возвращает аргумент --add-data для пакета customtkinter."""
    try:
        import customtkinter
    except ImportError:
        sys.exit("Не найден customtkinter. Установите зависимости: "
                 "pip install -r requirements.txt")

    package_dir = Path(customtkinter.__file__).resolve().parent
    separator = ";" if os.name == "nt" else ":"  # в Windows разделитель — ';'
    return f"{package_dir}{separator}customtkinter"


def build() -> int:
    if not ENTRY_POINT.is_file():
        sys.exit(f"Не найден файл точки входа: {ENTRY_POINT}")

    for folder in ("build", "dist"):
        shutil.rmtree(PROJECT_DIR / folder, ignore_errors=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--noconsole",
        "--onefile",
        "--name", APP_NAME,
        "--add-data", customtkinter_data_arg(),
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "darkdetect",
        str(ENTRY_POINT),
    ]

    print("Сборка:", " ".join(command), flush=True)
    result = subprocess.call(command, cwd=str(PROJECT_DIR))
    if result != 0:
        print("Сборка завершилась с ошибкой.", file=sys.stderr)
        return result

    exe_path = PROJECT_DIR / "dist" / f"{APP_NAME}.exe"
    if exe_path.is_file():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"\nГотово: {exe_path} ({size_mb:.1f} МБ)")
        print("При первом запуске разрешите доступ в Брандмауэре Windows "
              "(частные и публичные сети).")
    else:
        print("Сборка прошла, но .exe не найден в папке dist.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(build())
