from mopidy_platinum.background import BATCH_SIZE, QueueFiller


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _RecordingTracklist:
    def __init__(self):
        self.calls = []

    def add(self, uris=None):
        self.calls.append(list(uris))
        return _ImmediateFuture([])


class _FakeCore:
    def __init__(self):
        self.tracklist = _RecordingTracklist()


def _drain(filler, core):
    """Run scheduled batches synchronously instead of waiting on the real IOLoop."""
    generation = filler.generation
    while filler.remaining:
        filler._run_batch(core, generation)


def test_fills_a_huge_playlist_in_small_batches():
    core = _FakeCore()
    filler = QueueFiller()
    uris = [f"tidal:track:{i}" for i in range(8000)]

    filler.start(core, uris, "Huge playlist")
    _drain(filler, core)

    added = [uri for call in core.tracklist.calls for uri in call]
    assert added == uris
    assert all(len(call) <= BATCH_SIZE for call in core.tracklist.calls)
    assert filler.queued == 8000
    assert not filler.in_progress
    assert filler.status() is None


def test_starting_a_new_fill_supersedes_the_previous_one():
    core = _FakeCore()
    filler = QueueFiller()

    filler.start(core, [f"a:{i}" for i in range(200)], "First")
    filler._run_batch(core, filler.generation)  # flush exactly one batch of "a:" uris

    filler.start(core, [f"b:{i}" for i in range(50)], "Second")
    _drain(filler, core)

    added = [uri for call in core.tracklist.calls for uri in call]
    assert added.count("a:0") == 1  # the one batch that got through before being superseded
    assert added.count("b:0") == 1
    assert not any(uri.startswith("a:") and int(uri.split(":")[1]) >= BATCH_SIZE for uri in added)
    assert filler.playlist_name == "Second"


def test_cancel_stops_further_batches_from_running():
    core = _FakeCore()
    filler = QueueFiller()

    filler.start(core, [f"a:{i}" for i in range(100)], "Playlist")
    filler.cancel()

    assert not filler.in_progress
    assert filler.status() is None

    # Even if a stale scheduled batch fires after cancel, it must be a no-op.
    filler._run_batch(core, filler.generation - 1)
    assert core.tracklist.calls == []


def test_a_failing_batch_does_not_stop_the_rest_of_the_playlist():
    core = _FakeCore()
    call_count = 0
    real_add = core.tracklist.add

    def flaky_add(uris=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated backend hiccup")
        return real_add(uris=uris)

    core.tracklist.add = flaky_add

    filler = QueueFiller()
    uris = [f"tidal:track:{i}" for i in range(BATCH_SIZE * 3)]
    filler.start(core, uris, "Playlist")
    _drain(filler, core)

    assert not filler.in_progress
    assert filler.queued == len(uris)  # counted even though one batch's add() failed
