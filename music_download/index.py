"""Индекс MP3 в SQLite, существующий только во время текущей команды."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from mutagen import MutagenError

from music_download.paths import ensure_inside
from music_download.tags import read_track_source

logger = logging.getLogger(__name__)


def _file_metadata(info: os.stat_result) -> tuple[int, int, int, int]:
    """Собрать размер, время изменения, устройство и inode.

    :param info: Метаданные, полученные через stat() или lstat().
    """
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _is_nonempty_regular_file(info: os.stat_result) -> bool:
    """Проверить тип и размер файла.

    :param info: Метаданные объекта файловой системы.
    """
    return stat.S_ISREG(info.st_mode) and info.st_size != 0


@dataclass(frozen=True)
class IndexedFile:
    path: Path
    track_id: str
    album_id: str | None
    size: int
    modified_ns: int
    device: int
    inode: int

    def is_current(self) -> bool:
        """Проверить, что файл не удалён, не заменён и не изменён после индексирования."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISREG(info.st_mode) and (
            _file_metadata(info) == (self.size, self.modified_ns, self.device, self.inode)
        )


class LibraryIndex:
    def __init__(self, directory: Path):
        """Открыть пустую базу в памяти; сканирование запускается явно через scan().

        :param directory: Корневой каталог музыкальной библиотеки.
        """
        self.directory = Path(directory).resolve()
        self._connection = sqlite3.connect(":memory:")
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA temp_store = MEMORY")
        self._connection.executescript("""
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                track_id TEXT NOT NULL,
                album_id TEXT,
                size INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL
            );
            CREATE INDEX files_identity ON files(track_id, album_id);
        """)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """Закрыть базу при выходе из блока with.

        :param exc_type: Тип исключения или None при обычном выходе.
        :param exc_value: Экземпляр исключения или None.
        :param traceback: Трассировка исключения или None.
        """
        self.close()

    def close(self) -> None:
        self._connection.close()

    def scan(self) -> int:
        """Прочитать теги MP3 один раз; ссылки, пустые и повреждённые файлы пропустить.

        Ошибка обхода каталога прерывает сканирование: неполный индекс не используется.
        Отсутствующий корень считается пустой библиотекой и не создаётся.
        """
        if not self.directory.exists():
            return 0
        if not self.directory.is_dir():
            raise NotADirectoryError(self.directory)

        logger.info("Индексирование библиотеки: %s", self.directory)
        count = 0
        with self._connection:
            self._connection.execute("DELETE FROM files")  # Очищаем БД
            for folder, directories, names in os.walk(self.directory, onerror=self._scan_error):
                directories[:] = sorted(
                    name for name in directories if not (Path(folder) / name).is_symlink()
                )
                for name in sorted(names):
                    path = Path(folder) / name
                    if path.suffix.lower() != ".mp3" or path.is_symlink():
                        continue
                    try:
                        before = path.stat()

                        # Читаем MP3-теги только у непустых обычных файлов.
                        if not _is_nonempty_regular_file(before):
                            continue
                        source = read_track_source(path)
                        if source is None:
                            continue
                        after = path.stat()
                        if _file_metadata(after) != _file_metadata(before):
                            logger.warning("Файл изменился при индексировании: %s", path)
                            continue
                        self._record(path, source.track_id, source.album_id)
                    except (OSError, MutagenError, ValueError) as error:
                        logger.warning(
                            "Не удалось индексировать %s: %s", path, type(error).__name__
                        )
                        continue
                    count += 1
        logger.info("Индекс в памяти построен: файлов=%d", count)
        return count

    @staticmethod
    def _scan_error(error: OSError) -> None:
        """Прервать сканирование при ошибке обхода каталога.

        :param error: Ошибка, переданная os.walk().
        """
        raise error

    def _relative_path(self, path: Path) -> str:
        """Получить путь относительно корня библиотеки.

        :param path: Путь к файлу внутри библиотеки.
        """
        ensure_inside(self.directory, path)
        return str(path.resolve().relative_to(self.directory))

    def _record(self, path: Path, track_id: str, album_id: str | None) -> None:
        """Записать файл в рамках транзакции вызывающего метода.

        :param path: Путь к файлу внутри библиотеки.
        :param track_id: Идентификатор трека.
        :param album_id: Идентификатор альбома или None, если он неизвестен.
        """
        path = path.absolute()
        relative = self._relative_path(path)
        info = path.lstat()
        if not _is_nonempty_regular_file(info):
            raise ValueError("В индекс можно добавить только непустой обычный файл")
        self._connection.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
            (relative, track_id, album_id, *_file_metadata(info)),
        )

    def record(self, path: Path, track_id: str, album_id: str | None) -> None:
        """Учесть успешную загрузку или восстановленный после отката файл.

        :param path: Путь к сохранённому файлу.
        :param track_id: Идентификатор трека.
        :param album_id: Идентификатор альбома или None, если он неизвестен.
        """
        with self._connection:
            self._record(path, track_id, album_id)

    def find(self, track_id: str, album_id: str | None) -> IndexedFile | None:
        """Найти неизменённый файл того же трека и издания; остальные копии не трогать.

        :param track_id: Идентификатор искомого трека.
        :param album_id: Идентификатор искомого альбома. None ищет записи без ID альбома.
        """
        rows = self._connection.execute(
            "SELECT * FROM files WHERE track_id = ? AND album_id IS ? ORDER BY path",
            (track_id, album_id),
        ).fetchall()
        for row in rows:
            item = IndexedFile(
                self.directory / row["path"],
                row["track_id"],
                row["album_id"],
                row["size"],
                row["modified_ns"],
                row["device"],
                row["inode"],
            )
            ensure_inside(self.directory, item.path)
            if item.is_current():
                return item
            logger.warning("Запись индекса устарела: %s", item.path)
            with self._connection:
                self._delete_record(row["path"])
        return None

    def _delete_record(self, relative_path: str) -> None:
        """Удалить строку из индекса.

        :param relative_path: Путь относительно корня библиотеки, как он хранится в базе.
        """
        self._connection.execute("DELETE FROM files WHERE path = ?", (relative_path,))

    def _replace_path(
        self, source: Path, target: Path, track_id: str, album_id: str | None
    ) -> None:
        """Заменить запись о пути в одной транзакции.

        :param source: Путь, запись о котором нужно удалить.
        :param target: Путь к файлу, который нужно записать в индекс.
        :param track_id: Идентификатор трека.
        :param album_id: Идентификатор альбома или None, если он неизвестен.
        """
        with self._connection:
            self._delete_record(self._relative_path(source))
            self._record(target, track_id, album_id)

    def move(self, item: IndexedFile, target: Path) -> None:
        """Обновить путь после переноса одной транзакцией.

        :param item: Запись о файле до переноса.
        :param target: Новый путь к перенесённому файлу.
        """
        self._replace_path(item.path, target, item.track_id, item.album_id)

    def restore(self, item: IndexedFile, target: Path) -> None:
        """Вернуть исходную запись после отката, удалив возможную новую запись.

        :param item: Исходная запись о восстановленном файле.
        :param target: Путь назначения отменённого переноса.
        """
        self._replace_path(target, item.path, item.track_id, item.album_id)
