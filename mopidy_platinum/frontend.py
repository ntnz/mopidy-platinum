import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tornado.escape
import tornado.web

from mopidy_platinum import art, core_helpers
from mopidy_platinum.airplay import AirplayReconnector
from mopidy_platinum.background import QueueFiller
from mopidy_platinum.core_helpers import DEFAULT_TIMEOUT, LONG_TIMEOUT, CoreTimeout, get_result
from mopidy_platinum.track_cache import TrackCache

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

CONTROL_ACTIONS = {"play", "pause", "stop", "next", "previous"}

# Validates the status frame's optional bg override before it's echoed into
# an inline bgcolor attribute -- the only caller today (fullscreen.html)
# always passes a fixed, trusted value, but the endpoint is public.
HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{3}$|^[0-9A-Fa-f]{6}$")

# How long after a track is expected to end before refreshing for it -- long
# enough that the reload lands just after the transition (not a hair before
# it, which would just show the same stale track again until the next poll).
TRACK_END_REFRESH_BUFFER = 2
# A full page reload flashes/redraws the whole page on period-appropriate
# browsers -- floor it well above the status frame's own interval so a track
# nearing its end can't turn into a rapid-fire reload loop.
MIN_REFRESH_INTERVAL = 8

# A "Load More" click can only ever grow the page by max_list_items at a
# time, but nothing stops someone from hand-editing ?limit= in the URL --
# this is the hard ceiling on how much a single request will ever resolve,
# regardless of what's asked for.
HARD_MAX_LIMIT = 2000

TIMEOUT_MESSAGE = (
    "That took too long and was cancelled so the rest of the server keeps working. "
    "The backend involved (e.g. a streaming service) may be slow or unreachable right now."
)

# One filler shared by every request -- there's a single Mopidy core to queue
# tracks into, so a second "Play All" simply supersedes whatever the first
# one was still queueing in the background.
queue_filler = QueueFiller()

# Shared so paging deeper into the same long listing only ever resolves the
# newly-revealed tracks; previously-seen ones are already cached.
track_cache = TrackCache()

# One reconnector shared by every request -- there's a single AirPlay receiver
# to reconnect to, so a second click while one is already in flight simply
# supersedes it rather than racing it.
airplay_reconnector = AirplayReconnector()


class BaseHandler(tornado.web.RequestHandler):
    def initialize(
        self,
        core,
        refresh_interval=15,
        max_list_items=200,
        status_refresh_interval=2,
        airplay_device_name=None,
    ):
        self.core = core
        self.refresh_interval = refresh_interval
        self.max_list_items = max_list_items
        self.status_refresh_interval = status_refresh_interval
        self.airplay_device_name = airplay_device_name or None

    def get_template_path(self):
        return str(TEMPLATES_DIR)

    def set_default_headers(self):
        # Old Mac browsers are prone to aggressively caching a page fetched
        # via plain HTTP GET -- with a UI that stays "live" via meta-refresh
        # reloads of that exact same URL, a cached copy shows up as content
        # that looks frozen/stale rather than an obvious cache problem.
        # ArtHandler overrides this afterwards for actual images, which
        # should cache.
        self.set_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.set_header("Pragma", "no-cache")
        self.set_header("Expires", "0")

    def get_template_namespace(self):
        namespace = super().get_template_namespace()
        namespace["track_display_name"] = core_helpers.track_display_name
        namespace["format_time"] = core_helpers.format_time
        namespace["album_name"] = core_helpers.album_name
        namespace["source_label"] = core_helpers.source_label
        namespace["error"] = self.get_query_argument("error", None)
        namespace["title_text"] = core_helpers.get_title_text(self.core)
        # Appended to meta-refresh targets so each reload looks like a new
        # URL to the browser, forcing an actual fetch instead of a cache hit.
        namespace["cache_bust"] = str(int(time.time() * 1000))
        # Rendered as a small footer control on every page (not just Now
        # Playing) -- reconnecting is rare and not something a room full of
        # party guests should be able to stumble into among the main
        # transport buttons, but it still needs to be reachable from
        # wherever you happen to be when the audio actually drops.
        namespace["airplay_device_name"] = self.airplay_device_name
        namespace["airplay_status"] = airplay_reconnector.status() if self.airplay_device_name else None
        # Also surfaced as a small footer control everywhere, next to the
        # AirPlay one -- it's a set-and-forget preference, not something that
        # needs its own prominent spot next to Up Next.
        namespace["live"] = self.get_cookie("live", "1") == "1"
        namespace.setdefault("active_tab", None)
        return namespace

    def get_display_limit(self):
        try:
            limit = int(self.get_query_argument("limit", self.max_list_items))
        except ValueError:
            limit = self.max_list_items
        return max(self.max_list_items, min(limit, HARD_MAX_LIMIT))

    def redirect_back(self, arg_getter, error=None):
        target = arg_getter("return_to", "/platinum/") or "/platinum/"
        scheme, netloc, path, query, fragment = urlsplit(target)
        # return_to is seeded from the page's own request.uri, which -- once
        # that page has auto-refreshed at least once -- carries the old
        # cache-busting "_" param from that reload. Redirecting back to that
        # exact same (already-fetched) URL lets a caching-happy old browser
        # serve its stale copy instead of re-fetching, e.g. showing the art
        # for whatever track was playing before a skip rather than after.
        params = [(k, v) for k, v in parse_qsl(query) if k != "_"]
        if error:
            params.append(("error", error))
        target = urlunsplit((scheme, netloc, path, urlencode(params), fragment))
        self.redirect(target)

    def run_action(self, action, arg_getter):
        """Run a POST handler's core calls, redirecting back with an error on timeout/failure."""
        try:
            action()
        except CoreTimeout:
            self.redirect_back(arg_getter, error=TIMEOUT_MESSAGE)
            return
        except Exception as exc:  # noqa: BLE001 - surface any backend failure to the user
            self.redirect_back(arg_getter, error=f"Action failed: {exc}")
            return
        self.redirect_back(arg_getter)

    def render_page(self, render):
        """Run a GET handler's core calls, rendering an error page on timeout/failure."""
        try:
            render()
        except CoreTimeout:
            self.render("error.html", message=TIMEOUT_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - surface any backend failure to the user
            self.render("error.html", message=f"Something went wrong: {exc}")


def _now_playing_refresh_interval(handler, live, now_playing):
    """How long until the outer Now Playing/full-screen page should reload.

    Aimed at right around when the current track is expected to end -- this
    is the *fallback* path for a natural end; a track that changes for any
    other reason (an external skip via another client/remote) instead gets
    caught by the status frame's own frequent poll telling this page to
    reload early (see NowPlayingStatusHandler).
    """
    if not live:
        return 0
    seconds_left = core_helpers.seconds_until_track_end(
        now_playing["is_playing"], now_playing["time_position_ms"], now_playing["duration_ms"]
    )
    if seconds_left is None:
        return handler.refresh_interval
    return max(
        MIN_REFRESH_INTERVAL,
        min(handler.refresh_interval, seconds_left + TRACK_END_REFRESH_BUFFER),
    )


class NowPlayingHandler(BaseHandler):
    def get(self):
        live_param = self.get_query_argument("live", None)
        if live_param in ("0", "1"):
            self.set_cookie("live", live_param)
            self.redirect("/platinum/")
            return

        def render():
            live = self.get_cookie("live", "1") == "1"
            now_playing = core_helpers.get_now_playing(self.core)
            refresh_interval = _now_playing_refresh_interval(self, live, now_playing)
            self.render(
                "now_playing.html",
                active_tab="now_playing",
                live=live,
                refresh_interval=refresh_interval,
                status_refresh_interval=self.status_refresh_interval,
                **now_playing,
            )

        self.render_page(render)


class FullScreenHandler(BaseHandler):
    """A distraction-free, meant-to-be-viewed-from-across-the-room Now Playing
    screen -- big album art, big track name/progress, big transport buttons,
    no tabs/footer/other chrome.
    """

    def get(self):
        live_param = self.get_query_argument("live", None)
        if live_param in ("0", "1"):
            self.set_cookie("live", live_param)
            self.redirect("/platinum/fullscreen")
            return

        def render():
            live = self.get_cookie("live", "1") == "1"
            now_playing = core_helpers.get_now_playing(self.core)
            refresh_interval = _now_playing_refresh_interval(self, live, now_playing)
            self.render(
                "fullscreen.html",
                live=live,
                refresh_interval=refresh_interval,
                status_refresh_interval=self.status_refresh_interval,
                **now_playing,
            )

        self.render_page(render)


class NowPlayingStatusHandler(BaseHandler):
    """Renders the ticking "Playing -- 1:23 / 3:45" line plus any in-flight
    queue-fill/AirPlay status, in its own frame.

    Kept separate from NowPlayingHandler so the fast refresh needed for a
    smooth time display (and for that transient status) doesn't ever touch
    the album art or the rest of the page -- polling this costs 3 lightweight
    core calls instead of the full now-playing set, since nothing else here
    changes second to second.
    """

    def get(self):
        expected_uri = self.get_query_argument("expected_uri", "")
        bg = self.get_query_argument("bg", "")
        if not HEX_COLOR_RE.match(bg):
            bg = ""

        def render():
            status = core_helpers.get_playback_status(self.core)
            # This frame polls reliably every couple of seconds regardless of
            # *why* the track changed (natural end, a skip from this UI, or
            # an external client/remote the outer page has no way to
            # anticipate) -- so it's the one place that can promptly notice
            # a mismatch and tell the outer page to catch up, rather than the
            # outer page trying to predict in advance when to check.
            track_changed = expected_uri != (status["track_uri"] or "")
            self.render(
                "status_frame.html",
                status_refresh_interval=self.status_refresh_interval,
                fill_status=queue_filler.status(),
                track_changed=track_changed,
                bg=bg,
                # Carried into this frame's own meta-refresh target below --
                # otherwise every reload after the first drops back to a
                # plain /status URL with neither param, silently losing the
                # bg override (and the ability to notice a track change)
                # from the second reload onward.
                expected_uri=expected_uri,
                **status,
            )

        self.render_page(render)


class AirplayReconnectHandler(BaseHandler):
    def post(self):
        def do_it():
            if self.airplay_device_name:
                airplay_reconnector.start(self.core, self.airplay_device_name)

        self.run_action(do_it, self.get_body_argument)


class ControlHandler(BaseHandler):
    def post(self):
        action = self.get_body_argument("action", "")

        def do_it():
            if action in CONTROL_ACTIONS:
                get_result(getattr(self.core.playback, action)(), timeout=LONG_TIMEOUT)

        self.run_action(do_it, self.get_body_argument)


class VolumeHandler(BaseHandler):
    def post(self):
        level = self.get_body_argument("level", None)
        delta = self.get_body_argument("delta", None)
        mute = self.get_body_argument("mute", None)

        def do_it():
            if level is not None:
                get_result(self.core.mixer.set_volume(max(0, min(100, int(level)))))
            elif delta is not None:
                current = get_result(self.core.mixer.get_volume()) or 0
                new_volume = max(0, min(100, current + int(delta)))
                get_result(self.core.mixer.set_volume(new_volume))
            if mute is not None:
                get_result(self.core.mixer.set_mute(mute == "1"))

        self.run_action(do_it, self.get_body_argument)


class SeekHandler(BaseHandler):
    """Backs the click-to-seek progress bar in the status frame.

    A real drag-to-scrub slider risks losing the gesture mid-drag when the
    status frame's own meta-refresh reloads out from under the cursor (every
    couple of seconds) -- a single click is well inside that window and
    submits like every other control here, so it doesn't need any special
    handling for that.
    """

    def post(self):
        position_ms = self.get_body_argument("position_ms", None)

        def do_it():
            if position_ms is not None:
                get_result(self.core.playback.seek(int(position_ms)), timeout=LONG_TIMEOUT)

        self.run_action(do_it, self.get_body_argument)


class ArtHandler(BaseHandler):
    MIN_SIZE = 16
    # Big enough for the full-screen view's large centered art (see
    # FullScreenHandler/fullscreen.html) -- everywhere else still asks for
    # something much smaller.
    MAX_SIZE = 512
    DEFAULT_SIZE = 50

    def get(self):
        uri = self.get_query_argument("uri", None)
        kind = self.get_query_argument("type", "track")
        try:
            size = int(self.get_query_argument("size", self.DEFAULT_SIZE))
        except ValueError:
            size = self.DEFAULT_SIZE
        size = max(self.MIN_SIZE, min(self.MAX_SIZE, size))

        content_type, image_bytes = art.get_thumbnail(self.core, uri, size, kind=kind)
        self.set_header("Content-Type", content_type)
        # Overrides BaseHandler's blanket no-cache defaults -- unlike the
        # pages themselves, art for a given uri/size/type never changes, and
        # old browsers weigh Pragma/Expires at least as heavily as
        # Cache-Control, so all three need to actually say "cacheable" here.
        self.set_header("Cache-Control", "public, max-age=86400")
        self.clear_header("Pragma")
        self.clear_header("Expires")
        self.write(image_bytes)


class ShuffleToggleHandler(BaseHandler):
    def post(self):
        def do_it():
            current = get_result(self.core.tracklist.get_random())
            get_result(self.core.tracklist.set_random(not current))

        self.run_action(do_it, self.get_body_argument)


class BrowseHandler(BaseHandler):
    def get(self):
        uri = self.get_query_argument("uri", None) or None
        name = self.get_query_argument("name", None)
        path_param = self.get_query_argument("path", "")
        crumbs = core_helpers.decode_breadcrumbs(path_param)
        limit = self.get_display_limit()

        def render():
            refs = get_result(self.core.library.browse(uri), timeout=LONG_TIMEOUT)
            refs.sort(key=lambda r: (r.type == "track", (r.name or r.uri or "").lower()))
            visible, total_count = core_helpers.truncate(refs, limit)

            # Browse only gives us lightweight Refs -- no duration/album --
            # so fill those in for just the tracks on this page. The cache
            # means only newly-revealed tracks ever trigger a fresh lookup.
            track_uris = [r.uri for r in visible if r.type == "track"]
            tracks_by_uri = track_cache.get_many(self.core, track_uris) if track_uris else {}

            breadcrumb_links = []
            running = []
            for c_uri, c_name in crumbs:
                breadcrumb_links.append((c_uri, c_name, core_helpers.encode_breadcrumbs(running)))
                running.append((c_uri, c_name))

            if uri is not None:
                child_path = core_helpers.encode_breadcrumbs([*crumbs, (uri, name or uri)])
            else:
                child_path = ""

            load_more_url = None
            if total_count > len(visible):
                params = {"path": path_param, "limit": limit + self.max_list_items}
                if uri is not None:
                    params["uri"] = uri
                if name is not None:
                    params["name"] = name
                load_more_url = "/platinum/browse?" + urlencode(params)

            self.render(
                "browse.html",
                active_tab="browse",
                current_name=name,
                breadcrumb_links=breadcrumb_links,
                child_path=child_path,
                refs=visible,
                tracks_by_uri=tracks_by_uri,
                total_count=total_count,
                load_more_url=load_more_url,
            )

        self.render_page(render)


class PlaylistsHandler(BaseHandler):
    def get(self):
        limit = self.get_display_limit()
        selected_source = self.get_query_argument("source", "") or None

        def render():
            playlists = get_result(self.core.playlists.as_list(), timeout=LONG_TIMEOUT)
            playlists.sort(key=lambda r: (r.name or r.uri or "").lower())

            # Every source actually present, not every source configured --
            # no point offering a filter option that would just show an empty
            # list. Sorted by label so the dropdown order doesn't depend on
            # whatever order backends happened to respond in.
            available_sources = sorted(
                {
                    (scheme, core_helpers.SOURCE_LABELS.get(scheme, scheme.capitalize()))
                    for scheme in (core_helpers.source_scheme(p.uri) for p in playlists)
                    if scheme
                },
                key=lambda pair: pair[1].lower(),
            )

            if selected_source:
                playlists = [p for p in playlists if core_helpers.source_scheme(p.uri) == selected_source]

            visible, total_count = core_helpers.truncate(playlists, limit)

            load_more_url = None
            if total_count > len(visible):
                params = {"limit": limit + self.max_list_items}
                if selected_source:
                    params["source"] = selected_source
                load_more_url = "/platinum/playlists?" + urlencode(params)

            self.render(
                "playlists.html",
                active_tab="playlists",
                playlists=visible,
                total_count=total_count,
                load_more_url=load_more_url,
                available_sources=available_sources,
                selected_source=selected_source,
            )

        self.render_page(render)


class PlaylistHandler(BaseHandler):
    def get(self):
        uri = self.get_query_argument("uri")
        name = self.get_query_argument("name", uri)
        limit = self.get_display_limit()

        def render():
            # lookup() (unlike get_items()) returns full Track objects, so
            # duration/album come for free -- no extra per-page lookup needed.
            playlist = get_result(self.core.playlists.lookup(uri), timeout=LONG_TIMEOUT)
            tracks = list(playlist.tracks) if playlist else []
            visible, total_count = core_helpers.truncate(tracks, limit)

            load_more_url = None
            if total_count > len(visible):
                params = {"uri": uri, "name": name, "limit": limit + self.max_list_items}
                load_more_url = "/platinum/playlist?" + urlencode(params)

            self.render(
                "playlist.html",
                active_tab="playlists",
                uri=uri,
                name=name,
                tracks=visible,
                total_count=total_count,
                load_more_url=load_more_url,
            )

        self.render_page(render)


class PlaylistPlayHandler(BaseHandler):
    def post(self):
        uri = self.get_body_argument("uri")
        name = self.get_body_argument("name", uri)
        shuffle = self.get_body_argument("shuffle", "0") == "1"
        playlist_url = f"/platinum/playlist?uri={tornado.escape.url_escape(uri)}"

        try:
            items = get_result(self.core.playlists.get_items(uri), timeout=LONG_TIMEOUT) or []
            uris = [ref.uri for ref in items]
            if shuffle:
                random.shuffle(uris)

            get_result(self.core.tracklist.clear(), timeout=LONG_TIMEOUT)

            if uris:
                # Queue and play the first track immediately so this feels instant
                # no matter how big the playlist is, then fill in the rest -- a
                # handful at a time, in the background -- so a huge playlist (Tidal
                # in particular resolves every URI with its own lookup) never makes
                # one call big enough to tie up the whole server.
                first_track = get_result(self.core.tracklist.add(uris=[uris[0]]), timeout=LONG_TIMEOUT)
                if first_track:
                    get_result(self.core.playback.play(tlid=first_track[0].tlid), timeout=LONG_TIMEOUT)
                queue_filler.start(self.core, uris[1:], name)
        except CoreTimeout:
            self.redirect(f"{playlist_url}&error={tornado.escape.url_escape(TIMEOUT_MESSAGE)}")
            return
        except Exception as exc:  # noqa: BLE001 - surface any backend failure to the user
            self.redirect(f"{playlist_url}&error={tornado.escape.url_escape(f'Action failed: {exc}')}")
            return

        self.redirect("/platinum/")


class SearchHandler(BaseHandler):
    VALID_FIELDS = {"any", "track_name", "artist", "album"}

    def get(self):
        q = self.get_query_argument("q", "").strip()
        field = self.get_query_argument("field", "any")
        if field not in self.VALID_FIELDS:
            field = "any"
        limit = self.get_display_limit()

        def render():
            tracks = []
            if q:
                results = get_result(self.core.library.search({field: [q]}), timeout=LONG_TIMEOUT)
                seen = set()
                for result in results:
                    for track in result.tracks:
                        if track.uri not in seen:
                            seen.add(track.uri)
                            tracks.append(track)

            visible, total_count = core_helpers.truncate(tracks, limit)

            load_more_url = None
            if total_count > len(visible):
                params = {"q": q, "field": field, "limit": limit + self.max_list_items}
                load_more_url = "/platinum/search?" + urlencode(params)

            self.render(
                "search.html",
                active_tab="search",
                q=q,
                field=field,
                tracks=visible,
                total_count=total_count,
                load_more_url=load_more_url,
            )

        self.render_page(render)


class QueueHandler(BaseHandler):
    def get(self):
        limit = self.get_display_limit()

        def render():
            tl_tracks = get_result(self.core.tracklist.get_tl_tracks())
            current_tlid = get_result(self.core.playback.get_current_tlid())
            visible, total_count = core_helpers.truncate(tl_tracks, limit)

            load_more_url = None
            if total_count > len(visible):
                load_more_url = "/platinum/queue?" + urlencode({"limit": limit + self.max_list_items})

            self.render(
                "queue.html",
                active_tab="queue",
                tl_tracks=visible,
                current_tlid=current_tlid,
                total_count=total_count,
                load_more_url=load_more_url,
            )

        self.render_page(render)


class PlayHandler(BaseHandler):
    """Queues a single track to play next -- right after whatever's currently
    playing -- without touching anything already lined up after that.

    With a big playlist queued up, this is what makes it possible to slip a
    request in without derailing it: nothing already playing gets
    interrupted, and nothing further down the queue gets bumped except by
    one slot. Falls back to playing the track immediately if nothing's
    currently playing, since there's nothing to preserve in that case.
    """

    def post(self):
        uri = self.get_body_argument("uri")

        def do_it():
            current_tlid = get_result(self.core.playback.get_current_tlid(), timeout=LONG_TIMEOUT)
            at_position = None
            if current_tlid is not None:
                tl_tracks = get_result(self.core.tracklist.get_tl_tracks(), timeout=LONG_TIMEOUT)
                index = next((i for i, t in enumerate(tl_tracks) if t.tlid == current_tlid), None)
                if index is not None:
                    at_position = index + 1

            new_tl_tracks = get_result(
                self.core.tracklist.add(uris=[uri], at_position=at_position), timeout=LONG_TIMEOUT
            )
            if current_tlid is None and new_tl_tracks:
                get_result(self.core.playback.play(tlid=new_tl_tracks[0].tlid), timeout=LONG_TIMEOUT)

        self.run_action(do_it, self.get_body_argument)


class QueueAddHandler(BaseHandler):
    def post(self):
        uri = self.get_body_argument("uri")

        def do_it():
            get_result(self.core.tracklist.add(uris=[uri]), timeout=LONG_TIMEOUT)

        self.run_action(do_it, self.get_body_argument)


class QueuePlayHandler(BaseHandler):
    def post(self):
        tlid = int(self.get_body_argument("tlid"))

        def do_it():
            get_result(self.core.playback.play(tlid=tlid), timeout=LONG_TIMEOUT)

        self.run_action(do_it, self.get_body_argument)


class QueueRemoveHandler(BaseHandler):
    def post(self):
        tlid = int(self.get_body_argument("tlid"))

        def do_it():
            get_result(self.core.tracklist.remove({"tlid": [tlid]}))

        self.run_action(do_it, self.get_body_argument)


class QueueClearHandler(BaseHandler):
    def post(self):
        def do_it():
            queue_filler.cancel()
            get_result(self.core.tracklist.clear())

        self.run_action(do_it, self.get_body_argument)


def factory(config, core):
    platinum_config = config["platinum"]
    init = {
        "core": core,
        "refresh_interval": platinum_config["refresh_interval"],
        "max_list_items": platinum_config["max_list_items"],
        "status_refresh_interval": platinum_config.get("status_refresh_interval", 2),
        "airplay_device_name": platinum_config.get("airplay_device_name"),
    }
    return [
        (r"/", NowPlayingHandler, init),
        (r"/fullscreen", FullScreenHandler, init),
        (r"/status", NowPlayingStatusHandler, init),
        (r"/airplay/reconnect", AirplayReconnectHandler, init),
        (r"/control", ControlHandler, init),
        (r"/volume", VolumeHandler, init),
        (r"/seek", SeekHandler, init),
        (r"/shuffle", ShuffleToggleHandler, init),
        (r"/art", ArtHandler, init),
        (r"/browse", BrowseHandler, init),
        (r"/playlists", PlaylistsHandler, init),
        (r"/playlist", PlaylistHandler, init),
        (r"/playlist/play", PlaylistPlayHandler, init),
        (r"/search", SearchHandler, init),
        (r"/queue", QueueHandler, init),
        (r"/play", PlayHandler, init),
        (r"/queue/add", QueueAddHandler, init),
        (r"/queue/play", QueuePlayHandler, init),
        (r"/queue/remove", QueueRemoveHandler, init),
        (r"/queue/clear", QueueClearHandler, init),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": str(STATIC_DIR)}),
    ]
