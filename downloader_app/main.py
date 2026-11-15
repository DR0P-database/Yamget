"""Точка входа CLI и коды завершения процесса."""

import sys
from collections.abc import Sequence

from .arguments import parse_options


def main(argv: Sequence[str] | None = None) -> int:
    options, token = parse_options(argv)

    # Загрузка runtime-зависимостей после --help и проверки аргументов.
    from mutagen import MutagenError
    from yandex_music.exceptions import YandexMusicError

    from .application import run_download

    try:
        return run_download(options, token)
    except (YandexMusicError, MutagenError, ValueError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Загрузка отменена", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
