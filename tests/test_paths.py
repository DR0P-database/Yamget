from pathlib import Path
from unittest.mock import Mock

import pytest
from yandex_music import Album, Artist, Track

from downloader_app.arguments import parse_options
from music_download.models import DownloadContext, DownloadOptions
from music_download.paths import PathTemplate, build_target_path, select_target_path
from music_download.service import DownloadService


def context():
    from yandex_music import TrackPosition

    album = Album(id=42, title="Album", year=2024, track_position=TrackPosition(index=3, volume=1))
    track = Track(id="123", title="Song", artists=[Artist(id=1, name="Artist")], albums=[album])
    return DownloadContext(track, album)


def test_default_path_preserves_flat_layout(tmp_path):
    assert build_target_path(tmp_path, context(), PathTemplate("{title}")) == tmp_path / "Song"


def test_metadata_and_number_formatting(tmp_path):
    target = build_target_path(
        tmp_path,
        context(),
        PathTemplate(
            "{artist}/{year:04d} - {album}/{disc_number}-{track_number:02d} - {title} [{track_id}]"
        ),
    )
    assert target.relative_to(tmp_path) == Path("Artist/2024 - Album/1-03 - Song [123]")


def test_missing_metadata_has_stable_defaults(tmp_path):
    item = DownloadContext(Track(id="1"), Album())
    target = build_target_path(
        tmp_path,
        item,
        PathTemplate("{artist}/{album}/{year}-{disc_number}-{track_number:02d}-{title}"),
    )
    assert target.relative_to(tmp_path) == Path("Unknown artist/Unknown album/0-0-00-track")


def test_metadata_cannot_add_directories(tmp_path):
    item = context()
    item.track.artists[0].name = "../../Artist"
    item.album.title = "A/B\\C"
    item.track.title = "../../Song"
    target = build_target_path(tmp_path, item, PathTemplate("{artist}/{album}/{title}"))
    assert len(target.relative_to(tmp_path).parts) == 3
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert "/" not in target.name


@pytest.mark.parametrize("title", ["CON", "NUL", "..", "   ", "Song:*?", "я" * 300])
def test_names_are_portable_and_bounded(tmp_path, title):
    item = context()
    item.track.title = title
    target = build_target_path(tmp_path, item, PathTemplate("{title}"))
    assert target.name not in {"", ".", "..", "CON", "NUL"}
    assert not any(char in target.name for char in ":*?")
    assert len(target.name.encode("utf-8")) <= 160


@pytest.mark.parametrize(
    "template",
    [
        "",
        "/{title}",
        "../{title}",
        "a/../{title}",
        "a//{title}",
        "a/./{title}",
        "C:/{title}",
        "a\\{title}",
        "{unknown}",
        "{track.id}",
        "{artist[0]}",
        "{title!r}",
        "{title:02d}",
        "{track_number:999999d}",
        "{title",
        "{track_number:{year}d}",
    ],
)
def test_invalid_templates_are_rejected(template):
    with pytest.raises(ValueError):
        PathTemplate(template)


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "Artist").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="за пределы"):
        select_target_path(root, context(), PathTemplate("{artist}/{title}"), ".mp3")


def test_cli_accepts_template_and_rejects_invalid_before_token(monkeypatch):
    monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token")
    options, _ = parse_options(["123", "--path-template", "{artist}/{title}"])
    assert options.path_template.pattern == "{artist}/{title}"
    monkeypatch.delenv("YANDEX_MUSIC_TOKEN")
    with pytest.raises(SystemExit) as error:
        parse_options(["123", "--path-template", "../{title}"])
    assert error.value.code == 2


def test_service_creates_nested_directory_and_keeps_temporary_file_there(tmp_path, monkeypatch):
    item = context()
    engine = Mock()
    info = Mock(codec="mp3", bitrate_in_kbps=320)
    monkeypatch.setattr(Track, "get_download_info", lambda self: [info])
    target = tmp_path / "Artist/Album/03 - Song.mp3"

    def download(url, temporary, **kwargs):
        assert temporary.parent == target.parent
        temporary.write_bytes(b"audio")

    engine.download.side_effect = download
    monkeypatch.setattr("music_download.service.write_tags", Mock())
    service = DownloadService(Mock(), engine)
    monkeypatch.setattr(service, "_fetch_metadata", Mock())
    result = service.download(
        item,
        tmp_path,
        DownloadOptions(path_template="{artist}/{album}/{track_number:02d} - {title}"),
    )
    assert result.path == target
    assert target.read_bytes() == b"audio"
    assert list(target.parent.iterdir()) == [target]


def test_collision_suffix_stays_in_nested_directory(tmp_path, monkeypatch):
    item = context()
    folder = tmp_path / "Artist"
    folder.mkdir()
    (folder / "Song.mp3").write_bytes(b"existing")
    monkeypatch.setattr("music_download.tags.read_track_id", lambda path: None)
    assert (
        select_target_path(tmp_path, item, PathTemplate("{artist}/{title}"), ".mp3")
        == folder / "Song [123].mp3"
    )


@pytest.mark.parametrize("overwrite", [False, True])
def test_nested_existing_track_respects_overwrite(tmp_path, monkeypatch, overwrite):
    item = context()
    target = tmp_path / "Artist/Song.mp3"
    target.parent.mkdir()
    target.write_bytes(b"original")
    monkeypatch.setattr("music_download.tags.read_track_id", lambda path: "123")
    monkeypatch.setattr("music_download.service.write_tags", Mock())
    info = Mock(codec="mp3", bitrate_in_kbps=320)
    monkeypatch.setattr(Track, "get_download_info", lambda self: [info])
    engine = Mock()
    engine.download.side_effect = lambda url, path, **kwargs: path.write_bytes(b"replacement")
    service = DownloadService(Mock(), engine)
    monkeypatch.setattr(service, "_fetch_metadata", Mock())
    result = service.download(
        item, tmp_path, DownloadOptions(path_template="{artist}/{title}", overwrite=overwrite)
    )
    assert result.path == target
    assert result.skipped is not overwrite
    assert target.read_bytes() == (b"replacement" if overwrite else b"original")
    assert engine.download.call_count == int(overwrite)


def test_failed_nested_download_cleans_temporary_files(tmp_path, monkeypatch):
    info = Mock(codec="mp3", bitrate_in_kbps=320)
    monkeypatch.setattr(Track, "get_download_info", lambda self: [info])
    engine = Mock()

    def download(url, temporary, **kwargs):
        temporary.write_bytes(b"partial")
        Path(str(temporary) + ".aria2").write_bytes(b"control")
        raise OSError("transfer failed")

    engine.download.side_effect = download
    with pytest.raises(OSError, match="transfer failed"):
        DownloadService(Mock(), engine).download(
            context(), tmp_path, DownloadOptions(path_template="{artist}/{title}")
        )
    assert list((tmp_path / "Artist").iterdir()) == []


def test_validated_template_is_reused_without_revalidation(tmp_path, monkeypatch):
    from music_download import paths

    validate = Mock(wraps=paths._validate_path_template)
    monkeypatch.setattr(paths, "_validate_path_template", validate)
    template = PathTemplate("{artist}/{title}")
    options = DownloadOptions(path_template=template)
    assert options.path_template is template
    for _ in range(3):
        select_target_path(tmp_path, context(), options.path_template, ".mp3")
    validate.assert_called_once_with("{artist}/{title}")


def test_library_validates_string_template_when_options_are_created():
    with pytest.raises(ValueError, match="Неизвестное поле"):
        DownloadOptions(path_template="{unknown}")
    options = DownloadOptions(path_template="{artist}/{title}")
    assert isinstance(options.path_template, PathTemplate)


def test_collision_preserves_dots_in_title_and_adds_extension_once(tmp_path):
    item = context()
    item.track.title = "Song.live"
    (tmp_path / "Song.live.mp3").write_bytes(b"unknown")
    assert select_target_path(tmp_path, item, PathTemplate(), ".mp3").name == "Song.live [123].mp3"


def test_collision_suffix_cannot_follow_symlink_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "Song.mp3").write_bytes(b"unknown")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    (root / "Song [123].mp3").symlink_to(outside)
    with pytest.raises(ValueError, match="за пределы"):
        select_target_path(root, context(), PathTemplate(), ".mp3")
    assert outside.read_bytes() == b"outside"
