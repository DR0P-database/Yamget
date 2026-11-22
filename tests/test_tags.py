from unittest.mock import MagicMock, Mock

import pytest
from mutagen.id3 import ID3
from yandex_music import Album, Track, TrackPosition

from music_download.models import DownloadContext, DownloadOptions, TrackMetadata
from music_download.tags import write_tags


@pytest.mark.parametrize("album_id, expected", [("2", "Selected"), (None, "First")])
def test_album_metadata_follows_source(tmp_path, monkeypatch, album_id, expected):
    track = Track(id="123", albums=[
        Album(id=1, title="First", year=2001),
        Album(id=2, title="Selected", year=2020),
    ])
    audio = Mock(tags=ID3())
    monkeypatch.setattr("music_download.tags.MP3", Mock(return_value=audio))
    write_tags(tmp_path / "track.mp3", DownloadContext.from_track(track, album_id))
    assert str(audio.tags["TALB"]) == expected
    assert str(audio.tags["TDRC"]) == ("2020" if album_id else "2001")
    assert audio.tags.getall("WOAF")[0].url == (
        f"https://music.yandex.ru/album/{album_id or '1'}/track/123"
    )
    assert [album.id for album in track.albums] == [1, 2]


def test_missing_requested_album_is_rejected_when_creating_context():
    with pytest.raises(ValueError, match="нет альбома 2"):
        DownloadContext.from_track(Track(id="123", albums=[Album(id=1)]), album_id="2")


def test_track_without_album_keeps_track_link(tmp_path, monkeypatch):
    audio = Mock(tags=ID3())
    monkeypatch.setattr("music_download.tags.MP3", Mock(return_value=audio))
    context = DownloadContext.from_track(Track(id="123", albums=[]))
    write_tags(tmp_path / "track.mp3", context)
    assert "TALB" not in audio.tags
    assert audio.tags.getall("WOAF")[0].url == "https://music.yandex.ru/track/123"


@pytest.mark.parametrize("container, factory", [("mp3", "MP3"), ("m4a", "MP4"), ("flac", "FLAC")])
def test_cover_and_lyrics_from_metadata(tmp_path, monkeypatch, container, factory):
    audio = MagicMock(tags=ID3() if container == "mp3" else {})
    loader = Mock(return_value=audio)
    monkeypatch.setattr(f"music_download.tags.{factory}", loader)
    context = DownloadContext.from_track(Track(id="123"))
    metadata = TrackMetadata(cover=b"\x89PNG\r\n\x1a\nimage", lyrics="Lyrics")
    path = tmp_path / f"track.{container}"
    write_tags(path, context, metadata=metadata, options=DownloadOptions(container=container))
    loader.assert_called_once_with(path)
    audio.save.assert_called_once()
    if container == "mp3":
        assert audio.tags.getall("APIC")[0].data == metadata.cover
        assert audio.tags.getall("USLT")[0].text == metadata.lyrics
    elif container == "m4a":
        values = dict(call.args for call in audio.__setitem__.call_args_list)
        assert values["\xa9lyr"] == [metadata.lyrics]
        assert bytes(values["covr"][0]) == metadata.cover
    else:
        audio.__setitem__.assert_any_call("lyrics", metadata.lyrics)
        assert audio.add_picture.call_args.args[0].data == metadata.cover


def test_release_date_and_disc_position(tmp_path, monkeypatch):
    audio = Mock(tags=ID3())
    monkeypatch.setattr("music_download.tags.MP3", Mock(return_value=audio))
    album = Album(
        id=42, year=2000, release_date="2024-02-03T01:30:00+03:00",
        track_position=TrackPosition(index=7, volume=2),
    )
    write_tags(tmp_path / "track.mp3", DownloadContext(Track(id="123"), album))
    assert str(audio.tags["TDRC"]) == "2024-02-02 22:30:00"
    assert str(audio.tags["TRCK"]) == "7"
    assert str(audio.tags["TPOS"]) == "2"


@pytest.mark.parametrize("container", ["mp3", "m4a", "flac"])
def test_invalid_cover_does_not_save(tmp_path, monkeypatch, container):
    audio = MagicMock()
    for factory in ("MP3", "MP4", "FLAC"):
        monkeypatch.setattr(f"music_download.tags.{factory}", Mock(return_value=audio))
    with pytest.raises(ValueError, match="Обложка должна быть JPEG или PNG"):
        write_tags(
            tmp_path / "track", DownloadContext.from_track(Track(id="123")),
            metadata=TrackMetadata(cover=b"invalid"),
            options=DownloadOptions(container=container),
        )
    audio.save.assert_not_called()


def test_unsupported_tag_container(tmp_path):
    with pytest.raises(ValueError, match="Контейнер тегов не поддерживается"):
        write_tags(
            tmp_path / "missing", DownloadContext.from_track(Track(id="123")),
            options=DownloadOptions(container="wav"),
        )
