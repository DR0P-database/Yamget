import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, TypeVar

from yandex_music import Album, Client, Playlist, Track
from yandex_music.exceptions import YandexMusicError

from music_download.sources import Source

logger = logging.getLogger(__name__)


Model = TypeVar("Model", Track, Album, Playlist)


class ResourceError(ValueError):
    """Контролируемая причина отсутствия объекта без содержимого ответа API."""

    def __init__(self, label: str, reason: Literal["не найден", "недоступен"]):
        self.reason = reason
        super().__init__(f"{label} {reason}")


@dataclass(frozen=True)
class PlaylistEntry:
    index: int
    track_id: str
    track: Track | None = None
    error: Exception | None = None


class YandexProvider:
    """Тонкий провайдер над моделями и методами yandex-music."""

    def __init__(self, client: Client):
        self.client = client

    @classmethod
    def from_token(cls, token: str) -> "YandexProvider":
        logger.debug("Авторизация в Яндекс Музыке")
        provider = cls(Client(token).init())
        logger.info("Авторизация в Яндекс Музыке выполнена")
        return provider

    @staticmethod
    def _require_available(value: object, model: type[Model], label: str) -> Model:
        """Проверить тип и доступность объекта, возвращённого API."""
        if not isinstance(value, model):
            raise ResourceError(label, "не найден")
        if value.available is False:
            raise ResourceError(label, "недоступен")
        return value

    def track(self, track_id: str | Source) -> Track:
        source = track_id if isinstance(track_id, Source) else Source.parse(track_id)
        match source.kind:
            case "track" | "playlist_track":
                identifier = source.track_id
            case "album_track":
                identifier = f"{source.track_id}:{source.album_id}"
            case _:
                raise ValueError("Ожидается ID трека или ссылка на трек")
        logger.debug("Получение трека %s", identifier)
        tracks = self.client.tracks([identifier])
        return self._require_available(tracks[0] if tracks else None, Track, f"Трек {identifier}")

    def album_tracks(self, album_id: str) -> list[Track]:
        """Возвращает треки альбома в порядке дисков и позиций."""
        logger.debug("Получение треков альбома %s", album_id)
        album = self._require_available(self.client.albums_with_tracks(album_id), Album, "Альбом")
        if album.volumes is None:
            raise ValueError("API не вернул треки альбома")
        return [track for volume in album.volumes for track in volume]

    def playlist(self, user: str, kind: int) -> Playlist:
        logger.debug("Получение плейлиста по владельцу и ID")
        return self._require_available(
            self.client.users_playlists(kind, user_id=user), Playlist, "Плейлист"
        )

    def playlist_by_key(self, playlist_key: str) -> Playlist:
        logger.debug("Получение плейлиста по ключу")
        return self._require_available(self.client.playlist(playlist_key), Playlist, "Плейлист")

    def resolve_source(self, source: Source | str) -> Iterator[PlaylistEntry]:
        """Разрешить источник по kind, сохранив порядок и ошибки плейлиста.

        Args:
            source: Разобранный Source или строка для Source.parse(). Для
                track, album_track и playlist_track возвращается один трек;
                для album и playlist — все записи в исходном порядке.

        Ошибки отдельных записей плейлиста возвращаются в PlaylistEntry.error.
        Ошибки получения самого источника выбрасываются вызывающему коду.
        """
        if isinstance(source, str):
            source = Source.parse(source)
        logger.debug("Получение записей источника: тип=%s", source.kind)
        match source.kind:
            case "track" | "album_track" | "playlist_track":
                tracks = (self.track(source),)
            case "album":
                tracks = self.album_tracks(source.album_id)
            case "playlist":
                if source.playlist_key is not None:
                    playlist = self.playlist_by_key(source.playlist_key)
                else:
                    playlist = self.playlist(source.user_id, source.playlist_id)
                yield from self.resolve_playlist(playlist)
                return

        yield from (
            PlaylistEntry(index, str(track.id), track=track) for index, track in enumerate(tracks)
        )

    def resolve_playlist(self, playlist: Playlist) -> Iterator[PlaylistEntry]:
        """Сохраняет порядок и ошибки отдельных записей вместо их потери."""
        if playlist.tracks is not None:
            items = playlist.tracks
        elif playlist.playlist_uuid:
            # У плейлиста с ключом может не быть владельца/kind для fetch_tracks().
            refreshed = self.playlist_by_key(playlist.playlist_uuid)
            if refreshed.tracks is None:
                raise ValueError("API не вернул список треков плейлиста")
            items = refreshed.tracks
        else:
            items = playlist.fetch_tracks()
        for index, item in enumerate(items or []):
            identifier = str(item.id)
            try:
                track = item if isinstance(item, Track) else item.track
                if track is None:
                    track = self.track(item.track_id)
                else:
                    track = self._require_available(track, Track, f"Трек {identifier}")
            except (YandexMusicError, ValueError) as error:
                logger.debug("Не удалось получить трек %s: %s", identifier, type(error).__name__)
                yield PlaylistEntry(index, identifier, error=error)
            else:
                yield PlaylistEntry(index, identifier, track=track)
