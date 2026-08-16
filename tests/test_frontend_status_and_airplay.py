import subprocess

import tornado.testing
import tornado.web

from mopidy_platinum.frontend import airplay_reconnector, factory, queue_filler


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _FakePlayback:
    def __init__(self, state="playing", track=None, time_position=65_000):
        self._state = state
        self._track = track
        self._time_position = time_position

    def get_state(self):
        return _ImmediateFuture(self._state)

    def get_current_track(self):
        return _ImmediateFuture(self._track)

    def get_time_position(self):
        return _ImmediateFuture(self._time_position)


class _FakeCore:
    def __init__(self):
        self.playback = _FakePlayback()


class NowPlayingStatusHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        platinum_config = {"refresh_interval": 30, "max_list_items": 200, "status_refresh_interval": 2}
        handlers = factory({"platinum": platinum_config}, _FakeCore())
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_renders_just_the_ticking_status_line(self):
        response = self.fetch("/status")
        assert response.code == 200
        assert b"Playing" in response.body
        assert b"1:05" in response.body
        # not part of this fragment -- that's the whole point of splitting it out
        assert b"Album art" not in response.body

    def test_no_reload_script_when_expected_uri_matches_current_track(self):
        # The fixture's default playback has no current track, so "" is what
        # a same-track outer page would have been rendered with.
        response = self.fetch("/status?expected_uri=")
        assert b"location.reload" not in response.body

    def test_reload_script_when_track_changed_out_from_under_the_outer_page(self):
        # Regression test: an outer page that was rendered for a track that's
        # since changed (e.g. skipped via another client/remote, not just
        # reaching its predicted end) must be told to catch up -- this frame
        # polls reliably every couple of seconds regardless of why the track
        # changed, so it's the one place that can promptly notice.
        response = self.fetch("/status?expected_uri=some%3Aother%3Atrack")
        assert b"location.reload" in response.body

    def test_bg_override_applies_as_both_attribute_and_inline_style(self):
        # Needs both: bgcolor alone would just get overridden by the
        # stylesheet's .status-frame-body rule once it loads (external CSS
        # beats an HTML presentational attribute), so the persistent
        # override has to be the inline style too.
        response = self.fetch("/status?bg=DCDFE8")
        assert b'bgcolor="#DCDFE8"' in response.body
        assert b"background-color:#DCDFE8" in response.body

    def test_invalid_bg_is_ignored(self):
        response = self.fetch("/status?bg=not-a-color;color:red")
        assert b"bgcolor=" not in response.body
        assert b"not-a-color" not in response.body


def _forbid_real_subprocess_calls(cmd, **kwargs):
    raise AssertionError(f"a test tried to run a real subprocess: {cmd}")


class AirplayReconnectHandlerTest(tornado.testing.AsyncHTTPTestCase):
    """Regression test: clicking Reconnect must kick off the background reconnect
    and redirect immediately, never blocking the request on the actual OS calls.
    """

    def setUp(self):
        # If the scheduled continuation fires during this test's IOLoop, it must
        # never touch the real system -- restored in tearDown either way.
        self._real_subprocess_run = subprocess.run
        subprocess.run = _forbid_real_subprocess_calls
        super().setUp()

    def tearDown(self):
        super().tearDown()
        subprocess.run = self._real_subprocess_run

    def get_app(self):
        queue_filler.cancel()
        airplay_reconnector.phase = None  # shared singleton -- start each test clean
        airplay_reconnector.error = None
        airplay_reconnector.generation += 1
        platinum_config = {
            "refresh_interval": 30,
            "max_list_items": 200,
            "status_refresh_interval": 2,
            "airplay_device_name": "VSX-528",
        }
        handlers = factory({"platinum": platinum_config}, _FakeCore())
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_posting_starts_a_reconnect_and_redirects_back(self):
        response = self.fetch(
            "/airplay/reconnect",
            method="POST",
            body="return_to=%2Fplatinum%2F",
            follow_redirects=False,
        )

        assert response.code == 302
        assert airplay_reconnector.in_progress
        assert airplay_reconnector.phase == "restarting audio service"
