import contextlib
import io
import unittest
from unittest.mock import patch

from downloader_app.main import main


class CliArgumentTests(unittest.TestCase):
    def exit_code(self, arguments):
        with patch("sys.argv", ["music-download", *arguments]), patch.dict(
            "os.environ", {}, clear=True
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
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
        self.assertEqual(self.exit_code(["123"]), 2)
