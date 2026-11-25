import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import ANY, patch

from downloader_app.auth import load_local_token
from downloader_app.main import main


class CliArgumentTests(unittest.TestCase):
    def exit_code(self, arguments):
        with (
            patch("sys.argv", ["music-download", *arguments]),
            patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            main()
        return raised.exception.code

    def test_help_without_token_or_runtime_dependencies(self):
        self.assertEqual(self.exit_code(["--help"]), 0)

    def test_invalid_argument_combinations(self):
        for arguments in [[], ["--user", "u"], ["--playlist", "1"],
                          ["1", "--playlist", "2", "--user", "u"],
                          ["--playlist", "0", "--user", "u"], ["1:2:3"]]:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.exit_code(arguments), 2)

    def test_missing_token(self):
        with (
            patch("downloader_app.main.load_local_token", return_value=None),
            patch("sys.stdin.isatty", return_value=False),
        ):
            self.assertEqual(self.exit_code(["123"]), 2)

    def test_first_run_saves_token_and_reuses_it_with_environment_override(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("OTHER_SETTING=value", encoding="utf-8")
            with (
                patch("downloader_app.main.Path", return_value=env_path),
                patch.dict("os.environ", {}, clear=True),
                patch("sys.stdin.isatty", return_value=True),
                patch("yandex_music.Client") as client,
                patch("downloader_app.application.run_download", return_value=0) as download,
            ):
                client.return_value.device_auth.return_value = SimpleNamespace(
                    access_token="saved-token"
                )
                self.assertEqual(main(["123"]), 0)
                self.assertEqual(load_local_token(env_path), "saved-token")
                self.assertIn("OTHER_SETTING=value", env_path.read_text(encoding="utf-8"))
                download.assert_called_with(ANY, "saved-token")

                self.assertEqual(main(["123"]), 0)
                download.assert_called_with(ANY, "saved-token")

                with patch.dict("os.environ", {"YANDEX_MUSIC_TOKEN": "override"}):
                    self.assertEqual(main(["123"]), 0)
                download.assert_called_with(ANY, "override")
                client.return_value.device_auth.assert_called_once()
