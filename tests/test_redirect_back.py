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
        self.next_calls = 0

    def next(self):
        self.next_calls += 1
        return _ImmediateFuture(None)


class _FakeCore:
    def __init__(self):
        self.playback = _RecordingPlayback()


class RedirectBackDropsStaleCacheBusterTest(tornado.testing.AsyncHTTPTestCase):
    """Regression test: return_to is seeded from a page's own request.uri, which
    -- once that page has auto-refreshed at least once -- carries a stale
    cache-busting "_" query param from that reload. Redirecting back to that
    exact URL let a caching-happy old browser serve its stale copy instead of
    re-fetching (e.g. still showing the art for the track skipped past).
    """

    def get_app(self):
        queue_filler.cancel()  # queue_filler is a module-level singleton; start each test clean
        self.core = _FakeCore()
        platinum_config = {"refresh_interval": 15, "max_list_items": 200}
        handlers = factory({"platinum": platinum_config}, self.core)
        return tornado.web.Application(handlers, cookie_secret="test-secret")

    def test_strips_stale_cache_buster_from_return_to(self):
        response = self.fetch(
            "/control",
            method="POST",
            body="action=next&return_to=%2Fplatinum%2F%3F_%3D1234567890",
            follow_redirects=False,
        )

        assert response.code == 302
        assert self.core.playback.next_calls == 1
        location = unquote_plus(response.headers["Location"])
        assert location == "/platinum/"
        assert "_=" not in location

    def test_still_appends_error_on_failure_without_the_stale_param(self):
        def boom():
            raise RuntimeError("kaboom")

        self.core.playback.next = boom

        response = self.fetch(
            "/control",
            method="POST",
            body="action=next&return_to=%2Fplatinum%2F%3F_%3D1234567890",
            follow_redirects=False,
        )

        assert response.code == 302
        location = unquote_plus(response.headers["Location"])
        assert location.startswith("/platinum/?error=")
        assert "_=" not in location
