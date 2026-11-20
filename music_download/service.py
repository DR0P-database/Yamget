from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from yandex_music import Track

from music_download.engine import Aria2Engine, DownloadProgress
from music_download.formats import get_download_format, select_download_info
from music_download.models import DownloadContext, DownloadOptions, DownloadResult, TrackMetadata
from music_download.tags import read_track_id, write_tags

if TYPE_CHECKING:
    from music_download.providers.yandex import YandexProvider


logger = logging.getLogger(__name__)
_DEFAULT_OPTIONS = DownloadOptions()


def safe_name(value: str) -> str:
    """Подготовить компонент имени файла.

    :param value: Исходное название.

    :returns: Название без запрещённых символов и крайних точек/пробелов;
        ``track``, если после очистки строка пуста.
    """
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "track"


class DownloadService:
    def __init__(self, provider: YandexProvider, engine: Aria2Engine | None = None):
        """Создать сервис скачивания.

        :param provider: API-провайдер музыкальных данных.
        :param engine: Движок загрузки; при None создаётся Aria2Engine.
            Запуском и закрытием движка управляет вызывающий код.
        """
        self.provider = provider
        self.engine = engine or Aria2Engine()

    def download(
        self,
        context: DownloadContext,
        directory: Path,
        options: DownloadOptions = _DEFAULT_OPTIONS,
        on_progress: Callable[[DownloadProgress], None] | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> DownloadResult:
        """Скачать аудио, записать теги и опубликовать итоговый файл.

        :param context: Трек и выбранный альбом.
        :param directory: Каталог назначения; создаётся при необходимости.
        :param options: Контейнер, битрейт и режим замены; None — настройки по умолчанию.
        :param on_progress: Callback движка с текущим DownloadProgress.
        :param check_cancelled: Callback без аргументов, выбрасывающий
            DownloadCancelled при запросе отмены; вызывается между этапами.

        :returns: Результат с итоговым путём и признаком пропуска существующего файла.

        :raises ValueError: Формат не поддерживается, нет аудио или данные тегов некорректны.
        :raises FileExistsError: Имя с ID занято другим или неизвестным треком.
        :raises DownloadCancelled: Загрузка отменена движком или callback.
        :raises OSError: Ошибка движка/файловой системы или пустой аудиофайл.

        Имя файла содержит название; при конфликте добавляется ID трека.
        Ошибки API и записи тегов передаются вызывающему коду.
        """
        if check_cancelled:
            check_cancelled()
        audio_format = get_download_format(options.container)  # TODO: Добавить FLAC, m4a

        track = context.track
        logger.info("Обработка трека %s", track.id)
        directory = Path(directory)
        target = self._target_path(track, directory, audio_format.extension)
        if target.is_file() and target.stat().st_size > 0 and not options.overwrite:
            logger.info("Трек %s уже скачан, пропуск", track.id)
            return DownloadResult(track, target, skipped=True)

        info = select_download_info(track, audio_format, options.bitrate)
        directory.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            logger.debug("Скачивание аудио трека %s", track.id)
            self.engine.download(info.get_direct_link(), temporary, on_progress=on_progress)
            if temporary.stat().st_size == 0:
                raise OSError("Сервер вернул пустой файл")
            if check_cancelled:
                check_cancelled()
            logger.debug("Получение метаданных трека %s", track.id)
            metadata = self._fetch_metadata(track)
            if check_cancelled:
                check_cancelled()
            write_tags(temporary, context, metadata=metadata, options=options)
            if check_cancelled:
                check_cancelled()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + ".aria2").unlink(missing_ok=True)
        logger.info("Трек %s сохранён", track.id)
        return DownloadResult(track, target)

    @staticmethod
    def _target_path(track: Track, directory: Path, extension: str) -> Path:
        """Выбрать имя файла с учётом ID существующего трека.

        :param track: Трек, для которого выбирается имя.
        :param directory: Каталог назначения.
        :param extension: Расширение поддерживаемого формата, включая точку.
        :returns: Путь файла с названием трека или названием и ID при конфликте.
        :raises FileExistsError: Имя с ID также занято другим или неизвестным треком.
        :raises OSError: Не удалось прочитать существующий файл.
        """
        name = safe_name(track.title).encode("utf-8")[:160].decode("utf-8", errors="ignore")
        track_id = safe_name(str(track.id))

        target = directory / f"{name}{extension}"
        if target.exists() and read_track_id(target) != str(track.id):
            target = directory / f"{name} [{track_id}]{extension}"
            if target.exists() and read_track_id(target) != str(track.id):
                raise FileExistsError(f"Файл занят другим или неизвестным треком: {target}")
        return target

    @staticmethod
    def _fetch_metadata(track: Track) -> TrackMetadata:
        """Получить обложку и доступный текст песни через API.

        :param track: Трек с информацией о наличии обложки и текста.
        :returns: Обложка и текст; отсутствующие данные представлены как None.
        """
        cover = track.download_cover_bytes(size="400x400") if track.cover_uri else None
        lyrics = None
        if track.lyrics_info and track.lyrics_info.has_available_text_lyrics:
            track_lyrics = track.get_lyrics(format_="TEXT")
            if track_lyrics:
                lyrics = track_lyrics.fetch_lyrics()
        return TrackMetadata(cover=cover, lyrics=lyrics)
