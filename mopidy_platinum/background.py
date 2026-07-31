"""Fills the tracklist with a large batch of URIs a little at a time.

Mopidy's core is single-threaded, and backends like Tidal resolve every URI
with its own lookup, so handing core thousands of URIs in one call ties up
the whole HTTP server for as long as that takes. Splitting the work into
small batches scheduled on the IOLoop lets everything else -- including the
track that's already playing -- keep working while the rest of a huge
playlist queues up in the background.
"""

import logging

import tornado.ioloop

from mopidy_platinum.core_helpers import LONG_TIMEOUT, get_result

logger = logging.getLogger(__name__)

BATCH_SIZE = 40
BATCH_DELAY = 0.05


class QueueFiller:
    def __init__(self):
        self.generation = 0
        self.remaining = []
        self.total = 0
        self.queued = 0
        self.playlist_name = None

    @property
    def in_progress(self):
        return bool(self.remaining)

    def status(self):
        if not self.in_progress:
            return None
        return {
            "playlist_name": self.playlist_name,
            "queued": self.queued,
            "total": self.total,
        }

    def cancel(self):
        """Stop any in-progress fill (e.g. the user cleared the queue by hand)."""
        self.generation += 1
        self.remaining = []

    def start(self, core, uris, playlist_name):
        """Replace any in-progress fill and start queueing `uris` in the background."""
        self.generation += 1
        generation = self.generation
        self.remaining = list(uris)
        self.total = len(uris)
        self.queued = 0
        self.playlist_name = playlist_name
        if self.remaining:
            self._schedule(core, generation)

    def _schedule(self, core, generation):
        tornado.ioloop.IOLoop.current().call_later(BATCH_DELAY, self._run_batch, core, generation)

    def _run_batch(self, core, generation):
        if generation != self.generation or not self.remaining:
            return
        batch, self.remaining = self.remaining[:BATCH_SIZE], self.remaining[BATCH_SIZE:]
        try:
            get_result(core.tracklist.add(uris=batch), timeout=LONG_TIMEOUT)
        except Exception:
            # A single flaky track/batch shouldn't stop the rest of the playlist.
            logger.warning("Skipped a batch of %d tracks while filling the queue", len(batch), exc_info=True)
        self.queued += len(batch)
        if generation == self.generation and self.remaining:
            self._schedule(core, generation)
