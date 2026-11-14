"""Sequential in-memory queue; API links are resolved when each job starts."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from functools import partial, wraps
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate, ParamSpec, TypeVar

from music_download.engine import DownloadCancelled, DownloadProgress
from music_download.models import DownloadResult, Track

if TYPE_CHECKING:
    from music_download.service import DownloadService

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _check_job(
    method: Callable[Concatenate[DownloadQueue, DownloadJob, _P], _R],
) -> Callable[Concatenate[DownloadQueue, DownloadJob, _P], _R]:
    """Проверить принадлежность задания очереди перед вызовом метода."""
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
    track: Track
    codec: str = "mp3"
    bitrate: int | None = None
    state: JobState = JobState.PENDING
    result: DownloadResult | None = None
    error: Exception | None = None
    progress: DownloadProgress | None = None
    cancel_requested: bool = False

    @property
    def gid(self) -> str | None:
        return self.progress.gid if self.progress else None


class DownloadQueue:
    def __init__(self, service: DownloadService):
        self.service = service
        self.jobs: list[DownloadJob] = []
        self._pending: deque[DownloadJob] = deque()
        self._running = False

    def add(self, track: Track, codec: str = "mp3",
            bitrate: int | None = None) -> DownloadJob:
        job = DownloadJob(track, codec=codec, bitrate=bitrate)
        self.jobs.append(job)
        self._pending.append(job)
        return job

    @_check_job
    def pause(self, job: DownloadJob) -> None:
        if job.state != JobState.RUNNING or job.gid is None:
            raise ValueError("У задания нет активной загрузки")
        self.service.engine.pause(job.gid)

    @_check_job
    def resume(self, job: DownloadJob) -> None:
        if job.state != JobState.PAUSED or job.gid is None:
            raise ValueError("Загрузка не приостановлена")
        self.service.engine.resume(job.gid)

    @_check_job
    def cancel(self, job: DownloadJob) -> None:
        if job.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
            return
        job.cancel_requested = True
        if job.state == JobState.PENDING:
            job.state = JobState.CANCELLED
        elif job.gid:
            self.service.engine.cancel(job.gid)

    def _check_cancelled(self, job: DownloadJob) -> None:
        if job.cancel_requested:
            raise DownloadCancelled("Загрузка отменена")

    def _progress(self, job: DownloadJob, progress: DownloadProgress) -> None:
        job.progress = progress
        self._check_cancelled(job)
        job.state = JobState.PAUSED if progress.status == "paused" else JobState.RUNNING

    def run(
        self, 
        directory: Path, 
        overwrite: bool = False
    ) -> Iterator[DownloadJob]:
        """Последовательно выполнить задания с индивидуальными настройками качества.

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
                            job.track, directory, job.codec, job.bitrate, overwrite,
                            on_progress=partial(self._progress, job),
                            check_cancelled=partial(self._check_cancelled, job)
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
