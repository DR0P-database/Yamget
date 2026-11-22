from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from yandex_music import Album

from music_download.engine import Aria2Error, DownloadCancelled, DownloadProgress
from music_download.models import DownloadContext, DownloadOptions, DownloadResult
from music_download.queue import DownloadJob, DownloadQueue, JobState


def setup_queue():
    engine = MagicMock()
    service = SimpleNamespace(engine=engine)
    queue = DownloadQueue(service)
    jobs = [queue.add(DownloadContext(SimpleNamespace(id=str(index)), Album())) for index in range(3)]
    return queue, service, jobs


def test_each_job_uses_its_own_quality(tmp_path):
    queue, service, jobs = setup_queue()
    custom = queue.add(DownloadContext(SimpleNamespace(id="custom"), Album(id=42)),
                       DownloadOptions(overwrite=True, bitrate=320))
    service.download = MagicMock(return_value=DownloadResult(custom.context.track, Path("track")))
    list(queue.run(tmp_path))
    first, last = service.download.call_args_list[0], service.download.call_args_list[-1]
    assert first.args == (jobs[0].context, tmp_path)
    assert first.kwargs["options"].bitrate is None
    assert last.args == (custom.context, tmp_path)
    assert last.kwargs["options"].bitrate == 320
    assert last.kwargs["options"].overwrite is True
    assert last.args[0].album.id == 42
    assert first.args[0].album.id is None


@pytest.mark.parametrize("method", ["pause", "resume", "cancel"])
def test_controls_reject_foreign_job(method):
    queue, service, jobs = setup_queue()
    foreign = DownloadJob(jobs[0].context)
    with pytest.raises(ValueError, match="не принадлежит"):
        getattr(queue, method)(foreign)
    assert not service.engine.method_calls


def test_repeated_cancel_is_harmless():
    queue, _service, jobs = setup_queue()
    queue.cancel(job=jobs[0])
    queue.cancel(job=jobs[0])
    assert jobs[0].state == JobState.CANCELLED


def test_cancellation_is_checked_by_service_and_progress(tmp_path):
    queue, service, jobs = setup_queue()

    def download(context, *args, on_progress, check_cancelled, **kwargs):
        job = next(job for job in jobs if job.context is context)
        queue.cancel(job)
        with pytest.raises(DownloadCancelled):
            check_cancelled()
        on_progress(DownloadProgress(context.track.id, "active", 0, 100, 1))

    service.download = download
    list(queue.run(tmp_path))
    assert all(job.state == JobState.CANCELLED for job in jobs)


def test_one_engine_for_whole_queue_and_failure_isolation(tmp_path):
    queue, service, jobs = setup_queue()

    def download(context, *args, on_progress, check_cancelled, **kwargs):
        on_progress(DownloadProgress(context.track.id, "active", 5, None, 2))
        if context.track.id == "1":
            raise Aria2Error("failed")
        return DownloadResult(context.track, tmp_path / context.track.id)

    service.download = download
    assert list(queue.run(tmp_path)) == jobs
    assert [job.state for job in jobs] == [JobState.COMPLETED, JobState.FAILED,
                                         JobState.COMPLETED]
    assert jobs[0].gid == "0"
    assert jobs[0].progress.total_bytes is None
    service.engine.__enter__.assert_called_once()
    service.engine.__exit__.assert_called_once()


def test_pause_resume_cancel_and_pending_skip(tmp_path):
    queue, service, jobs = setup_queue()
    queue.cancel(jobs[1])

    def download(context, *args, on_progress, check_cancelled, **kwargs):
        job = next(job for job in jobs if job.context is context)
        on_progress(DownloadProgress(context.track.id, "active", 0, 100, 1))
        queue.pause(job)
        on_progress(DownloadProgress(context.track.id, "paused", 0, 100, 0))
        assert job.state == JobState.PAUSED
        queue.resume(job)
        queue.cancel(job)
        raise DownloadCancelled()

    service.download = download
    list(queue.run(tmp_path))
    assert all(job.state == JobState.CANCELLED for job in jobs)
    assert service.engine.pause.call_count == 2
    assert service.engine.resume.call_count == 2
    assert service.engine.cancel.call_count == 2


def test_ctrl_c_marks_remaining_jobs_and_closes_engine(tmp_path):
    queue, service, jobs = setup_queue()
    service.download = MagicMock(side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        list(queue.run(tmp_path))
    assert all(job.state == JobState.CANCELLED for job in jobs)
    service.engine.__exit__.assert_called_once()


def test_closing_iterator_stops_engine_and_cancels_pending(tmp_path):
    queue, service, jobs = setup_queue()
    service.download = lambda context, *args, **kwargs: DownloadResult(context.track, Path("track"))
    iterator = queue.run(tmp_path)
    next(iterator)
    iterator.close()
    assert jobs[0].state == JobState.COMPLETED
    assert jobs[1].state == JobState.CANCELLED
    service.engine.__exit__.assert_called_once()


def test_reentrant_run_does_not_stop_current_queue(tmp_path):
    queue, service, jobs = setup_queue()
    service.download = lambda context, *args, **kwargs: DownloadResult(context.track, Path("track"))
    iterator = queue.run(tmp_path)
    next(iterator)
    with pytest.raises(RuntimeError, match="уже запущена"):
        next(queue.run(tmp_path))
    assert list(iterator) == jobs[1:]
    service.engine.__exit__.assert_called_once()
