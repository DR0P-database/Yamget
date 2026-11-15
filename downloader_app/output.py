"""Сообщения консольного интерфейса."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_download.queue import DownloadJob


def print_track_error(track_id: str, error: Exception) -> None:
    print(f"Ошибка трека {track_id}: {error}", file=sys.stderr)


def report_job(job: DownloadJob) -> bool:
    """Вывести итог задания и вернуть признак ошибки или отмены."""
    from music_download.queue import JobState

    if job.state == JobState.CANCELLED:
        print(f"Отменён: {job.context.track.id}", file=sys.stderr)
        return True
    if job.error is not None:
        print_track_error(str(job.context.track.id), job.error)
        return True
    if job.result is None:
        raise ValueError(f"Нет результата загрузки трека {job.context.track.id}")

    status = "Пропущен" if job.result.skipped else "Скачан"
    print(f"{status}: {job.result.path}")
    return False
