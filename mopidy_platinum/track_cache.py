"""Caches resolved Track metadata (duration, album) by uri.

Browse only returns lightweight Refs (uri/name/type, no duration or album --
unlike Search and Playlist, which already carry full Track objects for
free). Getting that metadata for display requires a separate
library.lookup() call, and a browse listing can be up to a page's worth of
tracks at once. Caching means paging deeper into a long listing only ever
looks up the newly-revealed tracks -- previously-seen ones are free -- so a
single request never has to resolve more than one page's worth in one shot.
"""

from collections import OrderedDict

from mopidy_platinum.core_helpers import LONG_TIMEOUT, get_result

CACHE_MAX_ENTRIES = 2000


class TrackCache:
    def __init__(self, max_entries=CACHE_MAX_ENTRIES):
        self._cache = OrderedDict()
        self.max_entries = max_entries

    def get_many(self, core, uris):
        """Return {uri: Track|None} for the given uris, resolving cache misses
        with a single bounded library.lookup() call for just the misses.
        """
        result = {}
        missing = []
        for uri in uris:
            if uri in self._cache:
                self._cache.move_to_end(uri)
                result[uri] = self._cache[uri]
            else:
                missing.append(uri)

        if missing:
            try:
                resolved = get_result(core.library.lookup(uris=missing), timeout=LONG_TIMEOUT)
            except Exception:
                # Leave these uncached so a transient backend hiccup gets
                # retried on the next request instead of being stuck forever.
                resolved = None
            if resolved is not None:
                for uri in missing:
                    tracks = resolved.get(uri) or ()
                    track = tracks[0] if tracks else None
                    self._cache[uri] = track
                    self._cache.move_to_end(uri)
                    result[uri] = track

        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)

        return result
