import base64

import httpx
import pytest

from mopidy_platinum import art


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _FakeLibrary:
    def __init__(self, images_by_uri):
        self._images_by_uri = images_by_uri

    def get_images(self, uris):
        return _ImmediateFuture({uri: self._images_by_uri.get(uri, ()) for uri in uris})


class _FakeCore:
    def __init__(self, images_by_uri):
        self.library = _FakeLibrary(images_by_uri)


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def _clear_cache():
    art._cache = art.ArtCache()
    yield


def test_returns_placeholder_when_no_image_available():
    core = _FakeCore({})
    content_type, data = art.get_thumbnail(core, "tidal:track:1", 32)
    assert (content_type, data) == art.PLACEHOLDER


def test_returns_placeholder_on_unsupported_uri_scheme():
    from mopidy.models import Image

    core = _FakeCore({"tidal:track:1": (Image(uri="ftp://example/art.jpg"),)})
    content_type, data = art.get_thumbnail(core, "tidal:track:1", 32)
    assert (content_type, data) == art.PLACEHOLDER


def test_decodes_data_uri_and_resizes(monkeypatch):
    from mopidy.models import Image

    data_uri = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode()
    core = _FakeCore({"tidal:track:1": (Image(uri=data_uri),)})

    content_type, data = art.get_thumbnail(core, "tidal:track:1", 32)

    assert content_type == "image/jpeg"
    assert data != TINY_PNG  # actually got re-encoded, not just passed through
    assert len(data) > 0


def test_fetches_http_uri_and_caches_result(monkeypatch):
    from mopidy.models import Image

    core = _FakeCore({"tidal:track:1": (Image(uri="https://example/art.jpg"),)})

    calls = []

    def fake_get(url, timeout=None, follow_redirects=None):
        calls.append(url)
        return httpx.Response(200, content=TINY_PNG, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    first = art.get_thumbnail(core, "tidal:track:1", 32)
    second = art.get_thumbnail(core, "tidal:track:1", 32)

    assert first[0] == "image/jpeg"
    assert first == second
    assert len(calls) == 1  # second call was served from cache, not re-fetched


def test_falls_back_to_placeholder_when_fetch_raises(monkeypatch):
    from mopidy.models import Image

    core = _FakeCore({"tidal:track:1": (Image(uri="https://example/art.jpg"),)})

    def fake_get(url, timeout=None, follow_redirects=None):
        raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(httpx, "get", fake_get)

    assert art.get_thumbnail(core, "tidal:track:1", 32) == art.PLACEHOLDER


def test_falls_back_to_placeholder_on_corrupt_image_data(monkeypatch):
    from mopidy.models import Image

    core = _FakeCore({"tidal:track:1": (Image(uri="https://example/art.jpg"),)})

    def fake_get(url, timeout=None, follow_redirects=None):
        return httpx.Response(200, content=b"not actually an image")

    monkeypatch.setattr(httpx, "get", fake_get)

    assert art.get_thumbnail(core, "tidal:track:1", 32) == art.PLACEHOLDER
