"""Fetches, resizes, and caches album art thumbnails.

Album art URLs (Tidal's CDN, in particular) point at full-resolution images
that are expensive to ship to a decades-old browser on a slow connection --
and a listing page might reference a couple hundred of them at once. This
resizes everything down to a small baseline JPEG server-side, and caches the
result so the same track's art isn't re-fetched/re-resized on every page
view. Each lookup is for exactly one uri, so unlike a bulk metadata/track
resolution, this never risks tying up core (or the HTTP server) for long --
worst case is one bounded fetch, and it always degrades to a placeholder
rather than breaking the page it's embedded in.
"""

import base64
import logging
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image as PILImage

from mopidy_platinum.core_helpers import get_art_url

logger = logging.getLogger(__name__)

CACHE_MAX_ENTRIES = 500
FETCH_TIMEOUT = 5

STATIC_DIR = Path(__file__).parent / "static"

# Drawn OS9-style placeholders, one per kind of thing a ref can be -- served
# whenever art can't be resolved so a page full of thumbnails never turns
# into a wall of broken images/blank white boxes. Kept as source RGBA images
# and resized on demand (like real art) so they look right at any requested
# size.
_PLACEHOLDER_SOURCES = {
    "track": PILImage.open(STATIC_DIR / "icon-track.png").convert("RGBA"),
    "directory": PILImage.open(STATIC_DIR / "icon-folder.png").convert("RGBA"),
    "album": PILImage.open(STATIC_DIR / "icon-folder.png").convert("RGBA"),
    "artist": PILImage.open(STATIC_DIR / "icon-folder.png").convert("RGBA"),
    "playlist": PILImage.open(STATIC_DIR / "icon-playlist.png").convert("RGBA"),
}


def _placeholder(kind, size):
    source = _PLACEHOLDER_SOURCES.get(kind, _PLACEHOLDER_SOURCES["track"])
    image = source.resize((size, size), PILImage.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "image/png", buffer.getvalue()


class ArtCache:
    def __init__(self, max_entries=CACHE_MAX_ENTRIES):
        self._cache = OrderedDict()
        self.max_entries = max_entries

    def get(self, key):
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return self._cache[key]

    def set(self, key, value):
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)


_cache = ArtCache()


def _fetch_bytes(image_url):
    if image_url.startswith("data:"):
        _, _, data = image_url.partition(",")
        return base64.b64decode(data)
    if image_url.startswith(("http://", "https://")):
        response = httpx.get(image_url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return response.content
    msg = f"Unsupported image URI scheme: {image_url}"
    raise ValueError(msg)


def _resize(raw_bytes, size):
    with PILImage.open(BytesIO(raw_bytes)) as image:
        image = image.convert("RGB")
        image.thumbnail((size, size))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=70)
        return "image/jpeg", buffer.getvalue()


def get_thumbnail(core, uri, size, kind="track"):
    """Return (content_type, bytes) for a uri's art, resized to fit size x size.

    Always succeeds -- falls back to a drawn placeholder (picked by `kind`,
    e.g. a folder icon for a browse directory) if art can't be found,
    fetched, or decoded.
    """
    cache_key = (uri, size, kind)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    result = _placeholder(kind, size)
    try:
        image_url = get_art_url(core, uri)
        if image_url:
            result = _resize(_fetch_bytes(image_url), size)
    except Exception:
        logger.warning("Could not fetch/resize art for %s", uri, exc_info=True)
        result = _placeholder(kind, size)

    _cache.set(cache_key, result)
    return result
