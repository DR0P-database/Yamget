import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yandex_music import Album, Track

from downloader_app.application import populate_queue, run_download
from downloader_app.arguments import CliOptions, parse_options
from downloader_app.main import main
from downloader_app.output import report_job
from music_download.models import DownloadContext, DownloadOptions, DownloadResult
from music_download.providers.yandex import PlaylistEntry
from music_download.queue import DownloadJob, JobState
from music_download.sources import Source


class CliApplicationTests(unittest.TestCase):
    def test_playlist_arguments(self):
        cases = [
            (["--playlist", "12", "--user", "owner"], Source(user_id="owner", playlist_id=12)),
            (["--playlist", "playlist-key"], Source(playlist_key="playlist-key")),
            (["https://music.yandex.ru/album/42"], Source(album_id="42")),
        ]
        with patch.dict("os.environ", {"YANDEX_MUSIC_TOKEN": "token"}):
            for arguments, source in cases:
                with self.subTest(arguments=arguments):
                    options, token = parse_options(arguments)
                    self.assertEqual(options.source, source)
                    self.assertEqual(token, "token")

    def test_populate_queue_preserves_album_and_continues_after_entry_error(self):
        album = Album(id=42)
        track = Track(id="1", albums=[album])
        provider = Mock()
        provider.resolve_source.return_value = iter(
            [
                PlaylistEntry(0, "2", error=ValueError("Недоступен")),
                PlaylistEntry(1, "1", track=track),
            ]
        )
        queue = Mock()
        options = DownloadOptions(bitrate=320)
        with self.assertLogs("downloader_app.output", level="ERROR") as output:
            failed = populate_queue(queue, provider, Source(album_id="42"), options)
        self.assertEqual(failed, 1)
        self.assertIn("Ошибка трека 2", "\n".join(output.output))
        queue.add.assert_called_once_with(DownloadContext(track, album), options)

    def test_report_job_results(self):
        context = DownloadContext(Track(id="1"), Album())
        cases = [
            (DownloadJob(context, state=JobState.CANCELLED), True, "Отменён"),
            (DownloadJob(context, error=ValueError("Ошибка API")), True, "ValueError"),
            (
                DownloadJob(context, result=DownloadResult(context.track, Path("a.mp3"))),
                False,
                "Скачан",
            ),
            (
                DownloadJob(context, result=DownloadResult(context.track, Path("a.mp3"), True)),
                False,
                "Пропущен",
            ),
        ]
        for job, failed, message in cases:
            with (
                self.subTest(message=message),
                self.assertLogs("downloader_app.output", level="INFO") as output,
            ):
                self.assertEqual(report_job(job), failed)
                self.assertIn(message, "\n".join(output.output))

    def test_run_download_closes_iterator_and_counts_failure(self):
        options = CliOptions(Source(track_id="1"), Path("downloads"), 320, True)
        jobs = Mock()
        jobs.__iter__ = Mock(return_value=iter([Mock()]))
        with (
            patch("downloader_app.application.Aria2Engine"),
            patch("downloader_app.application.YandexProvider"),
            patch("downloader_app.application.DownloadService"),
            patch("downloader_app.application.DownloadQueue") as queue,
            patch("downloader_app.application.populate_queue", return_value=0),
            patch("downloader_app.application.report_job", return_value=True),
        ):
            queue.return_value.run.return_value = jobs
            self.assertEqual(run_download(options, "token"), 1)
        jobs.close.assert_called_once()
        queue.return_value.run.assert_called_once_with(options.output)

    def test_main_exit_codes(self):
        with patch.dict("os.environ", {"YANDEX_MUSIC_TOKEN": "token"}):
            for error, code in [(OSError("aria2 недоступен"), 1), (KeyboardInterrupt(), 130)]:
                with (
                    self.subTest(code=code),
                    patch("downloader_app.application.run_download", side_effect=error),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(main(["123"]), code)
