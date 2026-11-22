import unittest

from music_download.sources import Source


class SourceTests(unittest.TestCase):
    def test_parsed_attributes(self):
        source = Source.parse("https://music.yandex.ru/album/456/track/123")
        self.assertEqual(source, Source(album_id="456", track_id="123"))
        self.assertEqual(source.kind, "album_track")
        source = Source.parse("https://music.yandex.ru/users/alex/playlists/42")
        self.assertEqual(source, Source(user_id="alex", playlist_id=42))
        self.assertEqual(source.kind, "playlist")

    def test_requested_link_formats(self):
        source = Source.parse("https://music.yandex.ru/album/31257385/track/126065334?utm_source=web&utm_medium=copy_link")
        self.assertEqual(source.album_id, "31257385")
        self.assertEqual(source.track_id, "126065334")
        identifier = "lk.356e8c3a-5b45-4bbc-b666-626119e5ee43"
        for segment in ("playlist", "playlists"):
            with self.subTest(segment=segment):
                source = Source.parse(f"https://music.yandex.ru/{segment}/{identifier}")
                self.assertEqual(source.kind, "playlist")
                self.assertEqual(source.playlist_key, identifier)
        for prefix in ("", "https://music.yandex.ru"):
            source = Source.parse(f"{prefix}/users/alex/playlists/42/visibility")
            self.assertEqual(source.user_id, "alex")
            self.assertEqual(source.playlist_id, 42)

    def test_album(self):
        for suffix in ("", "/", "?utm_source=web"):
            source = Source.parse(f"https://music.yandex.ru/album/31257385{suffix}")
            self.assertEqual(source, Source(album_id="31257385"))
            self.assertEqual(source.kind, "album")

    def test_playlist_track(self):
        uuid = "lk.356e8c3a-5b45-4bbc-b666-626119e5ee43"
        for segment in ("playlist", "playlists"):
            source = Source.parse(f"https://music.yandex.ru/{segment}/{uuid}/track/123")
            self.assertEqual(source, Source(playlist_key=uuid, track_id="123"))
            self.assertEqual(source.kind, "playlist_track")
        source = Source.parse("/users/alex/playlists/42/track/123")
        self.assertEqual(source, Source(user_id="alex", playlist_id=42, track_id="123"))
        self.assertEqual(source.kind, "playlist_track")
        self.assertEqual(Source.parse("https://music.yandex.ru/track/123").kind, "track")

    def test_track_sources(self):
        for value, expected in [
            ("123", "123"), ("123:456", "123:456"),
            (" https://music.yandex.ru/track/123?x=y#player ", "123"),
            ("https://music.yandex.com/album/456/track/123/", "123:456"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(Source.parse(value), Source(track_id=expected.split(":")[0],
                                 album_id=expected.split(":")[1] if ":" in expected else None))

    def test_playlist_domains_and_encoded_owner(self):
        for domain in ["ru", "com", "kz", "by"]:
            value = f"https://music.yandex.{domain}/users/%D0%B8%D0%BC%D1%8F/playlists/42"
            self.assertEqual(Source.parse(value), Source(user_id="имя", playlist_id=42))

    def test_invalid_sources(self):
        for value in ["", "１２３", "1:2:3", "1:", ":1",
                      "http://music.yandex.ru/track/1", "https://example.com/track/1",
                      "https://music.yandex.ru.evil.org/track/1",
                      "https://user@music.yandex.ru/track/1",
                      "https://music.yandex.ru:123/track/1",
                      "https://music.yandex.ru:bad/track/1",
                      "https://music.yandex.ru/track/1/extra",
                      "https://music.yandex.ru//track/1",
                      "https://music.yandex.ru/users//playlists/1"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Source.parse(value)

    def test_uuid_playlist_sources(self):
        uuid = "12345678-1234-5678-9abc-123456789abc"
        expected = Source(playlist_key=uuid)
        self.assertEqual(Source.parse(uuid), expected)
        self.assertEqual(Source.parse(uuid.upper()).playlist_key, uuid.upper())
        self.assertEqual(Source.parse(f"https://music.yandex.ru/playlists/{uuid}?utm=x"), expected)

    def test_identifier_validation_is_left_to_api(self):
        self.assertEqual(Source.from_url("https://music.yandex.ru/playlists/new-format"),
                         Source(playlist_key="new-format"))
        self.assertEqual(Source.from_url("https://music.yandex.ru/album/0"),
                         Source(album_id="0"))
