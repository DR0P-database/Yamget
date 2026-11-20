"""Поддерживаемые форматы скачивания и выбор качества аудио."""

import logging
from dataclasses import dataclass

from yandex_music import DownloadInfo, Track

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadFormat:
    """Кодек источника и расширение итогового файла без перекодирования."""

    codec: str
    extension: str


_FORMATS = {"mp3": DownloadFormat(codec="mp3", extension=".mp3")}


def get_download_format(container: str) -> DownloadFormat:
    """Проверить поддержку контейнера до обращения к API или файловой системе."""
    logger.debug("Проверка формата: %s", container)
    try:
        return _FORMATS[container]
    except KeyError:
        raise ValueError(f"Контейнер скачивания не поддерживается: {container}") from None


def select_download_info(
    track: Track,
    audio_format: DownloadFormat,
    bitrate: int | None,
) -> DownloadInfo:
    """Выбрать желаемый битрейт кодека, иначе максимальный доступный."""
    choices = [info for info in track.get_download_info() if info.codec == audio_format.codec]
    if not choices:
        raise ValueError(f"Для трека {track.id} нет {audio_format.codec.upper()}")
    selected = next(
        (item for item in choices if item.bitrate_in_kbps == bitrate),
        max(choices, key=lambda item: item.bitrate_in_kbps),
    )
    logger.debug(
        "Выбран формат: трек=%s, кодек=%s, битрейт=%s",
        track.id,
        selected.codec,
        selected.bitrate_in_kbps,
    )
    return selected
