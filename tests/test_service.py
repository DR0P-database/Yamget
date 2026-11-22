from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mutagen.id3 import ID3, WOAF
from yandex_music import Album

from music_download.engine import DownloadCancelled
from music_download.models import DownloadContext, DownloadOptions, TrackMetadata
from music_download.service import DownloadService
from music_download.tags import read_track_id


def track():
    return SimpleNamespace(albums=[], id="1", title="Track", artists=[], cover_uri=None, lyrics_info=None,
        get_download_info=lambda: [SimpleNamespace(codec="mp3", bitrate_in_kbps=320,
                                                   get_direct_link=lambda: "https://host/audio")])


@pytest.mark.parametrize("desired, expected", [(128, 128), (192, 320), (None, 320)])
def test_selects_desired_bitrate_or_best_mp3(tmp_path, monkeypatch, desired, expected):
    item = track()
    item.get_download_info = lambda: [
        SimpleNamespace(codec=codec, bitrate_in_kbps=rate,
                        get_direct_link=lambda rate=rate: f"https://host/{rate}")
        for codec, rate in [("mp3", 128), ("mp3", 320), ("aac", 512)]
    ]
    engine = Mock()
    engine.download.side_effect = lambda url, path, **kwargs: path.write_bytes(b"audio")
    tags = Mock()
    monkeypatch.setattr("music_download.service.write_tags", tags)
    result = DownloadService(Mock(), engine).download(DownloadContext.from_track(item), tmp_path, DownloadOptions(bitrate=desired))
    assert engine.download.call_args.args[0] == f"https://host/{expected}"
    assert result.path.suffix == ".mp3"
    assert tags.call_args.kwargs["options"].container == "mp3"


def test_missing_mp3_does_not_start_transfer(tmp_path):
    item = track()
    item.get_download_info = lambda: [SimpleNamespace(codec="aac", bitrate_in_kbps=256)]
    engine = Mock()
    with pytest.raises(ValueError, match="нет MP3"):
        DownloadService(Mock(), engine).download(DownloadContext.from_track(item), tmp_path, DownloadOptions(bitrate=320))
    engine.download.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_selected_album_reaches_tags(tmp_path, monkeypatch):
    engine = Mock()
    engine.download.side_effect = lambda url, path, **kwargs: path.write_bytes(b"audio")
    tags = Mock()
    monkeypatch.setattr("music_download.service.write_tags", tags)
    context = DownloadContext(track(), Album(id=2))
    DownloadService(Mock(), engine).download(context, tmp_path)
    assert tags.call_args.args[1] is context


def test_fetched_metadata_reaches_tags(tmp_path, monkeypatch):
    item = track()
    item.cover_uri = "cover"
    item.download_cover_bytes = Mock(return_value=b"cover")
    item.lyrics_info = SimpleNamespace(has_available_text_lyrics=True)
    item.get_lyrics = Mock(return_value=SimpleNamespace(fetch_lyrics=lambda: "Lyrics"))
    engine = Mock()
    engine.download.side_effect = lambda url, path, **kwargs: path.write_bytes(b"audio")
    tags = Mock()
    monkeypatch.setattr("music_download.service.write_tags", tags)
    context = DownloadContext.from_track(item)
    DownloadService(Mock(), engine).download(context, tmp_path)
    assert tags.call_args.kwargs["metadata"] == TrackMetadata(cover=b"cover", lyrics="Lyrics")
    assert tags.call_args.kwargs["options"] == DownloadOptions()


@pytest.mark.parametrize("container", ["m4a", "flac"])
def test_unsupported_download_container_fails_before_transfer(tmp_path, container):
    engine = Mock()
    context = DownloadContext.from_track(track())
    with pytest.raises(ValueError, match="Контейнер скачивания не поддерживается"):
        DownloadService(Mock(), engine).download(context, tmp_path, DownloadOptions(container=container))
    engine.download.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("existing_id, expected", [("1", "Track.mp3"),
                                                   ("2", "Track [1].mp3"),
                                                   (None, "Track [1].mp3")])
def test_filename_collision_uses_metadata(tmp_path, monkeypatch, existing_id, expected):
    original = tmp_path / "Track.mp3"
    if existing_id is None:
        original.write_bytes(b"unknown")
    else:
        tags = ID3()
        tags.add(WOAF(url=f"https://music.yandex.ru/album/9/track/{existing_id}"))
        tags.save(original)
    before = original.read_bytes()
    engine = Mock()
    engine.download.side_effect = lambda url, path, **kwargs: path.write_bytes(b"audio")
    monkeypatch.setattr("music_download.service.write_tags", Mock())
    service = DownloadService(Mock(), engine)
    result = service.download(DownloadContext.from_track(track()), tmp_path, DownloadOptions(overwrite=True))
    assert result.path.name == expected
    if existing_id != "1":
        assert original.read_bytes() == before
    else:
        assert result.path.read_bytes() == b"audio"


def test_same_track_is_skipped_without_network(tmp_path):
    target = tmp_path / "Track.mp3"
    tags = ID3()
    tags.add(WOAF(url="https://music.yandex.ru/track/1"))
    tags.save(target)
    assert read_track_id(target) == "1"
    engine = Mock()
    result = DownloadService(Mock(), engine).download(DownloadContext.from_track(track()), tmp_path)
    assert result.skipped
    engine.download.assert_not_called()


def test_collision_with_existing_id_filename_is_not_overwritten(tmp_path):
    (tmp_path / "Track.mp3").write_bytes(b"unknown")
    (tmp_path / "Track [1].mp3").write_bytes(b"another")
    with pytest.raises(FileExistsError):
        DownloadService(Mock(), Mock()).download(DownloadContext.from_track(track()), tmp_path, DownloadOptions(overwrite=True))
    assert (tmp_path / "Track [1].mp3").read_bytes() == b"another"


def test_publish_after_transfer_and_tags(tmp_path, monkeypatch):
    engine = Mock()
    order = []

    def download(url, temporary, **kwargs):
        order.append("transfer")
        temporary.write_bytes(b"audio")

    def tags(temporary, *args, **kwargs):
        assert temporary.read_bytes() == b"audio"
        assert len(list(tmp_path.iterdir())) == 1
        order.append("tags")

    engine.download.side_effect = download
    monkeypatch.setattr("music_download.service.write_tags", tags)
    result = DownloadService(Mock(), engine).download(DownloadContext.from_track(track()), tmp_path)
    assert order == ["transfer", "tags"]
    assert result.path.read_bytes() == b"audio"
    assert result.path.name == "Track.mp3"
    assert list(tmp_path.iterdir()) == [result.path]


def test_cancel_before_publication_preserves_existing_file(tmp_path, monkeypatch):
    engine = Mock()
    state = {"cancelled": False}
    target = tmp_path / "Track.mp3"
    target.write_bytes(b"original")
    monkeypatch.setattr("music_download.tags.read_track_id", lambda path: "1")

    def download(url, temporary, **kwargs):
        temporary.write_bytes(b"replacement")
        Path(str(temporary) + ".aria2").write_bytes(b"control")

    def tags(*args, **kwargs):
        state["cancelled"] = True

    def check_cancelled():
        if state["cancelled"]:
            raise DownloadCancelled()

    engine.download.side_effect = download
    monkeypatch.setattr("music_download.service.write_tags", tags)
    with pytest.raises(DownloadCancelled):
        DownloadService(Mock(), engine).download(DownloadContext.from_track(track()), tmp_path, DownloadOptions(overwrite=True),
                                                check_cancelled=check_cancelled)
    assert list(tmp_path.iterdir()) == [target]
    assert target.read_bytes() == b"original"
