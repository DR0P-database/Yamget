from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from yandex_music import Track

from music_download.engine import Aria2Engine, DownloadProgress
from music_download.formats import get_download_format, select_download_info
from music_download.index import LibraryIndex
from music_download.models import DownloadContext, DownloadOptions, DownloadResult, TrackMetadata
from music_download.paths import ensure_inside, select_target_path
from music_download.relocation import relocate_file
from music_download.tags import write_tags

if TYPE_CHECKING:
    from music_download.providers.yandex import YandexProvider


logger = logging.getLogger(__name__)
_DEFAULT_OPTIONS = DownloadOptions()


class DownloadService:
    def __init__(
        self,
        provider: YandexProvider,
        engine: Aria2Engine | None = None,
        index: LibraryIndex | None = None,
    ):
        """Создать сервис скачивания.

        :param provider: API-провайдер музыкальных данных.
        :param index: Общий индекс команды; жизненным циклом управляет вызывающий код.
        :param engine: Движок загрузки; при None создаётся Aria2Engine.
            Запуском и закрытием движка управляет вызывающий код.
        """
        self.provider = provider
        self.engine = engine or Aria2Engine()
        self.index = index

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
        :param options: Контейнер, битрейт, шаблон пути и режим замены.
        :param on_progress: Callback движка с текущим DownloadProgress.
        :param check_cancelled: Callback без аргументов, выбрасывающий
            DownloadCancelled при запросе отмены; вызывается между этапами.

        :returns: Результат с итоговым путём и признаком пропуска существующего файла.

        :raises ValueError: Формат не поддерживается, нет аудио или данные тегов некорректны.
        :raises FileExistsError: Имя с ID занято другим или неизвестным треком.
        :raises DownloadCancelled: Загрузка отменена движком или callback.
        :raises OSError: Ошибка движка/файловой системы или пустой аудиофайл.

        Путь строится по шаблону; при конфликте имени добавляется ID трека.
        Ошибки API и записи тегов передаются вызывающему коду.
        """
        if check_cancelled:
            check_cancelled()
        audio_format = get_download_format(options.container)  # TODO: Добавить FLAC, m4a

        track = context.track
        album_id = str(context.album.id) if context.album.id is not None else None
        logger.info("Обработка трека %s", track.id)
        directory = Path(directory)
        if self.index is not None and directory.resolve() != self.index.directory:
            raise ValueError("Корень индекса не совпадает с каталогом загрузки")
        target = select_target_path(
            directory, context, options.path_template, audio_format.extension
        )
        if target.is_file() and target.stat().st_size > 0 and not options.overwrite:
            return DownloadResult(track, target, skipped=True)

        if self.index is not None and not options.overwrite:
            existing = self.index.find(str(track.id), album_id)
            if existing is not None:
                relocate_file(existing, target, self.index, check_cancelled)
                return DownloadResult(track, target, relocated=True)

        info = select_download_info(track, audio_format, options.bitrate)
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as handle:
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
            ensure_inside(directory, target)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + ".aria2").unlink(missing_ok=True)
        if self.index is not None:
            self.index.record(target, str(track.id), album_id)
        logger.info("Трек %s сохранён", track.id)
        return DownloadResult(track, target)

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
