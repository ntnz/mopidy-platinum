# Mopidy-Platinum

A server-rendered, no-JavaScript-required Mopidy web frontend styled after Mac OS 9's "Platinum" UI — built for playing music from a browser that has no business rendering a modern web page, like Classilla on an iMac G3.

Every page is Tornado-rendered HTML 4.01 with table-based layout and CSS2-only styling (no flexbox/grid, no `border-radius`, no gradients — those didn't exist yet). Core actions (play, queue, browse, search) work as plain forms with full-page reloads; a few small, purely cosmetic touches (double-submit prevention, auto-submitting dropdowns) use progressive-enhancement JavaScript that fails safe if it doesn't run.

## Features

- **Now Playing** — transport controls, volume, shuffle, a click-to-seek progress bar, and an "Up Next" queue preview. The ticking clock and playback status live in their own fast-refreshing frame so the rest of the page (album art included) doesn't reload every couple of seconds.
- **Browse** — walk a backend's library tree, queue or play-next any track.
- **Search** — search by track/artist/album/anything, with per-result source tags when multiple backends are configured.
- **Playlists** — browse and filter playlists by source (handy if the same playlist exists on more than one backend).
- **Queue** — view, reorder-by-removal, and jump playback to anything already queued.
- **AirPlay reconnect** — a background job that restarts the audio stack and re-links a dropped AirPlay/RAOP receiver, exposed as a small, deliberately unobtrusive control (not something a room full of people should be able to bump into).

## Requirements

- [Mopidy](https://mopidy.com/) 4.x
- Python 3.9+
- `httpx`, `Pillow` (installed automatically as dependencies)

## Installation

```sh
pip install -e .
```

Then enable it in your `mopidy.conf`:

```ini
[platinum]
enabled = true
```

## Configuration

All settings are optional; defaults live in `mopidy_platinum/ext.conf`.

| Key | Default | Meaning |
| --- | --- | --- |
| `refresh_interval` | `60` | Seconds between full-page reloads on Now Playing (only ever triggers around a natural track change). |
| `status_refresh_interval` | `2` | Seconds between refreshes of the ticking status frame (time position, queue-fill/AirPlay progress). |
| `max_list_items` | `200` | Page size for Browse/Search/Playlists/Queue, and the increment each "Load more" click adds. |
| `airplay_device_name` | *(empty)* | PipeWire/RAOP sink name to reconnect to. Leave blank to hide the AirPlay reconnect control entirely. |

Once running, the UI is served at `/platinum/` on Mopidy's HTTP server (default `http://localhost:6680/platinum/`).

## Development

```sh
pip install -e ".[test]"  # or just: pip install pytest
pytest
```

The UI is meant to be judged against what a genuinely old browser can do, not against what a modern one can do — when in doubt, prefer the plain-HTML-forms-and-full-reload approach already used throughout over anything that assumes fetch/AJAX/CSS3.

## License

Apache-2.0
