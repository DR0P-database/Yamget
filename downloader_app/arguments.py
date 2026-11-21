"""Аргументы CLI и проверка входных данных до обращения к API."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from music_download.paths import DEFAULT_TEMPLATE, PathTemplate
from music_download.sources import Source, positive_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliOptions:
    source: Source
    output: Path
    bitrate: int | None
    overwrite: bool
    log_level: str = "INFO"
    path_template: PathTemplate = DEFAULT_TEMPLATE


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Скачивание треков, альбомов и плейлистов Яндекс Музыки"
    )
    parser.add_argument("source", nargs="?", help="ID трека, ключ плейлиста или ссылка")
    parser.add_argument("--playlist", help="ключ плейлиста; числовой ID — вместе с --user")
    parser.add_argument("--user", help="Владелец плейлиста")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("downloads"),
        help="Каталог назначения (по умолчанию downloads)",
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        choices=[64, 128, 192, 320],
        help="Желаемый битрейт MP3 в кбит/с; если недоступен, выбирается максимальный",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Заменить существующий файл после успешной загрузки",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Уровень логирования (по умолчанию INFO)",
    )
    parser.add_argument(
        "--path-template",
        type=PathTemplate,
        default=DEFAULT_TEMPLATE,
        help=(
            "Путь внутри --output без расширения (по умолчанию {title}). "
            "Поля: artist, album, title, track_id, album_id, track_number, disc_number, year"
        ),
    )
    return parser


def resolve_source(args: argparse.Namespace) -> Source:
    if args.playlist is None:
        if not args.source or args.user:
            raise ValueError("Укажите источник; --user используется с --playlist ID")
        return Source.parse(args.source)

    if args.source:
        raise ValueError("--playlist не сочетается с позиционным источником")
    if args.user:
        if not positive_id(args.playlist):
            raise ValueError("С --user укажите положительный числовой ID плейлиста")
        return Source(user_id=args.user, playlist_id=int(args.playlist))

    source = Source.parse(args.playlist)
    if source.kind != "playlist":
        raise ValueError("Без --user укажите ключ плейлиста или ссылку на плейлист")
    return source


def parse_options(argv: Sequence[str] | None = None) -> tuple[CliOptions, str]:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        source = resolve_source(args)
    except ValueError as error:
        parser.error(str(error))

    token = os.environ.get("YANDEX_MUSIC_TOKEN")
    if not token or not token.strip():
        parser.error("Задайте переменную окружения YANDEX_MUSIC_TOKEN")

    logger.debug("Аргументы проверены: тип источника=%s", source.kind)
    return CliOptions(
        source=source,
        output=args.output,
        bitrate=args.bitrate,
        overwrite=args.overwrite,
        log_level=args.log_level,
        path_template=args.path_template,
    ), token
