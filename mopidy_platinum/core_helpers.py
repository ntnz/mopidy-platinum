from urllib.parse import quote, unquote

import pykka
from mopidy.types import PlaybackState

# Backends like Tidal make network calls under the hood. Mopidy's core is a
# single-threaded actor, and our Tornado handlers block on it directly, so a
# slow/unreachable backend can otherwise freeze the *entire* HTTP server
# (every extension, not just this one) forever. Bounding every call here
# turns that into a timely error message instead of a hang.
DEFAULT_TIMEOUT = 10
LONG_TIMEOUT = 25

# The title bar is decorative and rendered on every single page, so it gets
# its own short timeout -- if core is busy (e.g. mid background fill), fail
# fast and fall back rather than making every page wait on it.
TITLE_TIMEOUT = 3


class CoreTimeout(Exception):
    """A call into Mopidy core took too long to respond."""


def get_result(future, timeout=DEFAULT_TIMEOUT):
    try:
        return future.get(timeout=timeout)
    except pykka.Timeout as exc:
        raise CoreTimeout from exc


def truncate(items, limit):
    """Cap a list for display/bulk-operation purposes, returning (visible, total_count).

    Old browsers choke on huge pages, and backends like Tidal resolve each
    track with its own lookup, so handing core a list of thousands of URIs in
    one call can tie it up (and therefore the whole HTTP server) for minutes.
    """
    return items[:limit], len(items)


def format_time(ms):
    if ms is None:
        return "--:--"
    total_seconds = int(ms) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def track_display_name(track):
    if track is None:
        return None
    artists = ", ".join(sorted(a.name for a in track.artists if a.name))
    name = track.name or track.uri
    return f"{artists} - {name}" if artists else name


def get_art_url(core, uri):
    """Best-effort album art URL lookup for a single track/album/playlist URI.

    Never raises -- art is a nice-to-have, not something that should ever
    break a page. Takes a plain uri (not a Track) so it works for any listing
    row, not just a fully-resolved current track.
    """
    if uri is None:
        return None
    try:
        images = get_result(core.library.get_images([uri]), timeout=LONG_TIMEOUT)
    except Exception:
        return None
    candidates = images.get(uri) or ()
    return candidates[0].uri if candidates else None


def album_name(track):
    if track is None or track.album is None:
        return None
    return track.album.name


# Mopidy URIs are scheme-prefixed by the backend that produced them
# (e.g. "tidal:playlist:123"), so once multiple backends are configured
# this is how we tell their playlists/tracks apart after core has merged
# them into one list -- no extra backend calls needed.
SOURCE_LABELS = {
    "tidal": "Tidal",
    "spotify": "Spotify",
    "local": "Local",
    "m3u": "M3U",
    "file": "File",
}


def source_scheme(uri):
    if not uri or ":" not in uri:
        return None
    return uri.split(":", 1)[0]


def source_label(uri):
    scheme = source_scheme(uri)
    return SOURCE_LABELS.get(scheme, scheme.capitalize()) if scheme else None


def get_title_text(core):
    """Best-effort "Playing: Artist - Song" summary for the html <title>.

    The track name lives only here now (not in the page body), so the
    browser's own title bar/tab -- always visible, even while looking at
    another tab -- is what tells you what's playing. Rendered on every page,
    so this must never be slow or break anything -- falls back to "Mopidy" if
    nothing's playing or the lookup doesn't come back quickly.
    """
    try:
        state = get_result(core.playback.get_state(), timeout=TITLE_TIMEOUT)
        track = get_result(core.playback.get_current_track(), timeout=TITLE_TIMEOUT)
    except Exception:
        return "Mopidy"
    name = track_display_name(track)
    if not name or state == PlaybackState.STOPPED:
        return "Mopidy"
    prefix = "Paused" if state == PlaybackState.PAUSED else "Playing"
    return f"{prefix}: {name}"


def get_playback_status(core):
    """Just enough state to render the ticking time-position line.

    Used on every poll of the fast-refreshing status frame, so it only makes
    the 3 core calls that line actually needs instead of get_now_playing's
    full set (mixer, tracklist, etc.) -- those don't change every second and
    would be wasted round-trips into the single-threaded core actor.
    """
    state = get_result(core.playback.get_state())
    track = get_result(core.playback.get_current_track())
    time_position = get_result(core.playback.get_time_position())
    duration_ms = track.length if track else None
    progress_percent = None
    if time_position is not None and duration_ms:
        progress_percent = max(0, min(100, round(time_position / duration_ms * 100)))
    return {
        "state": state,
        "is_playing": state == PlaybackState.PLAYING,
        "track_name": track_display_name(track),
        "time_position": format_time(time_position),
        "duration": format_time(duration_ms),
        # Raw ms alongside the formatted string, needed client-side to turn a
        # click position on the progress bar into a seek target.
        "duration_ms": duration_ms,
        # Rendered as a table-based bar (no CSS3 gradients/box-shadow needed)
        # right in the fast-refreshing status frame -- None (no bar at all)
        # whenever there's nothing to show progress through.
        "progress_percent": progress_percent,
    }


def get_now_playing(core):
    state = get_result(core.playback.get_state())
    track = get_result(core.playback.get_current_track())
    time_position = get_result(core.playback.get_time_position())
    volume = get_result(core.mixer.get_volume())
    mute = get_result(core.mixer.get_mute())
    is_random = get_result(core.tracklist.get_random())
    current_tlid = get_result(core.playback.get_current_tlid())
    tl_tracks = get_result(core.tracklist.get_tl_tracks())

    if is_random:
        # The actual shuffled play order is private state inside Mopidy's
        # core actor (TracklistController._shuffled) -- there's no public API
        # for the full upcoming sequence, only a single "what plays next" via
        # get_next_tlid(). Slicing tl_tracks here would just show the
        # tracklist's original order, which random mode ignores entirely.
        next_tlid = get_result(core.tracklist.get_next_tlid())
        upcoming = [tl_track for tl_track in tl_tracks if tl_track.tlid == next_tlid][:1]
    elif current_tlid is not None:
        index = next(
            (i for i, tl_track in enumerate(tl_tracks) if tl_track.tlid == current_tlid),
            None,
        )
        upcoming = tl_tracks[index + 1 : index + 6] if index is not None else []
    else:
        upcoming = tl_tracks[:5]

    return {
        "state": state,
        "is_playing": state == PlaybackState.PLAYING,
        "track_name": track_display_name(track),
        "time_position": format_time(time_position),
        "duration": format_time(track.length if track else None),
        # Raw milliseconds alongside the formatted strings above, so the page
        # can schedule its own refresh for right around when the track is
        # expected to end instead of reformatting "1:05" back into a number.
        "time_position_ms": time_position,
        "duration_ms": track.length if track else None,
        "volume": volume,
        # Rounded to the nearest 10 -- the level a single auto-submitting
        # <select> click can set is that granular, so this is what should
        # show as selected rather than requiring an exact match.
        "volume_step": None if volume is None else int(round(volume / 10.0)) * 10,
        "mute": mute,
        "is_random": is_random,
        "track_uri": track.uri if track else None,
        "upcoming": upcoming,
    }


def seconds_until_track_end(is_playing, time_position_ms, duration_ms):
    """Whole seconds until the current track is expected to finish, or None if
    that can't be predicted (nothing playing, or position/duration unknown).
    """
    if not is_playing or time_position_ms is None or duration_ms is None:
        return None
    return max(0, (duration_ms - time_position_ms) // 1000)


def encode_breadcrumbs(crumbs):
    """crumbs: list of (uri, name) ancestor pairs -> opaque path string."""
    return "|".join(f"{quote(uri, safe='')}:{quote(name or '', safe='')}" for uri, name in crumbs)


def decode_breadcrumbs(path_param):
    crumbs = []
    if not path_param:
        return crumbs
    for part in path_param.split("|"):
        if ":" not in part:
            continue
        uri_part, name_part = part.split(":", 1)
        crumbs.append((unquote(uri_part), unquote(name_part)))
    return crumbs
