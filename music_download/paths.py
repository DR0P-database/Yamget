"""Шаблоны путей внутри каталога загрузки."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from string import Formatter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_download.models import DownloadContext

logger = logging.getLogger(__name__)
DEFAULT_PATH_TEMPLATE = "{title}"
_TEXT_FIELDS = frozenset({"artist", "album", "title", "track_id", "album_id"})
_NUMBER_FIELDS = frozenset({"track_number", "disc_number", "year"})
_FORMATTER = Formatter()


def _validate_path_template(template: str) -> tuple[str, ...]:
    """Проверить относительный шаблон и разрешённые поля до обращения к API."""
    if not template or "\\" in template or PureWindowsPath(template).drive:
        raise ValueError("Шаблон должен быть относительным путём с разделителем /")
    components = tuple(template.split("/"))
    for component in components:
        if component.strip() in {"", ".", ".."}:
            raise ValueError("Шаблон содержит пустой компонент, . или ..")
        for _, field_name, spec, conversion in _FORMATTER.parse(component):
            if field_name is None:
                continue
            if field_name not in _TEXT_FIELDS | _NUMBER_FIELDS:
                raise ValueError(f"Неизвестное поле шаблона: {field_name}")
            if conversion is not None:
                raise ValueError("Преобразования !s, !r и !a в шаблоне не поддерживаются")
            if spec and (
                field_name not in _NUMBER_FIELDS or not re.fullmatch(r"0?[1-9][0-9]?d", spec)
            ):
                raise ValueError("Формат допускается только для чисел, например {track_number:02d}")

    return components


@dataclass(frozen=True)
class PathTemplate:
    """Проверенный шаблон, который можно переиспользовать для всех треков."""

    pattern: str = DEFAULT_PATH_TEMPLATE
    components: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", _validate_path_template(self.pattern))


DEFAULT_TEMPLATE = PathTemplate()


def safe_name(value: str, fallback: str = "track") -> str:
    """Очистить отдельное имя, сохраняя запас для расширения и суффикса ID."""
    from pathvalidate import sanitize_filename

    value = sanitize_filename(
        value, replacement_text="_", platform="universal", max_len=160, fs_encoding="utf-8"
    ).strip(" .")
    return value or fallback


def ensure_inside(directory: Path, target: Path) -> None:
    """Проверить путь, включая существующие символические ссылки."""
    if not target.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Путь загрузки выходит за пределы каталога назначения")


def build_target_path(directory: Path, context: DownloadContext, template: PathTemplate) -> Path:
    """Подставить метаданные в проверенный шаблон без добавления расширения.

    Отсутствующие номера и год заменяются на 0; названия — на Unknown artist,
    Unknown album и track. artist содержит первого исполнителя трека.
    """
    track, album = context.track, context.album
    artists = track.artists or []
    position = album.track_position
    values = {
        "artist": safe_name(artists[0].name or "", "Unknown artist")
        if artists
        else "Unknown artist",
        "album": safe_name(album.title or "", "Unknown album"),
        "title": safe_name(track.title or ""),
        "track_id": safe_name(str(track.id or "")),
        "album_id": safe_name(str(album.id or ""), "unknown"),
        "track_number": position.index or 0 if position else 0,
        "disc_number": position.volume or 0 if position else 0,
        "year": album.year or 0,
    }
    components = [safe_name(component.format_map(values)) for component in template.components]
    target = Path(directory).joinpath(*components)
    logger.debug("Путь по шаблону подготовлен: трек=%s", track.id)
    return target


def select_target_path(
    directory: Path, context: DownloadContext, template: PathTemplate, extension: str
) -> Path:
    """Добавить расширение и выбрать свободное имя либо файл того же трека.

    При конфликте используется суффикс [ID]. Если и это имя занято другим
    или неизвестным треком, выбрасывается FileExistsError.
    """
    from music_download.tags import read_track_id

    base = build_target_path(directory, context, template)
    track_id = str(context.track.id)
    suffix_id = safe_name(track_id).encode("utf-8")[:40].decode("utf-8", errors="ignore")
    for suffix in ("", f" [{suffix_id}]"):
        target = base.with_name(f"{base.name}{suffix}{extension}")
        ensure_inside(directory, target)
        if not target.exists() or read_track_id(target) == track_id:
            return target
    raise FileExistsError(f"Файл занят другим или неизвестным треком: {target}")
