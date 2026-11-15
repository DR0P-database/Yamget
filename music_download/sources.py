from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, unquote, urlsplit

DOMAINS = frozenset({
    "music.yandex.ru", "music.yandex.com", "music.yandex.kz", "music.yandex.by",
})


@dataclass(frozen=True, slots=True)
class Source:
    """Трек, альбом или плейлист с отдельными идентификаторами"""

    playlist_id: int | None = None
    album_id: str | None = None
    track_id: str | None = None
    user_id: str | None = None
    playlist_key: str | None = None

    @property
    def kind(self) -> Literal["track", "album_track", "playlist_track", "playlist", "album"]:
        if self.track_id is not None:
            if self.album_id is not None:
                return "album_track"
            if self.playlist_id is not None or self.playlist_key is not None:
                return "playlist_track"
            return "track"
        if self.album_id is not None:
            return "album"
        if self.playlist_id is not None or self.playlist_key is not None:
            return "playlist"
        raise ValueError("Источник не содержит трека, альбома или плейлиста")

    @classmethod
    def from_url(cls, value: str) -> Source:
        """Извлекает ID из пути; параметры запроса и фрагмент игнорируются."""
        url = cls._parse_url(value.strip())
        path = url.path.removeprefix("/").removesuffix("/")
        parts = [unquote(part) for part in path.split("/")]
        if not all(parts):
            raise ValueError("В ссылке отсутствует идентификатор")

        match parts:
            case ["album", album_id]:
                return cls(album_id=album_id)
            case ["album", album_id, "track", track_id]:
                return cls(album_id=album_id, track_id=track_id)
            case ["track", track_id]:
                return cls(track_id=track_id)
            case ["playlist" | "playlists", playlist_key]:
                return cls(playlist_key=playlist_key)
            case ["playlist" | "playlists", playlist_key, "track", track_id]:
                return cls(playlist_key=playlist_key, track_id=track_id)
            case ["users", user_id, "playlist" | "playlists", playlist_id]:
                return cls(user_id=user_id, playlist_id=int(playlist_id))
            case ["users", user_id, "playlists", playlist_id, "visibility"]:
                return cls(user_id=user_id, playlist_id=int(playlist_id))
            case ["users", user_id, "playlists", playlist_id, "track", track_id]:
                return cls(user_id=user_id, playlist_id=int(playlist_id), track_id=track_id)
        raise ValueError("Не удалось распознать ссылку на трек, альбом или плейлист")

    @classmethod
    def parse(cls, value: str) -> Source:
        """Принимает ссылку, ID трека, «трек:альбом» или ключ плейлиста."""
        value = value.strip()
        if re.fullmatch(r"[0-9]+(?::[0-9]+)?", value):
            track_id, _, album_id = value.partition(":")
            return cls(track_id=track_id, album_id=album_id or None)
        if "/" not in value and ":" not in value and "-" in value:
            return cls(playlist_key=value)
        return cls.from_url(value)

    @staticmethod
    def _parse_url(value: str) -> SplitResult:
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError:
            raise ValueError("Некорректная ссылка Яндекс Музыки") from None

        if not url.scheme and not url.netloc and value.startswith("/users/"):
            return url
        if (url.scheme != "https" or url.hostname not in DOMAINS
                or url.username is not None or url.password is not None
                or port not in (None, 443)):
            raise ValueError("Укажите HTTPS-ссылку Яндекс Музыки")
        return url


def positive_id(value: str) -> bool:
    return re.fullmatch(r"[0-9]+", value) is not None and int(value) > 0


# Совместимость с существующими вызовами.
def parse_source(value: str) -> Source:
    return Source.parse(value)
