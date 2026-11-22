import os
import sqlite3
from unittest.mock import Mock

import pytest
from mutagen.id3 import ID3, WOAF
from yandex_music import Album, Track

from music_download.engine import DownloadCancelled
from music_download.index import LibraryIndex
from music_download.models import DownloadContext, DownloadOptions
from music_download.relocation import RelocationError, relocate_file
from music_download.service import DownloadService


def setup_file(tmp_path):
    source = tmp_path / "old/song.mp3"
    source.parent.mkdir()
    tags = ID3()
    tags.add(WOAF(url="https://music.yandex.ru/album/42/track/1"))
    tags.save(source)
    context = DownloadContext(Track(id="1", title="Song", artists=[]), Album(id=42, title="Album"))
    return source, context


def test_service_moves_requested_track_without_download_or_retagging(tmp_path, monkeypatch):
    source, context = setup_file(tmp_path)
    unrelated = tmp_path / "unrelated.mp3"
    unrelated.write_bytes(b"untouched")
    original = source.read_bytes()
    engine = Mock()
    tags = Mock()
    monkeypatch.setattr("music_download.service.write_tags", tags)
    with LibraryIndex(tmp_path) as index:
        index.scan()
        service = DownloadService(Mock(), engine, index=index)
        result = service.download(
            context, tmp_path, DownloadOptions(path_template="{album}/{title}")
        )
        assert result.relocated and not result.skipped
        assert result.path == tmp_path / "Album/Song.mp3"
        assert index.find("1", "42").path == result.path
        assert not source.exists()
        assert result.path.read_bytes() == original
        again = service.download(
            context, tmp_path, DownloadOptions(path_template="{album}/{title}")
        )
        assert again.skipped and not again.relocated
    engine.download.assert_not_called()
    tags.assert_not_called()
    assert unrelated.read_bytes() == b"untouched"


@pytest.mark.parametrize("failure", [DownloadCancelled, KeyboardInterrupt, OSError])
def test_failure_after_source_unlink_rolls_back(tmp_path, failure):
    source, _ = setup_file(tmp_path)
    original = source.read_bytes()
    target = tmp_path / "new/song.mp3"

    def check():
        if not source.exists():
            raise failure()

    with LibraryIndex(tmp_path) as index:
        index.scan()
        with pytest.raises(failure):
            relocate_file(index.find("1", "42"), target, index, check)
        assert index.find("1", "42").path == source
    assert source.read_bytes() == original
    assert not target.exists()


def test_index_update_failure_rolls_back_file(tmp_path, monkeypatch):
    source, _ = setup_file(tmp_path)
    target = tmp_path / "new/song.mp3"
    with LibraryIndex(tmp_path) as index:
        index.scan()
        monkeypatch.setattr(index, "move", Mock(side_effect=sqlite3.OperationalError("failed")))
        with pytest.raises(sqlite3.OperationalError):
            relocate_file(index.find("1", "42"), target, index)
        assert index.find("1", "42").path == source
    assert source.exists() and not target.exists()


def test_existing_destination_is_never_overwritten(tmp_path):
    source, _ = setup_file(tmp_path)
    target = tmp_path / "occupied.mp3"
    target.write_bytes(b"other")
    with LibraryIndex(tmp_path) as index:
        index.scan()
        with pytest.raises(FileExistsError):
            relocate_file(index.find("1", "42"), target, index)
    assert target.read_bytes() == b"other"
    assert source.exists()


def test_rollback_does_not_overwrite_recreated_source(tmp_path, caplog):
    source, _ = setup_file(tmp_path)
    original = source.read_bytes()
    target = tmp_path / "new/song.mp3"

    def check():
        if not source.exists():
            source.write_bytes(b"someone else's file")
            raise DownloadCancelled()

    with LibraryIndex(tmp_path) as index:
        index.scan()
        with pytest.raises(RelocationError, match="откатить"):
            relocate_file(index.find("1", "42"), target, index, check)
    assert target.read_bytes() == original
    assert source.read_bytes() == b"someone else's file"
    assert str(source) in caplog.text and str(target) in caplog.text


def test_changed_source_is_not_moved(tmp_path):
    source, _ = setup_file(tmp_path)
    with LibraryIndex(tmp_path) as index:
        index.scan()
        item = index.find("1", "42")
        source.write_bytes(b"changed")
        with pytest.raises(RelocationError, match="изменился"):
            relocate_file(item, tmp_path / "new.mp3", index)
    assert source.read_bytes() == b"changed"


def test_cross_device_link_failure_leaves_source_intact(tmp_path, monkeypatch):
    source, _ = setup_file(tmp_path)
    with LibraryIndex(tmp_path) as index:
        index.scan()
        monkeypatch.setattr(os, "link", Mock(side_effect=OSError("cross-device")))
        with pytest.raises(OSError, match="cross-device"):
            relocate_file(index.find("1", "42"), tmp_path / "new.mp3", index)
    assert source.exists()
    assert not (tmp_path / "new.mp3").exists()


def test_overwrite_bypasses_relocation(tmp_path, monkeypatch):
    source, context = setup_file(tmp_path)
    original = source.read_bytes()
    engine = Mock()
    engine.download.side_effect = lambda url, path, **kwargs: path.write_bytes(b"downloaded")
    info = Mock(codec="mp3", bitrate_in_kbps=320)
    monkeypatch.setattr(Track, "get_download_info", lambda self: [info])
    monkeypatch.setattr("music_download.service.write_tags", Mock())
    with LibraryIndex(tmp_path) as index:
        index.scan()
        service = DownloadService(Mock(), engine, index=index)
        monkeypatch.setattr(service, "_fetch_metadata", Mock())
        result = service.download(context, tmp_path, DownloadOptions(overwrite=True))
        assert not result.relocated
        assert result.path.read_bytes() == b"downloaded"
    engine.download.assert_called_once()
    assert source.read_bytes() == original


def test_error_after_index_commit_restores_original_record(tmp_path, monkeypatch):
    source, _ = setup_file(tmp_path)
    target = tmp_path / "new/song.mp3"
    with LibraryIndex(tmp_path) as index:
        index.scan()
        move = index.move

        def fail_after_update(item, target):
            move(item, target)
            raise OSError("interrupted after commit")

        monkeypatch.setattr(index, "move", fail_after_update)
        with pytest.raises(OSError, match="after commit"):
            relocate_file(index.find("1", "42"), target, index)
        assert index.find("1", "42").path == source
        assert index._connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    assert source.exists() and not target.exists()


def test_new_download_is_available_to_later_request_in_same_command(tmp_path, monkeypatch):
    context = DownloadContext(Track(id="1", title="Song", artists=[]), Album(id=42, title="Album"))
    info = Mock(codec="mp3", bitrate_in_kbps=320)
    monkeypatch.setattr(Track, "get_download_info", lambda self: [info])
    monkeypatch.setattr("music_download.service.write_tags", Mock())
    engine = Mock()
    engine.download.side_effect = lambda url, path, **kwargs: path.write_bytes(b"audio")
    with LibraryIndex(tmp_path) as index:
        index.scan()
        service = DownloadService(Mock(), engine, index=index)
        monkeypatch.setattr(service, "_fetch_metadata", Mock())
        first = service.download(context, tmp_path)
        second = service.download(
            context, tmp_path, DownloadOptions(path_template="{album}/{title}")
        )
        assert second.relocated
        assert not first.path.exists()
        assert second.path.read_bytes() == b"audio"
        assert index.find("1", "42").path == second.path
    engine.download.assert_called_once()
