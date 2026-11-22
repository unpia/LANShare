"""Упаковка папок в .zip (без сжатия) и распаковка на стороне получателя.

Используется ZIP_STORED: архивация мгновенная и не нагружает CPU,
скорость передачи не падает, а получатель распаковывает папку автоматически.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

# Колбэк прогресса упаковки: (обработано_байт, всего_байт) -> None


def _safe_arcname(relative: str) -> str:
    """Приводит путь внутри архива к виду с разделителем '/' (без корневой папки).

    Имя самой папки в архив не попадает: получатель распаковывает архив прямо
    в целевую папку (в том числе в переименованную копию вида «имя (1)»).
    """
    return relative.replace("\\", "/").lstrip("/") or "."


def folder_total_size(folder: Path) -> int:
    """Суммарный размер всех файлов папки (для прогресса упаковки)."""
    total = 0
    for root, _dirs, files in os.walk(folder):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                continue
    return total


def zip_folder(folder: str | os.PathLike, progress_cb=None) -> str:
    """Упаковывает папку во временный .zip без сжатия и возвращает путь к архиву.

    :param folder: путь к папке.
    :param progress_cb: необязательный колбэк (processed, total).
    :return: путь к созданному временному .zip
    """
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise NotADirectoryError(f"Не папка: {folder_path}")

    total = max(folder_total_size(folder_path), 1)
    processed = 0
    last_reported = 0

    fd, tmp_zip = tempfile.mkstemp(prefix="lanshare_", suffix=".zip")
    os.close(fd)

    try:
        with zipfile.ZipFile(tmp_zip, mode="w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            for root, dirs, files in os.walk(folder_path):
                rel_root = os.path.relpath(root, folder_path)
                if rel_root == ".":
                    rel_root = ""
                if rel_root and not files and not dirs:
                    # Сохраняем пустые папки, чтобы структура не терялась.
                    zf.writestr(_safe_arcname(rel_root) + "/", b"")
                for fname in files:
                    full = os.path.join(root, fname)
                    rel = os.path.join(rel_root, fname) if rel_root else fname
                    zf.write(full, _safe_arcname(rel))
                    try:
                        processed += os.path.getsize(full)
                    except OSError:
                        pass
                    if progress_cb and processed - last_reported >= 4 * 1024 * 1024:
                        last_reported = processed
                        try:
                            progress_cb(min(processed, total), total)
                        except Exception:
                            pass
    except Exception:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass
        raise

    if progress_cb:
        try:
            progress_cb(total, total)
        except Exception:
            pass
    return tmp_zip


def unzip_archive(zip_path: str | os.PathLike, dest_dir: str | os.PathLike) -> str:
    """Распаковывает архив в dest_dir и возвращает путь к целевой папке.

    Защищена от path traversal: записи с выходом за пределы папки игнорируются.
    """
    zip_path = Path(zip_path)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, mode="r") as zf:
        try:
            # filter="data" доступен с Python 3.11.4+, безопасен к traversal.
            zf.extractall(dest, filter="data")
        except TypeError:
            _extractall_safe(zf, dest)
    return str(dest)


def _extractall_safe(zf: zipfile.ZipFile, dest: Path) -> None:
    """Ручная распаковка с проверкой, что путь не выходит за пределы dest."""
    dest_root = dest.resolve()
    for member in zf.infolist():
        target = (dest_root / member.filename).resolve()
        if not str(target).startswith(str(dest_root)):
            continue
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)


def remove_path(path: str | os.PathLike) -> None:
    """Удаляет файл или папку (рекурсивно), не падая на отсутствующих путях."""
    p = Path(path)
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    except Exception:
        pass


def unique_path(path: Path, is_dir: bool = False) -> Path:
    """Подбирает свободное имя вида 'имя (1).ext' (для режима «Сохранить как копию»)."""
    if not path.exists():
        return path
    if is_dir:
        base, suffix = path.name, ""
    else:
        base, suffix = path.stem, path.suffix
    for index in range(1, 100000):
        candidate = path.parent / f"{base} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Не удалось подобрать свободное имя для {path}")
