"""Recovers AirPlay/RAOP playback after the receiver drops it (e.g. by switching
its input to something else).

A stale RAOP connection doesn't reliably self-heal: PipeWire's raop-discover
module only re-announces receivers on its own schedule, and just killing the
dead sink node leaves nothing to reconnect to (confirmed live -- Mopidy's
output fell back to the local analog sink and stayed there). Restarting the
audio stack is what actually forces a fresh mDNS discovery pass and a new RAOP
handshake with the receiver, which is also what makes most AirPlay receivers
switch their input back on their own.

That restart also severs Mopidy's own GStreamer pipeline -- confirmed live, it
throws "pa_stream_writable_size() failed: Connection terminated" and never
recovers on its own, leaving Mopidy silent even once the receiver is back.
So once the receiver reappears, this points PipeWire's default sink at it and
then cycles Mopidy's own playback (stop, then play if it was playing before),
which rebuilds Mopidy's pipeline against the new default -- confirmed live to
be enough, no full Mopidy restart needed.

All of this is genuinely slow (a service restart plus waiting for the
receiver to reappear on the network can take several seconds), so it runs as
a chain of scheduled IOLoop callbacks -- like QueueFiller in background.py --
rather than blocking the single-threaded Tornado server for the whole
sequence.
"""

import json
import logging
import subprocess

import tornado.ioloop
from mopidy.types import PlaybackState

from mopidy_platinum.core_helpers import LONG_TIMEOUT, CoreTimeout, get_result

logger = logging.getLogger(__name__)

AUDIO_SERVICES = ("pipewire.service", "pipewire-pulse.service", "wireplumber.service")
POLL_INTERVAL = 0.5
DEVICE_WAIT_ATTEMPTS = 30  # ~15s at POLL_INTERVAL


def _run(cmd, timeout):
    return subprocess.run(cmd, check=True, timeout=timeout, capture_output=True, text=True)


def _find_sink_name(device_name):
    """The pactl sink name PipeWire gives a rediscovered RAOP receiver, or None."""
    out = _run(["pactl", "-f", "json", "list", "sinks"], timeout=5).stdout
    prefix = f"raop_sink.{device_name}."
    for sink in json.loads(out):
        if sink.get("name", "").startswith(prefix):
            return sink["name"]
    return None


class AirplayReconnector:
    def __init__(self):
        self.generation = 0
        self.phase = None
        self.error = None

    @property
    def in_progress(self):
        return self.phase is not None

    def status(self):
        # None once idle with nothing to report; otherwise either a phase
        # (still working) or a lingering error from the last attempt (kept
        # around until the next start() so a failure survives the next poll
        # of the page, not just the request that happened to see it first).
        if self.phase is None and self.error is None:
            return None
        return {"phase": self.phase, "error": self.error}

    def start(self, core, device_name):
        """(Re)start the reconnect sequence for `device_name`, superseding any in-progress one."""
        self.generation += 1
        generation = self.generation
        self.phase = "restarting audio service"
        self.error = None
        tornado.ioloop.IOLoop.current().call_later(0, self._restart_audio, generation, core, device_name)

    def _fail(self, generation, message):
        if generation != self.generation:
            return
        logger.warning("AirPlay reconnect failed: %s", message)
        self.phase = None
        self.error = message

    def _succeed(self, generation):
        if generation != self.generation:
            return
        self.phase = None
        self.error = None

    def _restart_audio(self, generation, core, device_name):
        if generation != self.generation:
            return
        try:
            # Captured before the restart so we know whether to resume
            # playback afterwards -- core still reports its last-known state
            # even once the underlying GStreamer pipeline has silently died.
            was_playing = get_result(core.playback.get_state(), timeout=LONG_TIMEOUT) == PlaybackState.PLAYING
        except CoreTimeout as exc:
            self._fail(generation, f"Couldn't check playback state: {exc}")
            return

        try:
            _run(["systemctl", "--user", "restart", *AUDIO_SERVICES], timeout=20)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            self._fail(generation, f"Couldn't restart the audio service: {exc}")
            return
        self.phase = f"waiting for {device_name} to reappear"
        self._wait_for_device(generation, core, device_name, was_playing, attempt=0)

    def _wait_for_device(self, generation, core, device_name, was_playing, attempt):
        if generation != self.generation:
            return
        try:
            sink_name = _find_sink_name(device_name)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            self._fail(generation, f"Couldn't check for {device_name}: {exc}")
            return

        if sink_name is not None:
            self._relink(generation, core, sink_name, was_playing)
            return

        if attempt >= DEVICE_WAIT_ATTEMPTS:
            self._fail(generation, f"{device_name} didn't reappear on the network in time.")
            return

        tornado.ioloop.IOLoop.current().call_later(
            POLL_INTERVAL, self._wait_for_device, generation, core, device_name, was_playing, attempt + 1
        )

    def _relink(self, generation, core, sink_name, was_playing):
        try:
            _run(["pactl", "set-default-sink", sink_name], timeout=5)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            self._fail(generation, f"{sink_name} is back, but couldn't make it the default: {exc}")
            return

        try:
            # Rebuilds Mopidy's own GStreamer pipeline against the new
            # default sink -- it doesn't reconnect on its own once its old
            # PipeWire connection is severed by the service restart above.
            get_result(core.playback.stop(), timeout=LONG_TIMEOUT)
            if was_playing:
                get_result(core.playback.play(), timeout=LONG_TIMEOUT)
        except CoreTimeout as exc:
            self._fail(generation, f"Reconnected, but couldn't get Mopidy's output going again: {exc}")
            return
        self._succeed(generation)
