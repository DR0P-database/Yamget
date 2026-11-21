"""Сообщения консольного интерфейса."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_download.providers.yandex import ResourceError

if TYPE_CHECKING:
    from music_download.queue import DownloadJob


logger = logging.getLogger(__name__)


def describe_error(error: Exception) -> str:
    """Показать известную причину, не раскрывая произвольный ответ API."""
    return error.reason if isinstance(error, ResourceError) else type(error).__name__


def report_track_error(track_id: str, error: Exception) -> None:
    logger.error("Ошибка трека %s: %s", track_id, describe_error(error))


def report_job(job: DownloadJob) -> bool:
    """Вывести итог задания и вернуть признак ошибки или отмены."""
    from music_download.queue import JobState

    if job.state == JobState.CANCELLED:
        logger.warning("Отменён: %s", job.context.track.id)
        return True
    if job.error is not None:
        report_track_error(str(job.context.track.id), job.error)
        return True
    if job.result is None:
        raise ValueError(f"Нет результата загрузки трека {job.context.track.id}")

    status = "Пропущен" if job.result.skipped else "Скачан"
    logger.info("%s: %s", status, job.result.path)
    return False
