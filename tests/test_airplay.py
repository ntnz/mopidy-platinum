import subprocess

from mopidy.types import PlaybackState

from mopidy_platinum import airplay
from mopidy_platinum.airplay import AirplayReconnector

SINKS_WITH_DEVICE = '[{"name": "raop_sink.VSX-528.local.192.168.1.115.1024"}, {"name": "alsa_output.foo"}]'
SINKS_WITHOUT_DEVICE = '[{"name": "alsa_output.foo"}]'


class _FakeCompletedProcess:
    def __init__(self, stdout=""):
        self.stdout = stdout


class _RecordingRun:
    """Fake subprocess.run: dispatches on the full argv, recording every call made."""

    def __init__(self, stdout_by_argv0, raise_for_argv0=None):
        self.stdout_by_argv0 = stdout_by_argv0
        self.raise_for_argv0 = raise_for_argv0 or {}
        self.calls = []

    def __call__(self, cmd, check=True, timeout=None, capture_output=True, text=False):
        self.calls.append(cmd)
        if cmd[0] in self.raise_for_argv0:
            raise self.raise_for_argv0[cmd[0]]
        return _FakeCompletedProcess(stdout=self.stdout_by_argv0.get(cmd[0], ""))


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def get(self, *, timeout=None):
        return self._value


class _RecordingPlayback:
    def __init__(self, state=PlaybackState.PLAYING):
        self.state = state
        self.calls = []

    def get_state(self):
        return _ImmediateFuture(self.state)

    def stop(self):
        self.calls.append("stop")
        return _ImmediateFuture(None)

    def play(self):
        self.calls.append("play")
        return _ImmediateFuture(None)


class _FakeCore:
    def __init__(self, state=PlaybackState.PLAYING):
        self.playback = _RecordingPlayback(state)


def test_successful_reconnect_restarts_audio_and_resumes_mopidys_playback(monkeypatch):
    fake_run = _RecordingRun({"systemctl": "", "pactl": SINKS_WITH_DEVICE})
    monkeypatch.setattr(subprocess, "run", fake_run)
    core = _FakeCore(state=PlaybackState.PLAYING)

    reconnector = AirplayReconnector()
    reconnector.start(core, "VSX-528")
    reconnector._restart_audio(reconnector.generation, core, "VSX-528")

    assert reconnector.status() is None  # succeeded, nothing left to report
    assert any(c[:3] == ["systemctl", "--user", "restart"] for c in fake_run.calls)
    assert ["pactl", "set-default-sink", "raop_sink.VSX-528.local.192.168.1.115.1024"] in fake_run.calls
    # stop then play, in that order -- rebuilds Mopidy's own GStreamer pipeline
    assert core.playback.calls == ["stop", "play"]


def test_reconnect_when_nothing_was_playing_does_not_start_playback(monkeypatch):
    fake_run = _RecordingRun({"systemctl": "", "pactl": SINKS_WITH_DEVICE})
    monkeypatch.setattr(subprocess, "run", fake_run)
    core = _FakeCore(state=PlaybackState.STOPPED)

    reconnector = AirplayReconnector()
    reconnector.start(core, "VSX-528")
    reconnector._restart_audio(reconnector.generation, core, "VSX-528")

    assert reconnector.status() is None
    assert core.playback.calls == ["stop"]  # pipeline still gets rebuilt, just not resumed


def test_failed_service_restart_reports_an_error_and_stops(monkeypatch):
    fake_run = _RecordingRun({}, raise_for_argv0={"systemctl": subprocess.CalledProcessError(1, "systemctl")})
    monkeypatch.setattr(subprocess, "run", fake_run)
    core = _FakeCore()

    reconnector = AirplayReconnector()
    reconnector.start(core, "VSX-528")
    reconnector._restart_audio(reconnector.generation, core, "VSX-528")

    status = reconnector.status()
    assert not reconnector.in_progress
    assert "Couldn't restart the audio service" in status["error"]
    # never got as far as looking for the sink, or touching Mopidy's playback
    assert not any(c[0] == "pactl" for c in fake_run.calls)
    assert core.playback.calls == []


def test_device_never_reappearing_times_out_with_an_error(monkeypatch):
    fake_run = _RecordingRun({"systemctl": "", "pactl": SINKS_WITHOUT_DEVICE})
    monkeypatch.setattr(subprocess, "run", fake_run)
    core = _FakeCore()

    reconnector = AirplayReconnector()
    reconnector.start(core, "VSX-528")
    reconnector._restart_audio(reconnector.generation, core, "VSX-528")

    # _restart_audio's first wait attempt (0) won't have hit the timeout yet --
    # drain the remaining attempts synchronously instead of waiting on the real IOLoop.
    attempt = 1
    while reconnector.in_progress and attempt <= airplay.DEVICE_WAIT_ATTEMPTS + 1:
        reconnector._wait_for_device(reconnector.generation, core, "VSX-528", True, attempt)
        attempt += 1

    status = reconnector.status()
    assert not reconnector.in_progress
    assert "didn't reappear" in status["error"]
    assert not any(c[0] == "pactl" and c[1] == "set-default-sink" for c in fake_run.calls)
    assert core.playback.calls == []


def test_starting_a_new_reconnect_supersedes_a_stale_in_progress_one(monkeypatch):
    fake_run = _RecordingRun({"systemctl": "", "pactl": SINKS_WITHOUT_DEVICE})
    monkeypatch.setattr(subprocess, "run", fake_run)
    core = _FakeCore()

    reconnector = AirplayReconnector()
    reconnector.start(core, "VSX-528")
    stale_generation = reconnector.generation

    reconnector.start(core, "VSX-528")  # supersedes before the first attempt ever ran

    # A callback from the stale attempt firing late must be a no-op.
    reconnector._restart_audio(stale_generation, core, "VSX-528")

    assert fake_run.calls == []
    assert reconnector.in_progress
    assert reconnector.phase == "restarting audio service"
