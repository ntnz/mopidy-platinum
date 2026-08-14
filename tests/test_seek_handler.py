from urllib.parse import unquote_plus

import tornado.testing
import tornado.web

from mopidy_platinum.frontend import factory, queue_filler


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _RecordingPlayback:
    def __init__(self):
        self.seek_calls = []

    def seek(self, time_position):
        self.seek_calls.append(time_position)
        return _ImmediateFuture(True)


class _FakeCore:
    def __init__(self):
        self.playback = _RecordingPlayback()


class SeekHandlerTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        queue_filler.cancel()  # queue_filler is a module-level singleton; start each test clean
        self.core = _FakeCore()
        platinum_config = {"refresh_interval": 15, "max_list_items": 200}
        handlers = factory({"platinum": platinum_config}, self.core)
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_posting_a_position_seeks_and_redirects_back(self):
        response = self.fetch(
            "/seek",
            method="POST",
            body="position_ms=45000&return_to=%2Fplatinum%2Fstatus",
            follow_redirects=False,
        )

        assert response.code == 302
        assert unquote_plus(response.headers["Location"]) == "/platinum/status"
        assert self.core.playback.seek_calls == [45000]

    def test_missing_position_is_a_no_op(self):
        response = self.fetch(
            "/seek",
            method="POST",
            body="return_to=%2Fplatinum%2Fstatus",
            follow_redirects=False,
        )

        assert response.code == 302
        assert self.core.playback.seek_calls == []
