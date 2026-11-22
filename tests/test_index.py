import sqlite3

import pytest
from mutagen.id3 import ID3, WOAF

from music_download.index import LibraryIndex
from music_download.tags import read_track_source


def tagged_file(path, track_id="1", album_id="42"):
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = ID3()
    url = (
        f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
        if album_id is not None
        else f"https://music.yandex.ru/track/{track_id}"
    )
    tags.add(WOAF(url=url))
    tags.save(path)
    return path


def test_index_is_in_memory_and_closed_with_context(tmp_path):
    tagged_file(tmp_path / "old/song.mp3")
    before = set(tmp_path.rglob("*"))
    with LibraryIndex(tmp_path) as index:
        assert index.scan() == 1
        assert index.find("1", "42").path == tmp_path / "old/song.mp3"
        assert index._connection.execute("PRAGMA database_list").fetchone()["file"] == ""
    assert set(tmp_path.rglob("*")) == before
    with pytest.raises(sqlite3.ProgrammingError):
        index.find("1", "42")
    with LibraryIndex(tmp_path) as index:
        assert index.find("1", "42") is None


def test_scan_skips_unknown_empty_temporary_and_symlink_files(tmp_path):
    tagged_file(tmp_path / "good.MP3")
    (tmp_path / "empty.mp3").touch()
    (tmp_path / "unknown.mp3").write_bytes(b"audio without id")
    tagged_file(tmp_path / "audio.part", "2")
    (tmp_path / "link.mp3").symlink_to(tmp_path / "good.MP3")
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    with LibraryIndex(tmp_path) as index:
        assert index.scan() == 1
        assert index.find("2", "42") is None


def test_track_and_album_are_both_part_of_identity(tmp_path):
    tagged_file(tmp_path / "one.mp3", album_id="42")
    tagged_file(tmp_path / "two.mp3", album_id="99")
    tagged_file(tmp_path / "unknown-album.mp3", album_id=None)
    with LibraryIndex(tmp_path) as index:
        assert index.scan() == 3
        assert index.find("1", "42").path.name == "one.mp3"
        assert index.find("1", "99").path.name == "two.mp3"
        assert index.find("1", None).path.name == "unknown-album.mp3"
        assert index.find("1", "100") is None


@pytest.mark.parametrize("change", ["delete", "modify", "symlink"])
def test_find_does_not_return_stale_file(tmp_path, change):
    path = tagged_file(tmp_path / "one.mp3")
    with LibraryIndex(tmp_path) as index:
        index.scan()
        if change == "modify":
            path.write_bytes(b"changed")
        else:
            path.unlink()
            if change == "symlink":
                other = tagged_file(tmp_path / "other.mp3", "2")
                path.symlink_to(other)
        assert index.find("1", "42") is None


def test_duplicates_are_retained_and_lookup_is_deterministic(tmp_path):
    tagged_file(tmp_path / "b.mp3")
    tagged_file(tmp_path / "a.mp3")
    with LibraryIndex(tmp_path) as index:
        assert index.scan() == 2
        assert index.find("1", "42").path.name == "a.mp3"
        (tmp_path / "a.mp3").unlink()
        assert index.find("1", "42").path.name == "b.mp3"


def test_missing_root_is_not_created(tmp_path):
    root = tmp_path / "missing"
    with LibraryIndex(root) as index:
        assert index.scan() == 0
    assert not root.exists()


def test_scan_failure_rolls_back_index(tmp_path, monkeypatch):
    tagged_file(tmp_path / "a.mp3")
    with LibraryIndex(tmp_path) as index:
        index.scan()

        def walk(*args, **kwargs):
            yield str(tmp_path), [], ["a.mp3"]
            kwargs["onerror"](PermissionError("unreadable directory"))

        monkeypatch.setattr("music_download.index.os.walk", walk)
        with pytest.raises(PermissionError):
            index.scan()
        assert index.find("1", "42") is not None


def test_read_source_retains_selected_album(tmp_path):
    source = read_track_source(tagged_file(tmp_path / "track.mp3"))
    assert (source.track_id, source.album_id) == ("1", "42")
