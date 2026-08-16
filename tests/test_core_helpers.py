import pykka
import pytest
from mopidy.models import Album, Artist, Image, Track
from mopidy.types import PlaybackState

from mopidy_platinum import core_helpers


def test_format_time_none():
    assert core_helpers.format_time(None) == "--:--"


def test_format_time_rounds_down_to_whole_seconds():
    assert core_helpers.format_time(65_499) == "1:05"


def test_format_time_pads_seconds():
    assert core_helpers.format_time(5_000) == "0:05"


def test_track_display_name_with_artists():
    track = Track(uri="file:///a.mp3", name="Song", artists=frozenset({Artist(name="Band")}))
    assert core_helpers.track_display_name(track) == "Band - Song"


def test_track_display_name_without_artists_falls_back_to_track_name():
    track = Track(uri="file:///a.mp3", name="Song")
    assert core_helpers.track_display_name(track) == "Song"


def test_track_display_name_without_name_falls_back_to_uri():
    track = Track(uri="file:///a.mp3")
    assert core_helpers.track_display_name(track) == "file:///a.mp3"


def test_track_display_name_none():
    assert core_helpers.track_display_name(None) is None


def test_breadcrumb_roundtrip():
    crumbs = [("file:///music", "Music"), ("file:///music/rock", "Rock & Roll")]
    encoded = core_helpers.encode_breadcrumbs(crumbs)
    assert core_helpers.decode_breadcrumbs(encoded) == crumbs


def test_decode_breadcrumbs_empty():
    assert core_helpers.decode_breadcrumbs("") == []


class _HangingFuture:
    """Stands in for a pykka Future backed by a backend that never responds."""

    def get(self, *, timeout=None):
        raise pykka.Timeout("simulated hang")


def test_get_result_turns_pykka_timeout_into_core_timeout():
    with pytest.raises(core_helpers.CoreTimeout):
        core_helpers.get_result(_HangingFuture(), timeout=0.01)


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


def test_get_result_passes_through_value_on_success():
    assert core_helpers.get_result(_ImmediateFuture(42)) == 42


def test_truncate_caps_list_and_reports_total():
    visible, total = core_helpers.truncate(list(range(8000)), 300)
    assert len(visible) == 300
    assert visible == list(range(300))
    assert total == 8000


def test_truncate_passes_through_short_list_unchanged():
    visible, total = core_helpers.truncate([1, 2, 3], 300)
    assert visible == [1, 2, 3]
    assert total == 3


class _FakeLibrary:
    def __init__(self, images_by_uri):
        self._images_by_uri = images_by_uri

    def get_images(self, uris):
        return _ImmediateFuture({uri: self._images_by_uri.get(uri, ()) for uri in uris})


class _FailingLibrary:
    def get_images(self, uris):
        raise RuntimeError("backend is unreachable")


def test_get_art_url_returns_none_for_no_uri():
    assert core_helpers.get_art_url(core=object(), uri=None) is None


def test_get_art_url_returns_first_image_uri():
    library = _FakeLibrary({"tidal:track:1": (Image(uri="https://example/art.jpg"),)})
    core = type("Core", (), {"library": library})()
    assert core_helpers.get_art_url(core, "tidal:track:1") == "https://example/art.jpg"


def test_get_art_url_returns_none_when_backend_has_no_art():
    library = _FakeLibrary({})
    core = type("Core", (), {"library": library})()
    assert core_helpers.get_art_url(core, "tidal:track:1") is None


def test_get_art_url_degrades_gracefully_on_backend_failure():
    core = type("Core", (), {"library": _FailingLibrary()})()
    assert core_helpers.get_art_url(core, "tidal:track:1") is None


def test_album_name_returns_none_without_album():
    assert core_helpers.album_name(Track(uri="tidal:track:1")) is None
    assert core_helpers.album_name(None) is None


def test_album_name_returns_album_name():
    track = Track(uri="tidal:track:1", album=Album(name="Moon Safari"))
    assert core_helpers.album_name(track) == "Moon Safari"


class _FakePlaybackWithTrack:
    def __init__(self, track, state=PlaybackState.PLAYING):
        self._track = track
        self._state = state

    def get_current_track(self):
        return _ImmediateFuture(self._track)

    def get_state(self):
        return _ImmediateFuture(self._state)


class _FailingPlayback:
    def get_current_track(self):
        return _HangingFuture()

    def get_state(self):
        return _HangingFuture()


def test_get_title_text_prefixes_playing_track_with_status():
    track = Track(uri="tidal:track:1", name="Song", artists=frozenset({Artist(name="Band")}))
    core = type("Core", (), {"playback": _FakePlaybackWithTrack(track, PlaybackState.PLAYING)})()
    assert core_helpers.get_title_text(core) == "Playing: Band - Song"


def test_get_title_text_prefixes_paused_track_with_status():
    track = Track(uri="tidal:track:1", name="Song", artists=frozenset({Artist(name="Band")}))
    core = type("Core", (), {"playback": _FakePlaybackWithTrack(track, PlaybackState.PAUSED)})()
    assert core_helpers.get_title_text(core) == "Paused: Band - Song"


def test_get_title_text_falls_back_to_mopidy_when_stopped():
    track = Track(uri="tidal:track:1", name="Song", artists=frozenset({Artist(name="Band")}))
    core = type("Core", (), {"playback": _FakePlaybackWithTrack(track, PlaybackState.STOPPED)})()
    assert core_helpers.get_title_text(core) == "Mopidy"


def test_get_title_text_falls_back_to_mopidy_when_nothing_playing():
    core = type("Core", (), {"playback": _FakePlaybackWithTrack(None)})()
    assert core_helpers.get_title_text(core) == "Mopidy"


def test_get_title_text_falls_back_to_mopidy_on_timeout():
    core = type("Core", (), {"playback": _FailingPlayback()})()
    assert core_helpers.get_title_text(core) == "Mopidy"


class _FakePlaybackStatus:
    def __init__(self, state, track, time_position):
        self._state = state
        self._track = track
        self._time_position = time_position

    def get_state(self):
        return _ImmediateFuture(self._state)

    def get_current_track(self):
        return _ImmediateFuture(self._track)

    def get_time_position(self):
        return _ImmediateFuture(self._time_position)


def test_get_playback_status_while_playing():
    track = Track(uri="tidal:track:1", name="Song", length=185_000)
    core = type("Core", (), {"playback": _FakePlaybackStatus(PlaybackState.PLAYING, track, 65_000)})()

    status = core_helpers.get_playback_status(core)

    assert status == {
        "state": PlaybackState.PLAYING,
        "is_playing": True,
        "track_name": "Song",
        "track_uri": "tidal:track:1",
        "time_position": "1:05",
        "duration": "3:05",
        "duration_ms": 185_000,
        "progress_percent": 35,
    }


def test_get_playback_status_with_nothing_playing():
    core = type("Core", (), {"playback": _FakePlaybackStatus(PlaybackState.STOPPED, None, 0)})()

    status = core_helpers.get_playback_status(core)

    assert status["is_playing"] is False
    assert status["duration"] == "--:--"
    assert status["progress_percent"] is None


def test_get_playback_status_progress_percent_clamped_at_100():
    # Position can momentarily read past duration right at a track's end.
    track = Track(uri="tidal:track:1", name="Song", length=100_000)
    core = type("Core", (), {"playback": _FakePlaybackStatus(PlaybackState.PLAYING, track, 100_500)})()

    status = core_helpers.get_playback_status(core)

    assert status["progress_percent"] == 100


def test_seconds_until_track_end_while_playing():
    assert core_helpers.seconds_until_track_end(True, 60_000, 185_000) == 125


def test_seconds_until_track_end_none_when_not_playing():
    assert core_helpers.seconds_until_track_end(False, 60_000, 185_000) is None


def test_seconds_until_track_end_none_without_a_track():
    assert core_helpers.seconds_until_track_end(True, 60_000, None) is None


def test_seconds_until_track_end_clamps_to_zero_past_the_end():
    assert core_helpers.seconds_until_track_end(True, 190_000, 185_000) == 0


class _FakeMixer:
    def __init__(self, volume=50, mute=False):
        self._volume = volume
        self._mute = mute

    def get_volume(self):
        return _ImmediateFuture(self._volume)

    def get_mute(self):
        return _ImmediateFuture(self._mute)


class _FakeTracklist:
    def __init__(self, tl_tracks, random_, next_tlid=None):
        self._tl_tracks = tl_tracks
        self._random = random_
        self._next_tlid = next_tlid

    def get_random(self):
        return _ImmediateFuture(self._random)

    def get_tl_tracks(self):
        return _ImmediateFuture(self._tl_tracks)

    def get_next_tlid(self):
        return _ImmediateFuture(self._next_tlid)


class _FakeNowPlayingPlayback:
    def __init__(self, current_tlid, track):
        self._current_tlid = current_tlid
        self._track = track

    def get_state(self):
        return _ImmediateFuture(PlaybackState.PLAYING)

    def get_current_track(self):
        return _ImmediateFuture(self._track)

    def get_time_position(self):
        return _ImmediateFuture(0)

    def get_current_tlid(self):
        return _ImmediateFuture(self._current_tlid)


def _make_tl_track(tlid, uri):
    return type("TlTrack", (), {"tlid": tlid, "track": Track(uri=uri, name=uri)})()


def test_get_now_playing_upcoming_in_order_when_not_shuffled():
    tl_tracks = [_make_tl_track(i, f"t:{i}") for i in range(1, 6)]
    core = type(
        "Core",
        (),
        {
            "playback": _FakeNowPlayingPlayback(current_tlid=2, track=tl_tracks[1].track),
            "mixer": _FakeMixer(),
            "tracklist": _FakeTracklist(tl_tracks, random_=False),
        },
    )()

    upcoming = core_helpers.get_now_playing(core)["upcoming"]

    assert [tl.tlid for tl in upcoming] == [3, 4, 5]


def test_get_now_playing_upcoming_when_shuffled_only_shows_the_confirmed_next_track():
    tl_tracks = [_make_tl_track(i, f"t:{i}") for i in range(1, 6)]
    core = type(
        "Core",
        (),
        {
            "playback": _FakeNowPlayingPlayback(current_tlid=2, track=tl_tracks[1].track),
            "mixer": _FakeMixer(),
            # tracklist order is 1..5, but the real next track (per Mopidy's
            # private shuffle state) is 5 -- upcoming must reflect that, not
            # naively slice the tracklist as if it were sequential.
            "tracklist": _FakeTracklist(tl_tracks, random_=True, next_tlid=5),
        },
    )()

    upcoming = core_helpers.get_now_playing(core)["upcoming"]

    assert [tl.tlid for tl in upcoming] == [5]


def test_get_now_playing_rounds_volume_to_nearest_step_of_ten():
    core = type(
        "Core",
        (),
        {
            "playback": _FakeNowPlayingPlayback(current_tlid=None, track=None),
            "mixer": _FakeMixer(volume=63),
            "tracklist": _FakeTracklist([], random_=False),
        },
    )()

    now_playing = core_helpers.get_now_playing(core)

    assert now_playing["volume"] == 63
    assert now_playing["volume_step"] == 60


def test_get_now_playing_volume_step_none_when_volume_unknown():
    core = type(
        "Core",
        (),
        {
            "playback": _FakeNowPlayingPlayback(current_tlid=None, track=None),
            "mixer": _FakeMixer(volume=None),
            "tracklist": _FakeTracklist([], random_=False),
        },
    )()

    assert core_helpers.get_now_playing(core)["volume_step"] is None


def test_source_label_known_scheme():
    assert core_helpers.source_label("tidal:playlist:123") == "Tidal"
    assert core_helpers.source_label("spotify:track:abc") == "Spotify"


def test_source_label_unknown_scheme_falls_back_to_capitalized():
    assert core_helpers.source_label("soundcloud:track:1") == "Soundcloud"


def test_source_label_none_or_schemeless_uri():
    assert core_helpers.source_label(None) is None
    assert core_helpers.source_label("") is None
    assert core_helpers.source_label("not-a-uri") is None


def test_source_scheme_extracts_prefix():
    assert core_helpers.source_scheme("tidal:playlist:123") == "tidal"


def test_source_scheme_none_or_schemeless_uri():
    assert core_helpers.source_scheme(None) is None
    assert core_helpers.source_scheme("") is None
    assert core_helpers.source_scheme("not-a-uri") is None


