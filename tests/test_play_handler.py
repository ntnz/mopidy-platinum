from urllib.parse import unquote_plus

import tornado.testing
import tornado.web

from mopidy_platinum.frontend import factory, queue_filler


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _FakeTlTrack:
    def __init__(self, tlid, uri):
        self.tlid = tlid
        self.uri = uri


class _RecordingTracklist:
    def __init__(self, tl_tracks=()):
        self.tl_tracks = list(tl_tracks)
        self.add_calls = []
        self._next_tlid = (max((t.tlid for t in self.tl_tracks), default=0)) + 1

    def get_tl_tracks(self):
        return _ImmediateFuture(self.tl_tracks)

    def add(self, uris=None, at_position=None):
        self.add_calls.append({"uris": list(uris), "at_position": at_position})
        new_tracks = [_FakeTlTrack(self._next_tlid, uri) for uri in uris]
        self._next_tlid += len(new_tracks)
        if at_position is not None:
            self.tl_tracks[at_position:at_position] = new_tracks
        else:
            self.tl_tracks.extend(new_tracks)
        return _ImmediateFuture(new_tracks)


class _RecordingPlayback:
    def __init__(self, current_tlid=None):
        self.current_tlid = current_tlid
        self.played_tlid = None

    def get_current_tlid(self):
        return _ImmediateFuture(self.current_tlid)

    def play(self, tlid=None):
        self.played_tlid = tlid
        return _ImmediateFuture(None)


class _FakeCore:
    def __init__(self, tl_tracks=(), current_tlid=None):
        self.tracklist = _RecordingTracklist(tl_tracks)
        self.playback = _RecordingPlayback(current_tlid)


class PlayHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        queue_filler.cancel()  # queue_filler is a module-level singleton; start each test clean
        self.core = self.make_core()
        platinum_config = {"refresh_interval": 15, "max_list_items": 200}
        handlers = factory({"platinum": platinum_config}, self.core)
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def make_core(self):
        raise NotImplementedError

    def post_play(self, uri):
        return self.fetch(
            "/play",
            method="POST",
            body=f"uri={uri}&return_to=%2Fplatinum%2F",
            follow_redirects=False,
        )


class PlayNextWithActiveQueueTest(PlayHandlerTest):
    """Regression test: with a big playlist already playing, requesting a track
    must insert it right after the current one -- not interrupt playback or
    disturb anything else queued up.
    """

    def make_core(self):
        tl_tracks = [
            _FakeTlTrack(1, "tidal:track:already-playing"),
            _FakeTlTrack(2, "tidal:track:up-next"),
            _FakeTlTrack(3, "tidal:track:later"),
        ]
        return _FakeCore(tl_tracks=tl_tracks, current_tlid=1)

    def test_inserts_after_current_track_without_interrupting_playback(self):
        response = self.post_play("tidal:track:requested")

        assert response.code == 302
        assert unquote_plus(response.headers["Location"]) == "/platinum/"

        assert self.core.tracklist.add_calls == [{"uris": ["tidal:track:requested"], "at_position": 1}]
        assert [t.uri for t in self.core.tracklist.tl_tracks] == [
            "tidal:track:already-playing",
            "tidal:track:requested",
            "tidal:track:up-next",
            "tidal:track:later",
        ]
        # Nothing already playing gets touched.
        assert self.core.playback.played_tlid is None


class PlayImmediatelyWhenIdleTest(PlayHandlerTest):
    """Regression test: with nothing currently playing, there's nothing to
    preserve, so the requested track should just start right away.
    """

    def make_core(self):
        return _FakeCore(tl_tracks=[], current_tlid=None)

    def test_falls_back_to_playing_immediately(self):
        response = self.post_play("tidal:track:requested")

        assert response.code == 302
        assert self.core.tracklist.add_calls == [{"uris": ["tidal:track:requested"], "at_position": None}]
        assert self.core.playback.played_tlid == self.core.tracklist.tl_tracks[0].tlid
