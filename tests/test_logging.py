import contextlib
import io
import logging
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from yandex_music import Album, Playlist, Track

from downloader_app.arguments import parse_options
from downloader_app.main import configure_logging, main
from downloader_app.output import report_track_error
from music_download.engine import Aria2Engine
from music_download.models import DownloadContext
from music_download.queue import DownloadQueue


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.loggers = [logging.getLogger(name) for name in ("downloader_app", "music_download")]
        self.saved = [(item.handlers[:], item.level, item.propagate) for item in self.loggers]

    def tearDown(self):
        for item, (handlers, level, propagate) in zip(self.loggers, self.saved):
            item.handlers[:] = handlers
            item.setLevel(level)
            item.propagate = propagate

    def test_reconfiguration_does_not_duplicate_logs_or_modify_root(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        with contextlib.redirect_stderr(io.StringIO()) as output:
            configure_logging("INFO")
            configure_logging("DEBUG")
            logging.getLogger("music_download.engine").debug("test event")
            logging.getLogger("downloader_app.application").info("application event")
        self.assertEqual(output.getvalue().count("test event"), 1)
        self.assertEqual(output.getvalue().count("application event"), 1)
        self.assertEqual(root.handlers, handlers)
        self.assertEqual(root.level, level)

    def test_log_level_filters_debug(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            configure_logging("WARNING")
            logging.getLogger("music_download.engine").debug("hidden")
            logging.getLogger("music_download.engine").warning("visible")
        self.assertNotIn("hidden", output.getvalue())
        self.assertIn("visible", output.getvalue())

    def test_queue_error_does_not_log_exception_payload(self):
        service = Mock()
        service.engine.__enter__ = Mock()
        service.engine.__exit__ = Mock(return_value=False)
        service.download.side_effect = ValueError("secret-token https://audio.test/?sign=secret")
        queue = DownloadQueue(service)
        queue.add(DownloadContext(Track(id="123"), Album()))
        with contextlib.redirect_stderr(io.StringIO()) as output:
            configure_logging("DEBUG")
            list(queue.run(Path("downloads")))
        self.assertIn("ValueError", output.getvalue())
        self.assertIn("123", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("https://", output.getvalue())

    def test_rpc_does_not_log_secret_or_parameters(self):
        engine = Aria2Engine()
        engine._process = Mock()
        engine._process.poll.return_value = None
        engine._secret = "rpc-secret"
        engine._endpoint = "http://127.0.0.1:12345/jsonrpc"
        engine._opener = MagicMock()
        with contextlib.redirect_stderr(io.StringIO()) as output:
            configure_logging("DEBUG")
            with (
                patch(
                    "music_download.engine.json.load", return_value={"id": "id", "result": "gid"}
                ),
                patch("music_download.engine.secrets.token_hex", return_value="id"),
            ):
                self.assertEqual(engine._rpc("addUri", ["https://audio.test/?sign=secret"]), "gid")
        self.assertIn("addUri", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("https://", output.getvalue())

    def test_cli_log_level(self):
        with patch.dict("os.environ", {"YANDEX_MUSIC_TOKEN": "secret-token"}):
            options, token = parse_options(["123", "--log-level", "debug"])
        self.assertEqual(options.log_level, "DEBUG")
        self.assertEqual(token, "secret-token")

    def test_cli_errors_use_logging_without_exposing_exception_payload(self):
        with (
            patch.dict("os.environ", {"YANDEX_MUSIC_TOKEN": "secret-token"}),
            patch(
                "downloader_app.application.run_download",
                side_effect=OSError("secret-token https://audio.test/?sign=secret"),
            ),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(main(["123"]), 1)
        self.assertIn("ERROR downloader_app.main", output.getvalue())
        self.assertIn("OSError", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_track_error_uses_error_level_without_exception_payload(self):
        with self.assertLogs("downloader_app.output", level="ERROR") as output:
            report_track_error("123", ValueError("secret-token"))
        self.assertIn("Ошибка трека 123: ValueError", output.output[0])
        self.assertNotIn("secret-token", output.output[0])

    def test_unavailable_playlist_track_reports_reason_once(self):
        from music_download.providers.yandex import YandexProvider

        provider = YandexProvider(Mock())
        playlist = Playlist(cover=None, tracks=[Track(id="150325301", available=False)])
        with contextlib.redirect_stderr(io.StringIO()) as output:
            configure_logging("INFO")
            entry = next(provider.resolve_playlist(playlist))
            report_track_error(entry.track_id, entry.error)
        self.assertIn("Ошибка трека 150325301: недоступен", output.getvalue())
        self.assertEqual(output.getvalue().count("150325301"), 1)
        self.assertNotIn("ValueError", output.getvalue())
