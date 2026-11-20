"""Получение треков и выполнение очереди для консольного приложения."""

import logging
from contextlib import closing

from music_download.engine import Aria2Engine
from music_download.models import DownloadContext, DownloadOptions
from music_download.providers.yandex import YandexProvider
from music_download.queue import DownloadQueue
from music_download.service import DownloadService
from music_download.sources import Source

from .arguments import CliOptions
from .output import report_job, report_track_error

logger = logging.getLogger(__name__)


def populate_queue(
    queue: DownloadQueue,
    provider: YandexProvider,
    source: Source,
    options: DownloadOptions,
) -> int:
    """Добавить доступные треки, сохранив ошибки отдельных записей."""
    logger.info("Получение треков: тип источника=%s", source.kind)
    failed = 0
    added = 0
    for entry in provider.resolve_source(source):
        if entry.error is not None:
            failed += 1
            report_track_error(entry.track_id, entry.error)
            continue
        if entry.track is None:
            raise ValueError(f"API не вернул трек {entry.track_id}")
        queue.add(DownloadContext.from_track(entry.track, source.album_id), options)
        added += 1

    if not added and not failed:
        if source.kind == "album":
            logger.info("Альбом пуст")
        elif source.kind == "playlist":
            logger.info("Плейлист пуст")
    logger.info("Очередь подготовлена: заданий=%d, ошибок=%d", added, failed)
    return failed


def run_download(options: CliOptions, token: str) -> int:
    logger.info("Запуск загрузки: битрейт=%s, перезапись=%s", options.bitrate, options.overwrite)
    engine = Aria2Engine()
    engine.check_available()
    provider = YandexProvider.from_token(token)  # Получаем тонкий клиент по токену
    queue = DownloadQueue(DownloadService(provider, engine))
    download_options = DownloadOptions(overwrite=options.overwrite, bitrate=options.bitrate)
    failed = populate_queue(queue, provider, options.source, download_options)  # наполняем очередь

    # Гарантировано по цепочке закроем все контексты и aria процесс
    with closing(queue.run(options.output)) as jobs:
        for job in jobs:
            failed += report_job(job)
    logger.info("Обработка завершена: ошибок=%d", failed)
    return 1 if failed else 0
