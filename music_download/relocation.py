"""Перенос запрошенного файла без перезаписи и откат в текущем процессе."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

from music_download.index import IndexedFile, LibraryIndex
from music_download.paths import ensure_inside

logger = logging.getLogger(__name__)


class RelocationError(OSError):
    """Перенос не удалось завершить или полностью откатить."""


def _same_file(path: Path, item: IndexedFile) -> bool:
    """Сравнить устройство и inode объекта с данными из индекса.

    :param path: Путь к проверяемому объекту.
    :param item: Запись с ожидаемыми устройством и inode.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (info.st_dev, info.st_ino) == (item.device, item.inode)


def _rollback(item: IndexedFile, target: Path, index: LibraryIndex) -> None:
    """Вернуть исходное имя, не заменяя появившийся на его месте чужой файл.

    :param item: Запись с исходным путём и идентификатором файла.
    :param target: Путь назначения отменяемого переноса.
    :param index: Индекс, в котором восстанавливается исходная запись.
    """
    # Восстанавливать файл можно только из ссылки, созданной при переносе.
    if not _same_file(target, item):
        raise RelocationError("Файл назначения изменён, откат остановлен")
    # Перед изменением файлов проверяем, что оба пути ведут внутрь библиотеки.
    ensure_inside(index.directory, item.path)
    ensure_inside(index.directory, target)
    # Если исходное имя уже удалили при переносе, создаём его заново через os.link().
    if not item.path.exists():
        os.link(target, item.path, follow_symlinks=False)
    # Удаляем имя назначения только после проверки, что исходное имя ведёт к тому же файлу.
    if not _same_file(item.path, item):
        raise RelocationError("Исходный путь занят другим файлом, откат остановлен")
    target.unlink()
    # Возвращаем в индекс исходный путь и убираем возможную запись о новом пути.
    index.restore(item, target)


def relocate_file(
    item: IndexedFile,
    target: Path,
    index: LibraryIndex,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    """Перенести файл в пределах одной ФС и обновить индекс.

    link + unlink не перезаписывает занятый путь даже при гонке. Копирования
    аудио нет; между действиями существуют два имени одного файла. При аварийном
    завершении оба имени могут остаться; восстановления при следующем запуске нет.

    :param item: Запись о файле, который нужно перенести.
    :param target: Путь назначения.
    :param index: Индекс, в котором обновляется путь файла.
    :param check_cancelled: Необязательная функция без аргументов, которая
        выбрасывает исключение при отмене задания.
    """
    target = target.absolute()
    ensure_inside(index.directory, item.path)
    ensure_inside(index.directory, target)
    if not item.is_current():
        raise RelocationError("Исходный файл изменился перед переносом")
    if check_cancelled:
        check_cancelled()
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_inside(index.directory, target)
    if item.path.stat().st_dev != target.parent.stat().st_dev:
        raise RelocationError("Перенос между файловыми системами не поддерживается")
    linked = False
    try:
        logger.info("Перенос: %s → %s", item.path, target)
        os.link(item.path, target, follow_symlinks=False)
        linked = True
        if not item.is_current() or not _same_file(target, item):
            raise RelocationError("Исходный файл изменился во время переноса")
        if check_cancelled:
            check_cancelled()
        item.path.unlink()
        if check_cancelled:
            check_cancelled()
        index.move(item, target)
    except BaseException:
        if linked:
            try:
                _rollback(item, target, index)
                logger.warning("Перенос отменён, файл возвращён: %s", item.path)
            except (OSError, ValueError, sqlite3.Error) as error:
                logger.error(
                    "Откат не завершён: исходный=%s, целевой=%s, ошибка=%s. Проверьте файлы вручную",
                    item.path,
                    target,
                    type(error).__name__,
                )
                raise RelocationError(
                    "Не удалось откатить перенос; проверьте пути из лога"
                ) from error
        raise
