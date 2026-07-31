from mopidy.models import Track

from mopidy_platinum.track_cache import TrackCache


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _RecordingLibrary:
    def __init__(self, tracks_by_uri):
        self._tracks_by_uri = tracks_by_uri
        self.lookup_calls = []

    def lookup(self, uris):
        self.lookup_calls.append(list(uris))
        return _ImmediateFuture({uri: [self._tracks_by_uri[uri]] for uri in uris if uri in self._tracks_by_uri})


class _FailingLibrary:
    def lookup(self, uris):
        raise RuntimeError("backend unreachable")


def test_resolves_and_returns_tracks():
    track = Track(uri="tidal:track:1", name="Song")
    library = _RecordingLibrary({"tidal:track:1": track})
    core = type("Core", (), {"library": library})()
    cache = TrackCache()

    result = cache.get_many(core, ["tidal:track:1"])

    assert result == {"tidal:track:1": track}
    assert library.lookup_calls == [["tidal:track:1"]]


def test_second_lookup_of_same_uri_is_served_from_cache():
    track = Track(uri="tidal:track:1", name="Song")
    library = _RecordingLibrary({"tidal:track:1": track})
    core = type("Core", (), {"library": library})()
    cache = TrackCache()

    cache.get_many(core, ["tidal:track:1"])
    cache.get_many(core, ["tidal:track:1"])

    assert library.lookup_calls == [["tidal:track:1"]]  # only looked up once


def test_only_fetches_the_newly_seen_uris():
    tracks = {f"tidal:track:{i}": Track(uri=f"tidal:track:{i}") for i in range(5)}
    library = _RecordingLibrary(tracks)
    core = type("Core", (), {"library": library})()
    cache = TrackCache()

    cache.get_many(core, [f"tidal:track:{i}" for i in range(3)])
    cache.get_many(core, [f"tidal:track:{i}" for i in range(5)])

    assert library.lookup_calls == [
        ["tidal:track:0", "tidal:track:1", "tidal:track:2"],
        ["tidal:track:3", "tidal:track:4"],
    ]


def test_missing_track_is_cached_as_none():
    library = _RecordingLibrary({})
    core = type("Core", (), {"library": library})()
    cache = TrackCache()

    result = cache.get_many(core, ["tidal:track:unknown"])

    assert result == {"tidal:track:unknown": None}
    assert library.lookup_calls == [["tidal:track:unknown"]]


def test_a_failed_lookup_is_not_cached_so_it_can_be_retried():
    core = type("Core", (), {"library": _FailingLibrary()})()
    cache = TrackCache()

    result = cache.get_many(core, ["tidal:track:1"])

    assert result == {}
    assert "tidal:track:1" not in cache._cache


def test_cache_evicts_oldest_entries_past_max_size():
    tracks = {f"t:{i}": Track(uri=f"t:{i}") for i in range(5)}
    library = _RecordingLibrary(tracks)
    core = type("Core", (), {"library": library})()
    cache = TrackCache(max_entries=3)

    cache.get_many(core, list(tracks.keys()))

    assert len(cache._cache) == 3
    assert list(cache._cache.keys()) == ["t:2", "t:3", "t:4"]
