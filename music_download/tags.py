"""Metadata mapping following ymd's set_tags conventions."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    USLT,
    WOAF,
    ID3NoHeaderError,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from yandex_music import Album, Track

from music_download.models import DownloadContext, DownloadOptions, TrackMetadata
from music_download.sources import Source

logger = logging.getLogger(__name__)


_EMPTY_METADATA = TrackMetadata()
_DEFAULT_OPTIONS = DownloadOptions()


def read_track_id(path: Path) -> str | None:
    """Прочитать ID Яндекс Музыки из ссылки WOAF в MP3.

    :param path: Путь существующего MP3-файла.
    :returns: ID трека либо None, если нет ID3-заголовка или подходящей ссылки.
    :raises OSError: Файл не удалось прочитать.
    :raises mutagen.MutagenError: ID3-теги повреждены и не могут быть разобраны.
    """
    logger.debug("Чтение ID трека из ID3")
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return None
    for frame in tags.getall("WOAF"):
        try:
            source = Source.from_url(frame.url)
        except ValueError:
            continue
        if source.track_id is not None:
            return source.track_id
    return None


def full_title(item: Track | Album) -> str:
    """Сформировать название с версией в скобках.

    :param item: Библиотечная модель трека или альбома.
    :returns: Название с версией, если она указана; иначе только название.
    """
    title = item.title or ""
    return f"{title} ({item.version})" if item.version else title


@dataclass(frozen=True)
class _TagValues:
    """Общие значения тегов до преобразования в формат контейнера."""

    title: str
    album: str
    artists: list[str]
    album_artists: list[str]
    date: str | None
    genre: str | None
    number: int | None
    disc: int | None
    url: str


def _release_date(album: Album) -> str | None:
    """Использовать дату релиза в UTC, а при её отсутствии — год."""
    if not album.release_date:
        return str(album.year) if album.year else None
    released = datetime.fromisoformat(album.release_date)
    if released.tzinfo is not None:
        released = released.astimezone(UTC)
    return released.strftime("%Y-%m-%d %H:%M:%S")


def track_url(context: DownloadContext) -> str:
    """Сформировать ссылку на трек с учётом выбранного издания."""
    # TODO: заменить методом yandex_music, когда библиотека начнёт возвращать
    # ссылку на страницу трека с учётом выбранного альбома (см. TODO.md).
    track, album = context.track, context.album
    if album.id:
        return f"https://music.yandex.ru/album/{album.id}/track/{track.id}"
    return f"https://music.yandex.ru/track/{track.id}"


def _tag_values(context: DownloadContext) -> _TagValues:
    """Подготовить общие музыкальные данные для любого контейнера."""
    track, album = context.track, context.album
    position = album.track_position
    return _TagValues(
        title=full_title(track),
        album=full_title(album),
        artists=[artist.name for artist in track.artists or [] if artist.name],
        album_artists=[artist.name for artist in album.artists or [] if artist.name],
        date=_release_date(album),
        genre=album.genre,
        number=position.index if position else None,
        disc=position.volume if position else None,
        url=track_url(context),
    )


def _cover_mime(cover: bytes | None) -> str | None:
    """Распознать JPEG/PNG или отклонить неизвестный формат обложки."""
    if cover is None:
        return None
    if cover.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if cover.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    raise ValueError("Обложка должна быть JPEG или PNG")


def _write_mp3(
    audio: MP3,
    values: _TagValues,
    metadata: TrackMetadata,
    mime: str | None,
) -> None:
    """Записать ID3-фреймы, текст и обложку MP3."""
    frames = [
        (TIT2, values.title),
        (TALB, values.album),
        (TPE1, values.artists),
        (TPE2, values.album_artists),
        (TDRC, values.date),
        (TCON, values.genre),
        (TRCK, str(values.number) if values.number else None),
        (TPOS, str(values.disc) if values.disc else None),
    ]
    for frame, value in frames:
        if value:
            audio.tags.add(frame(encoding=3, text=value))
    audio.tags.add(WOAF(url=values.url))
    if metadata.lyrics:
        audio.tags.add(USLT(encoding=3, text=metadata.lyrics))
    if metadata.cover is not None:
        audio.tags.delall("APIC")
        audio.tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=3,
                data=metadata.cover,
            )
        )


def _write_m4a(
    audio: MP4,
    values: _TagValues,
    metadata: TrackMetadata,
    mime: str | None,
) -> None:
    """Записать атомы метаданных M4A."""
    atoms = {
        "\xa9nam": values.title,
        "\xa9alb": values.album,
        "\xa9ART": "; ".join(values.artists),
        "aART": "; ".join(values.album_artists),
        "\xa9day": values.date,
        "\xa9gen": values.genre,
        "\xa9lyr": metadata.lyrics,
        "\xa9cmt": values.url,
    }
    for key, value in atoms.items():
        if value:
            audio[key] = [value]
    if values.number:
        audio["trkn"] = [(values.number, 0)]
    if values.disc:
        audio["disk"] = [(values.disc, 0)]
    if metadata.cover is not None:
        imageformat = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(metadata.cover, imageformat=imageformat)]


def _write_flac(
    audio: FLAC,
    values: _TagValues,
    metadata: TrackMetadata,
    mime: str | None,
) -> None:
    """Записать Vorbis Comments и обложку FLAC."""
    comments = {
        "title": values.title,
        "album": values.album,
        "artist": values.artists,
        "albumartist": values.album_artists,
        "date": values.date,
        "genre": values.genre,
        "tracknumber": str(values.number) if values.number else None,
        "discnumber": str(values.disc) if values.disc else None,
        "lyrics": metadata.lyrics,
        "comment": values.url,
    }
    for key, value in comments.items():
        if value:
            audio[key] = value
    if metadata.cover is not None:
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.data = metadata.cover
        audio.clear_pictures()
        audio.add_picture(picture)


def write_tags(
    path: Path,
    context: DownloadContext,
    *,
    metadata: TrackMetadata = _EMPTY_METADATA,
    options: DownloadOptions = _DEFAULT_OPTIONS,
) -> None:
    """Записать теги в существующий аудиофайл выбранного контейнера.

    :param path: Путь аудиофайла, изменяемого на месте.
    :param context: Трек и выбранный альбом.
    :param metadata: Обложка JPEG/PNG и текст песни.
    :param options: Настройки с контейнером тегов: mp3, m4a или flac.
    :raises ValueError: Контейнер, формат обложки или дата некорректны.
    :raises OSError: Ошибка чтения или записи файла.
    :raises mutagen.MutagenError: Ошибка разбора аудиофайла или записи тегов.
    """
    logger.debug("Запись тегов: трек=%s, контейнер=%s", context.track.id, options.container)
    handlers = {
        "mp3": (MP3, _write_mp3),
        "m4a": (MP4, _write_m4a),
        "flac": (FLAC, _write_flac),
    }
    if options.container not in handlers:
        raise ValueError(f"Контейнер тегов не поддерживается: {options.container}")
    factory, writer = handlers[options.container]
    values = _tag_values(context)
    mime = _cover_mime(metadata.cover)
    audio = factory(path)
    if audio.tags is None:
        audio.add_tags()
    writer(audio, values, metadata, mime)
    audio.save()
    logger.debug("Теги трека %s записаны", context.track.id)
