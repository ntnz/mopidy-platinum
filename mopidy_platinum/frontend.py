import random
from pathlib import Path
from urllib.parse import urlencode

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

# How long after a track is expected to end before refreshing for it -- long
# enough that the reload lands just after the transition (not a hair before
# it, which would just show the same stale track again until the next poll).
TRACK_END_REFRESH_BUFFER = 2
MIN_REFRESH_INTERVAL = 2

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

    def get_template_namespace(self):
        namespace = super().get_template_namespace()
        namespace["track_display_name"] = core_helpers.track_display_name
        namespace["format_time"] = core_helpers.format_time
        namespace["album_name"] = core_helpers.album_name
        namespace["source_label"] = core_helpers.source_label
        namespace["error"] = self.get_query_argument("error", None)
        namespace["title_text"] = core_helpers.get_title_text(self.core)
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
        if error:
            separator = "&" if "?" in target else "?"
            target = f"{target}{separator}error={tornado.escape.url_escape(error)}"
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
            fill_status = queue_filler.status()
            airplay_status = airplay_reconnector.status()
            if live:
                # Refresh faster while a big playlist is still queueing, or an
                # AirPlay reconnect is in flight, so progress actually looks
                # like it's moving. The ticking time position itself doesn't
                # need this -- that lives in its own fast-refreshing frame.
                airplay_in_progress = bool(airplay_status and airplay_status["phase"])
                if fill_status or airplay_in_progress:
                    refresh_interval = min(self.refresh_interval, 3)
                else:
                    # Otherwise, aim the refresh at right around when the
                    # current track is expected to end -- art/track name/queue
                    # update almost immediately on a natural track change,
                    # instead of waiting out the full interval every time.
                    seconds_left = core_helpers.seconds_until_track_end(
                        now_playing["is_playing"], now_playing["time_position_ms"], now_playing["duration_ms"]
                    )
                    if seconds_left is None:
                        refresh_interval = self.refresh_interval
                    else:
                        refresh_interval = max(
                            MIN_REFRESH_INTERVAL,
                            min(self.refresh_interval, seconds_left + TRACK_END_REFRESH_BUFFER),
                        )
            else:
                refresh_interval = 0
            self.render(
                "now_playing.html",
                active_tab="now_playing",
                live=live,
                refresh_interval=refresh_interval,
                status_refresh_interval=self.status_refresh_interval,
                fill_status=fill_status,
                airplay_status=airplay_status,
                airplay_device_name=self.airplay_device_name,
                **now_playing,
            )

        self.render_page(render)


class NowPlayingStatusHandler(BaseHandler):
    """Renders just the ticking "Playing -- 1:23 / 3:45" line, in its own frame.

    Kept separate from NowPlayingHandler so the fast refresh needed for a
    smooth time display doesn't ever touch the album art or the rest of the
    page -- polling this costs 3 lightweight core calls instead of the full
    now-playing set, since nothing else here changes second to second.
    """

    def get(self):
        def render():
            status = core_helpers.get_playback_status(self.core)
            self.render(
                "status_frame.html",
                status_refresh_interval=self.status_refresh_interval,
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
        delta = self.get_body_argument("delta", None)
        mute = self.get_body_argument("mute", None)

        def do_it():
            if delta is not None:
                current = get_result(self.core.mixer.get_volume()) or 0
                new_volume = max(0, min(100, current + int(delta)))
                get_result(self.core.mixer.set_volume(new_volume))
            if mute is not None:
                get_result(self.core.mixer.set_mute(mute == "1"))

        self.run_action(do_it, self.get_body_argument)


class ArtHandler(BaseHandler):
    MIN_SIZE = 16
    MAX_SIZE = 128
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

        def render():
            playlists = get_result(self.core.playlists.as_list(), timeout=LONG_TIMEOUT)
            playlists.sort(key=lambda r: (r.name or r.uri or "").lower())
            visible, total_count = core_helpers.truncate(playlists, limit)

            load_more_url = None
            if total_count > len(visible):
                load_more_url = "/platinum/playlists?" + urlencode({"limit": limit + self.max_list_items})

            self.render(
                "playlists.html",
                active_tab="playlists",
                playlists=visible,
                total_count=total_count,
                load_more_url=load_more_url,
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
    def post(self):
        uri = self.get_body_argument("uri")

        def do_it():
            tl_tracks = get_result(self.core.tracklist.add(uris=[uri]), timeout=LONG_TIMEOUT)
            if tl_tracks:
                get_result(self.core.playback.play(tlid=tl_tracks[0].tlid), timeout=LONG_TIMEOUT)

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
        (r"/status", NowPlayingStatusHandler, init),
        (r"/airplay/reconnect", AirplayReconnectHandler, init),
        (r"/control", ControlHandler, init),
        (r"/volume", VolumeHandler, init),
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
