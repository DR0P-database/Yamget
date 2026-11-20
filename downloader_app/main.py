"""Точка входа CLI и коды завершения процесса."""

import logging
from collections.abc import Sequence

from .arguments import parse_options

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

    try:
        exit_code = run_download(options, token)
        logger.info("CLI завершён: код=%d", exit_code)
        return exit_code
    except (YandexMusicError, MutagenError, ValueError, OSError) as error:
        logger.error("Загрузка завершилась ошибкой: %s", type(error).__name__)
        return 1
    except KeyboardInterrupt:
        logger.info("Загрузка прервана пользователем")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
