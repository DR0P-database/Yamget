"""Точка входа CLI и коды завершения процесса."""

import logging
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from .arguments import create_parser, parse_options
from .auth import TOKEN_KEY, authorize_and_save, load_local_token

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Настроить вывод логов приложения, не включая HTTP-логи зависимостей."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    for name in ("downloader_app", "music_download"):
        package_logger = logging.getLogger(name)
        package_logger.handlers[:] = [handler]
        package_logger.setLevel(level)
        package_logger.propagate = False


def main(argv: Sequence[str] | None = None) -> int:
    options, token = parse_options(argv)
    configure_logging(options.log_level)
    logger.debug("CLI запущен: тип источника=%s", options.source.kind)

    # Загрузка runtime-зависимостей после --help и проверки аргументов.
    from mutagen import MutagenError
    from yandex_music.exceptions import YandexMusicError

    from .application import run_download
    from .output import describe_error

    try:
        if not token:
            env_path = Path(".env")
            token = load_local_token(env_path)
            if not token:
                if not sys.stdin.isatty():
                    create_parser().error(
                        f"Задайте {TOKEN_KEY} или запустите Yamget в терминале для входа"
                    )
                token = authorize_and_save(env_path)
                logger.info("Токен Яндекс Музыки сохранён в .env")
        exit_code = run_download(options, token)
        logger.info("CLI завершён: код=%d", exit_code)
        return exit_code
    except (YandexMusicError, MutagenError, ValueError, OSError, sqlite3.Error) as error:
        logger.error("Загрузка завершилась ошибкой: %s", describe_error(error))
        return 1
    except KeyboardInterrupt:
        logger.info("Загрузка прервана пользователем")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
