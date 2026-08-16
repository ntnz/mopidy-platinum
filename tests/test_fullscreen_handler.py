import tornado.testing
import tornado.web
from mopidy.types import PlaybackState

from mopidy_platinum.frontend import factory, queue_filler


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _FakeTrack:
    def __init__(self, uri, name):
        self.uri = uri
        self.name = name
        self.length = 200_000
        self.album = None
        self.artists = []


class _FakePlayback:
    def __init__(self, track=None):
        self._track = track

    def get_state(self):
        return _ImmediateFuture(PlaybackState.PLAYING)

    def get_current_track(self):
        return _ImmediateFuture(self._track)

    def get_time_position(self):
        return _ImmediateFuture(50_000)

    def get_current_tlid(self):
        return _ImmediateFuture(None)


class _FakeMixer:
    def get_volume(self):
        return _ImmediateFuture(50)

    def get_mute(self):
        return _ImmediateFuture(False)


class _FakeTracklist:
    def get_random(self):
        return _ImmediateFuture(False)

    def get_tl_tracks(self):
        return _ImmediateFuture([])


class _FakeCore:
    def __init__(self, track=None):
        self.playback = _FakePlayback(track)
        self.mixer = _FakeMixer()
        self.tracklist = _FakeTracklist()


class FullScreenHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        queue_filler.cancel()  # queue_filler is a module-level singleton; start each test clean
        self.core = _FakeCore(track=_FakeTrack("tidal:track:1", "Song"))
        platinum_config = {"refresh_interval": 60, "max_list_items": 200, "status_refresh_interval": 2}
        handlers = factory({"platinum": platinum_config}, self.core)
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_renders_large_art_and_transport_controls(self):
        response = self.fetch("/fullscreen")
        body = response.body.decode()

        assert response.code == 200
        assert "Something went wrong" not in body
        # Requests a bigger thumbnail than anywhere else in the app, but a
        # fixed, conservative size -- max-width/max-height CSS turned out
        # not to be honored on the actual target browser, so this is hard
        # sized small enough to fit a small period display's real usable
        # area (screen resolution minus browser chrome) without it.
        assert "uri=tidal%3Atrack%3A1&amp;size=280" in body
        assert 'src="/platinum/static/icon-prev.png"' in body
        assert 'src="/platinum/static/icon-pause.png"' in body  # is_playing -> pause icon
        assert 'src="/platinum/static/icon-next.png"' in body
        # No tab bar/footer chrome from base.html on this standalone page.
        assert "tabbar" not in body
        assert "Exit Full Screen" in body

    def test_status_frame_uses_the_same_light_styling_as_everywhere_else(self):
        # Mac OS 9 never had a "dark mode" -- this view uses the same light
        # Platinum palette as the rest of the app, not a separate theme, so
        # there's nothing frame-specific to request or apply here.
        response = self.fetch("/fullscreen")
        body = response.body.decode()
        assert "dark=1" not in body

        status_response = self.fetch("/status?expected_uri=tidal%3Atrack%3A1")
        assert b'class="status-frame-body"' in status_response.body

    def test_no_track_falls_back_to_placeholder_text_without_error(self):
        self.core.playback = _FakePlayback(track=None)
        response = self.fetch("/fullscreen")
        body = response.body.decode()
        assert response.code == 200
        assert "(Nothing playing)" in body
        assert "<img" not in body  # no art tag at all when there's no track to show art for
