import tornado.testing
import tornado.web

from mopidy_platinum.frontend import factory, queue_filler


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _RecordingMixer:
    def __init__(self, volume=50, mute=False):
        self.volume = volume
        self.mute = mute
        self.set_volume_calls = []
        self.set_mute_calls = []

    def get_volume(self):
        return _ImmediateFuture(self.volume)

    def get_mute(self):
        return _ImmediateFuture(self.mute)

    def set_volume(self, value):
        self.set_volume_calls.append(value)
        self.volume = value
        return _ImmediateFuture(True)

    def set_mute(self, value):
        self.set_mute_calls.append(value)
        self.mute = value
        return _ImmediateFuture(True)


class VolumeHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        queue_filler.cancel()
        self.core = type("Core", (), {"mixer": _RecordingMixer(volume=50)})()
        platinum_config = {"refresh_interval": 15, "max_list_items": 200}
        handlers = factory({"platinum": platinum_config}, self.core)
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_level_sets_an_absolute_volume(self):
        response = self.fetch(
            "/volume",
            method="POST",
            body="level=70&return_to=%2Fplatinum%2F",
            follow_redirects=False,
        )

        assert response.code == 302
        assert self.core.mixer.set_volume_calls == [70]

    def test_level_is_clamped_to_100(self):
        self.fetch(
            "/volume",
            method="POST",
            body="level=150&return_to=%2Fplatinum%2F",
            follow_redirects=False,
        )

        assert self.core.mixer.set_volume_calls == [100]

    def test_delta_still_works_alongside_level(self):
        response = self.fetch(
            "/volume",
            method="POST",
            body="delta=10&return_to=%2Fplatinum%2F",
            follow_redirects=False,
        )

        assert response.code == 302
        assert self.core.mixer.set_volume_calls == [60]

    def test_mute_still_works_independently_of_level(self):
        self.fetch(
            "/volume",
            method="POST",
            body="level=40&mute=1&return_to=%2Fplatinum%2F",
            follow_redirects=False,
        )

        assert self.core.mixer.set_volume_calls == [40]
        assert self.core.mixer.set_mute_calls == [True]


class _FakeRef:
    def __init__(self, uri, name):
        self.uri = uri
        self.name = name


class _FakePlaylists:
    def __init__(self, refs):
        self._refs = refs

    def as_list(self):
        return _ImmediateFuture(self._refs)


class PlaylistsSourceFilterTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        queue_filler.cancel()
        refs = [
            _FakeRef("tidal:playlist:1", "Tidal Mix"),
            _FakeRef("spotify:playlist:1", "Spotify Mix"),
            _FakeRef("spotify:playlist:2", "Another Spotify Mix"),
        ]
        self.core = type("Core", (), {"playlists": _FakePlaylists(refs)})()
        platinum_config = {"refresh_interval": 15, "max_list_items": 200}
        handlers = factory({"platinum": platinum_config}, self.core)
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_no_filter_shows_every_source(self):
        response = self.fetch("/playlists")
        body = response.body.decode()
        assert "Tidal Mix" in body
        assert "Spotify Mix" in body
        assert "Another Spotify Mix" in body

    def test_filtering_by_source_hides_other_sources(self):
        response = self.fetch("/playlists?source=spotify")
        body = response.body.decode()
        assert "Tidal Mix" not in body
        assert "Spotify Mix" in body
        assert "Another Spotify Mix" in body

    def test_filter_dropdown_offers_only_sources_actually_present(self):
        response = self.fetch("/playlists")
        body = response.body.decode()
        assert "Tidal" in body
        assert "Spotify" in body
