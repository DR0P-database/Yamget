import logging
from dataclasses import dataclass
from pathlib import Path

from yandex_music import Album, Track

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadContext:
    """Трек и выбранное издание без настроек и состояния скачивания."""

    track: Track
    album: Album

    @classmethod
    def from_track(
        cls,
        track: Track,
        album_id: str | None = None,
    ) -> "DownloadContext":
        """Выбрать альбом источника или первый альбом для отдельного трека."""
        logger.debug("Выбор альбома: трек=%s, альбом=%s", track.id, album_id)
        albums = track.albums or []
        if album_id is None:
            return cls(track, albums[0] if albums else Album())
        album = next((item for item in albums if str(item.id) == str(album_id)), None)
        if album is None:
            raise ValueError(f"У трека {track.id} нет альбома {album_id}")
        return cls(track, album)


@dataclass(frozen=True)
class TrackMetadata:
    """Обложка и текст, полученные перед записью тегов."""

    cover: bytes | None = None
    lyrics: str | None = None


@dataclass(frozen=True)
class DownloadOptions:
    """Настройки скачивания, независимые от состояния задания."""

    overwrite: bool = False
    container: str = "mp3"
    bitrate: int | None = None


@dataclass(frozen=True)
class DownloadResult:
    track: Track
    path: Path
    skipped: bool = False
