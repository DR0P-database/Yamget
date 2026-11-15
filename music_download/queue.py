"""Последовательная очередь загрузки треков с выбранными альбомами."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from functools import partial, wraps
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate, ParamSpec, TypeVar

from music_download.engine import DownloadCancelled, DownloadProgress
from music_download.models import DownloadContext, DownloadOptions, DownloadResult

if TYPE_CHECKING:
    from music_download.service import DownloadService

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _check_job(
    method: Callable[Concatenate[DownloadQueue, DownloadJob, _P], _R],
) -> Callable[Concatenate[DownloadQueue, DownloadJob, _P], _R]:
    """Проверить принадлежность задания очереди перед вызовом метода.

    :param method: Метод очереди с аргументами self, job и дополнительными параметрами.
    :returns: Обёрнутый метод с сохранением результата и метаданных исходного метода.
    :raises ValueError: При вызове обёртки задание не принадлежит очереди.
    """
    @wraps(method)
    def wrapper(
        self: DownloadQueue, 
        job: DownloadJob,
        *args: _P.args, 
        **kwargs: _P.kwargs
    ) -> _R:
        if not any(candidate is job for candidate in self.jobs):
            raise ValueError("Задание не принадлежит этой очереди")
        return method(self, job, *args, **kwargs)

    return wrapper


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadJob:
    """Состояние выполнения со ссылками на данные и настройки загрузки."""
    context: DownloadContext
    options: DownloadOptions = field(default_factory=DownloadOptions)
    state: JobState = JobState.PENDING
    result: DownloadResult | None = None
    error: Exception | None = None
    progress: DownloadProgress | None = None
    cancel_requested: bool = False

    @property
    def gid(self) -> str | None:
        """Вернуть GID из последнего прогресса или None до первого обновления."""
        return self.progress.gid if self.progress else None


class DownloadQueue:
    def __init__(self, service: DownloadService):
        """Создать последовательную очередь в памяти.

        :param service: Сервис скачивания; очередь управляет его движком в run().
        """
        self.service = service
        self.jobs: list[DownloadJob] = []
        self._pending: deque[DownloadJob] = deque()
        self._running = False

    def add(
        self, context: DownloadContext, options: DownloadOptions | None = None,
    ) -> DownloadJob:
        """Добавить трек с выбранным альбомом и настройками без обращения к API."""
        job = DownloadJob(context, options=options if options is not None else DownloadOptions())
        self.jobs.append(job)
        self._pending.append(job)
        return job

    @_check_job
    def pause(self, job: DownloadJob) -> None:
        """Запросить паузу загрузки.

        :param job: Задание этой очереди.

        :raises ValueError: Задание не принадлежит очереди или не имеет активной загрузки.
        :raises Aria2Error: Движок отклонил операцию или недоступен.
        """
        if job.state != JobState.RUNNING or job.gid is None:
            raise ValueError("У задания нет активной загрузки")
        self.service.engine.pause(job.gid)

    @_check_job
    def resume(self, job: DownloadJob) -> None:
        """Запросить продолжение загрузки.

        :param job: Задание этой очереди.

        :raises ValueError: Задание не принадлежит очереди или не приостановлено.
        :raises Aria2Error: Движок отклонил операцию или недоступен.
        """
        if job.state != JobState.PAUSED or job.gid is None:
            raise ValueError("Загрузка не приостановлена")
        self.service.engine.resume(job.gid)

    @_check_job
    def cancel(self, job: DownloadJob) -> None:
        """Запросить отмену задания.

        :param job: Задание этой очереди.

        :raises ValueError: Задание не принадлежит очереди.
        :raises Aria2Error: Движок отклонил операцию или недоступен.
        """
        if job.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
            return
        job.cancel_requested = True
        if job.state == JobState.PENDING:
            job.state = JobState.CANCELLED
        elif job.gid:
            self.service.engine.cancel(job.gid)

    def _check_cancelled(self, job: DownloadJob) -> None:
        """Прервать обработку при запросе отмены.

        :param job: Проверяемое задание.

        :raises DownloadCancelled: У задания установлен cancel_requested.
        """
        if job.cancel_requested:
            raise DownloadCancelled("Загрузка отменена")

    def _progress(self, job: DownloadJob, progress: DownloadProgress) -> None:
        """Обновить прогресс и состояние задания по данным движка.

        :param job: Задание, для которого получен прогресс.
        :param progress: Последнее состояние загрузки aria2.

        :raises DownloadCancelled: До обновления состояния запрошена отмена.
        """
        job.progress = progress
        self._check_cancelled(job)
        job.state = JobState.PAUSED if progress.status == "paused" else JobState.RUNNING

    def run(
        self, 
        directory: Path,
    ) -> Iterator[DownloadJob]:
        """Последовательно выполнить задания с индивидуальными настройками качества.

        :param directory: Общий каталог назначения для файлов очереди.
        :yields: Обработанное или отменённое задание с итоговым состоянием.
        :raises RuntimeError: Этот экземпляр очереди уже выполняется.
        :raises Aria2Error: Не удалось запустить или закрыть движок.

        Движок работает до исчерпания очереди или закрытия итератора.
        Ошибка одного задания не прерывает остальные; отмена проверяется
        во время загрузки и перед последующими этапами обработки файла.
        """
        # The generator owns the engine for the entire queue, including tagging.
        if self._running:
            raise RuntimeError("Очередь уже запущена")
        self._running = True
        try:
            with self.service.engine:
                while self._pending:
                    job = self._pending.popleft()
                    if job.state == JobState.CANCELLED:
                        yield job
                        continue
                    job.state = JobState.RUNNING
                    try:
                        job.result = self.service.download(
                            job.context, directory, options=job.options,
                            on_progress=partial(self._progress, job),
                            check_cancelled=partial(self._check_cancelled, job),
                        )
                        job.state = JobState.COMPLETED
                    except DownloadCancelled:
                        job.state = JobState.CANCELLED
                    except Exception as error:  # noqa: BLE001 - isolate failed queue jobs
                        job.error = error
                        job.state = JobState.FAILED
                    except BaseException:
                        job.state = JobState.CANCELLED
                        raise
                    yield job
        finally:
            self._running = False
            for pending in self._pending:
                pending.state = JobState.CANCELLED
            self._pending.clear()
