import unittest
from unittest.mock import Mock, patch

from music_download.providers.yandex import YandexProvider
from music_download.sources import Source
from yandex_music import Album, Client, Playlist, Track, TrackShort
from yandex_music.exceptions import BadRequestError, NetworkError


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(spec=Client)
        self.provider = YandexProvider(self.client)

    def test_album_tracks_keep_disc_order(self):
        first, second, third = Track(id="1"), Track(id="2"), Track(id="3")
        self.client.albums_with_tracks.return_value = Album(
            id=42, volumes=[[first, second], [third]], available=True,
        )
        self.assertEqual(self.provider.album_tracks("42"), [first, second, third])
        self.client.albums_with_tracks.assert_called_once_with("42")

    def test_missing_or_unavailable_album(self):
        for album in (None, Album(id=42, available=False), Album(id=42, volumes=None)):
            self.client.albums_with_tracks.return_value = album
            with self.subTest(album=album), self.assertRaises(ValueError):
                self.provider.album_tracks("42")

    def test_original_track_model(self):
        track = Track(id="1", available=True)
        self.client.tracks.return_value = [track]
        self.assertIs(self.provider.track("1:2"), track)
        self.client.tracks.assert_called_once_with(["1:2"])

    def test_track_missing_or_unavailable(self):
        for tracks in [[], [Track(id="1", available=False)]]:
            self.client.tracks.return_value = tracks
            with self.assertRaises(ValueError):
                self.provider.track("1")

    def test_invalid_input_never_calls_api(self):
        with self.assertRaises(ValueError):
            self.provider.track("1:2:3")
        self.client.tracks.assert_not_called()
        self.client.users_playlists.assert_not_called()

    def test_original_playlist_model(self):
        playlist = Playlist(cover=None, tracks=[])
        self.client.users_playlists.return_value = playlist
        self.assertIs(self.provider.playlist("owner", 42), playlist)
        self.client.users_playlists.assert_called_once_with(42, user_id="owner")

    def test_missing_playlist_and_api_error(self):
        self.client.users_playlists.return_value = None
        with self.assertRaises(ValueError):
            self.provider.playlist("owner", 1)
        self.client.tracks.side_effect = NetworkError("offline")
        with self.assertRaises(NetworkError):
            self.provider.track("1")

    def test_playlist_order_and_per_entry_errors(self):
        first = Track(id="1")
        third = Track(id="3")
        fourth = Track(id="4")
        playlist = Playlist(cover=None, tracks=[
            TrackShort(id="1", timestamp="", track=first),
            TrackShort(id="2", timestamp=""),
            TrackShort(id="3", timestamp="", track=third),
            fourth,
        ])
        self.client.tracks.return_value = []
        entries = list(self.provider.resolve_playlist(playlist))
        self.assertEqual([entry.index for entry in entries], [0, 1, 2, 3])
        self.assertEqual([entry.track for entry in entries], [first, None, third, fourth])
        self.assertIsInstance(entries[1].error, ValueError)

    def test_short_track_resolves_with_album(self):
        track = Track(id="1")
        self.client.tracks.return_value = [track]
        playlist = Playlist(cover=None, tracks=[TrackShort(id="1", album_id="2", timestamp="")])
        self.assertIs(next(self.provider.resolve_playlist(playlist)).track, track)
        self.client.tracks.assert_called_once_with(["1:2"])

    def test_empty_playlist_does_not_refetch(self):
        playlist = Playlist(cover=None, tracks=[])
        with patch.object(Playlist, "fetch_tracks") as fetch:
            self.assertEqual(list(self.provider.resolve_playlist(playlist)), [])
            fetch.assert_not_called()

    def test_missing_playlist_tracks_refetch(self):
        playlist = Playlist(cover=None, tracks=None)
        with patch.object(Playlist, "fetch_tracks", return_value=[]) as fetch:
            self.assertEqual(list(self.provider.resolve_playlist(playlist)), [])
            fetch.assert_called_once()

    def test_token_initialization(self):
        with patch("music_download.providers.yandex.Client") as factory:
            provider = YandexProvider.from_token("test-token")
            factory.assert_called_once_with("test-token")
            factory.return_value.init.assert_called_once_with()
            self.assertIs(provider.client, factory.return_value.init.return_value)

    def test_playlist_by_key_uses_current_api(self):
        uuid = "12345678-1234-5678-9abc-123456789abc"
        playlist = Playlist(cover=None, tracks=[], playlist_uuid=uuid)
        self.client.playlist.return_value = playlist
        self.assertIs(self.provider.playlist_by_key(uuid), playlist)
        self.client.playlist.assert_called_once_with(uuid)
        self.client.users_playlists.assert_not_called()

    def test_invalid_key_is_rejected_by_api(self):
        error = BadRequestError("invalid playlist key")
        self.client.playlist.side_effect = error
        with self.assertRaises(BadRequestError) as raised:
            self.provider.playlist_by_key("")
        self.assertIs(raised.exception, error)
        self.client.playlist.assert_called_once_with("")

    def test_uuid_playlist_missing_and_unavailable(self):
        uuid = "12345678-1234-5678-9abc-123456789abc"
        for playlist in [None, Playlist(cover=None, available=False)]:
            self.client.playlist.return_value = playlist
            with self.assertRaises(ValueError):
                self.provider.playlist_by_key(uuid)

    def test_uuid_playlist_tracks_are_refetched_by_uuid(self):
        uuid = "12345678-1234-5678-9abc-123456789abc"
        playlist = Playlist(cover=None, tracks=None, playlist_uuid=uuid)
        self.client.playlist.return_value = Playlist(cover=None, tracks=[])
        with patch.object(Playlist, "fetch_tracks") as legacy_fetch:
            self.assertEqual(list(self.provider.resolve_playlist(playlist)), [])
            legacy_fetch.assert_not_called()
        self.client.playlist.assert_called_once_with(uuid)


class SourceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(spec=Client)
        self.provider = YandexProvider(self.client)

    def test_all_track_kinds_resolve_only_one_track(self):
        for source, identifier in [
            (Source(track_id="1"), "1"),
            (Source(track_id="1", album_id="2"), "1:2"),
            (Source(track_id="1", playlist_key="key"), "1"),
            (Source(track_id="1", playlist_id=42, user_id="owner"), "1"),
        ]:
            with self.subTest(kind=source.kind):
                self.client.reset_mock()
                track = Track(id="1")
                self.client.tracks.return_value = [track]
                entries = list(self.provider.resolve_source(source))
                self.assertEqual(len(entries), 1)
                self.assertIs(entries[0].track, track)
                self.assertEqual(entries[0].index, 0)
                self.client.tracks.assert_called_once_with([identifier])
                self.client.playlist.assert_not_called()
                self.client.users_playlists.assert_not_called()
                self.client.albums_with_tracks.assert_not_called()

    def test_album_kind_keeps_volume_order(self):
        first, second = Track(id="1"), Track(id="2")
        self.client.albums_with_tracks.return_value = Album(id=42, volumes=[[first], [second]])
        entries = list(self.provider.resolve_source(Source(album_id="42")))
        self.assertEqual([entry.track for entry in entries], [first, second])
        self.assertEqual([entry.index for entry in entries], [0, 1])
        self.client.albums_with_tracks.assert_called_once_with("42")

    def test_both_playlist_forms_preserve_per_entry_errors(self):
        for source in [Source(playlist_key="key"), Source(user_id="owner", playlist_id=42)]:
            with self.subTest(source=source):
                self.client.reset_mock()
                playlist = Playlist(cover=None, tracks=[Track(id="1"), Track(id="2", available=False)])
                self.client.playlist.return_value = playlist
                self.client.users_playlists.return_value = playlist
                entries = list(self.provider.resolve_source(source))
                self.assertEqual([entry.track_id for entry in entries], ["1", "2"])
                self.assertIsInstance(entries[1].error, ValueError)
                if source.playlist_key:
                    self.client.playlist.assert_called_once_with("key")
                    self.client.users_playlists.assert_not_called()
                else:
                    self.client.users_playlists.assert_called_once_with(42, user_id="owner")
                    self.client.playlist.assert_not_called()

    def test_url_with_playlist_track_uses_track_route(self):
        self.client.tracks.return_value = [Track(id="1")]
        entries = list(self.provider.resolve_source("https://music.yandex.ru/playlists/key/track/1"))
        self.assertEqual(entries[0].track_id, "1")
        self.client.tracks.assert_called_once_with(["1"])
        self.client.playlist.assert_not_called()

    def test_empty_source_cannot_select_api_method(self):
        with self.assertRaises(ValueError):
            list(self.provider.resolve_source(Source()))
        self.assertEqual(self.client.mock_calls, [])

    def test_input_validation_is_delegated_to_api(self):
        cases = [
            (Source(track_id="0"), "tracks", (["0"],), {}),
            (Source(track_id="abc"), "tracks", (["abc"],), {}),
            (Source(track_id="1", album_id="0"), "tracks", (["1:0"],), {}),
            (Source(album_id="0"), "albums_with_tracks", ("0",), {}),
            (Source(playlist_key=""), "playlist", ("",), {}),
            (Source(playlist_key=" key "), "playlist", (" key ",), {}),
            (Source(playlist_id=-1, user_id=""), "users_playlists", (-1,), {"user_id": ""}),
            (Source(playlist_id=42), "users_playlists", (42,), {"user_id": None}),
        ]
        for source, method, args, kwargs in cases:
            with self.subTest(source=source):
                self.client.reset_mock()
                api_method = getattr(self.client, method)
                error = BadRequestError("invalid input")
                api_method.side_effect = error
                with self.assertRaises(BadRequestError) as raised:
                    list(self.provider.resolve_source(source))
                self.assertIs(raised.exception, error)
                api_method.assert_called_once_with(*args, **kwargs)

    def test_token_validation_is_delegated_to_client(self):
        with patch("music_download.providers.yandex.Client") as factory:
            error = BadRequestError("invalid token")
            factory.return_value.init.side_effect = error
            with self.assertRaises(BadRequestError) as raised:
                YandexProvider.from_token(" ")
            self.assertIs(raised.exception, error)
            factory.assert_called_once_with(" ")
            factory.return_value.init.assert_called_once_with()

    def test_track_rejects_collection_sources(self):
        for source in [Source(album_id="42"), Source(playlist_key="key")]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.provider.track(source)
        self.client.tracks.assert_not_called()
