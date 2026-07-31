from urllib.parse import unquote_plus

import pykka
import tornado.testing
import tornado.web

from mopidy_platinum.frontend import factory, queue_filler


class _TimeoutFuture:
    def get(self, *, timeout=None):
        raise pykka.Timeout("simulated hang")


class _FakePlayback:
    def get_state(self):
        return _TimeoutFuture()


class _FakeCore:
    def __init__(self):
        self.playback = _FakePlayback()


class NowPlayingTimeoutTest(tornado.testing.AsyncHTTPTestCase):
    """Regression test: a hung backend call must render the error page, not crash."""

    def get_app(self):
        queue_filler.cancel()  # queue_filler is a module-level singleton; start each test clean
        platinum_config = {"refresh_interval": 15, "max_list_items": 200}
        handlers = factory({"platinum": platinum_config}, _FakeCore())
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_now_playing_renders_error_page_instead_of_crashing(self):
        response = self.fetch("/")
        assert response.code == 200
        assert b"took too long" in response.body


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _FakeRef:
    def __init__(self, uri):
        self.uri = uri


class _FakeTlTrack:
    def __init__(self, tlid, uri):
        self.tlid = tlid
        self.uri = uri


class _RecordingPlaylists:
    def __init__(self, uris):
        self._refs = [_FakeRef(uri) for uri in uris]

    def get_items(self, uri):
        return _ImmediateFuture(self._refs)


class _RecordingTracklist:
    def __init__(self):
        self.add_calls = []
        self._next_tlid = 1

    def clear(self):
        return _ImmediateFuture(None)

    def add(self, uris=None):
        # A real backend (e.g. Tidal) would resolve every one of these with
        # its own lookup here -- this is exactly the call that must never
        # receive thousands of URIs at once in a single shot.
        uris = list(uris)
        self.add_calls.append(uris)
        tl_tracks = []
        for uri in uris:
            tl_tracks.append(_FakeTlTrack(self._next_tlid, uri))
            self._next_tlid += 1
        return _ImmediateFuture(tl_tracks)


class _RecordingPlayback:
    def __init__(self):
        self.played_tlid = None

    def play(self, tlid=None):
        self.played_tlid = tlid
        return _ImmediateFuture(None)


class _HugePlaylistCore:
    def __init__(self, track_count):
        uris = [f"tidal:track:{i}" for i in range(track_count)]
        self.playlists = _RecordingPlaylists(uris)
        self.tracklist = _RecordingTracklist()
        self.playback = _RecordingPlayback()


class PlaylistPlayStartsInstantlyTest(tornado.testing.AsyncHTTPTestCase):
    """Regression test: Play All on an 8000-track playlist must respond immediately,
    having only added/played the first track synchronously -- the rest is handed off
    to the background filler instead of being resolved in this one request.
    """

    TRACK_COUNT = 8000

    def get_app(self):
        queue_filler.cancel()  # queue_filler is a module-level singleton; start each test clean
        platinum_config = {"refresh_interval": 15, "max_list_items": 200}
        self.core = _HugePlaylistCore(track_count=self.TRACK_COUNT)
        handlers = factory({"platinum": platinum_config}, self.core)
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_play_all_only_adds_first_track_synchronously(self):
        response = self.fetch(
            "/playlist/play",
            method="POST",
            body="uri=tidal:playlist:huge&name=Huge+playlist&shuffle=0",
            follow_redirects=False,
        )

        assert response.code == 302
        assert unquote_plus(response.headers["Location"]) == "/platinum/"

        # Exactly one add() call, for exactly the first track -- nothing else
        # about this request should have touched core.tracklist for the
        # other 7999 tracks.
        assert self.core.tracklist.add_calls == [["tidal:track:0"]]
        assert self.core.playback.played_tlid == 1
