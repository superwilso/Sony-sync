# Sony sync

Two things live here, and they are deliberately separate:

| | what it does | run it |
|---|---|---|
| **music sync** (`sync.py`) | plans and copies the library onto the Walkman's two volumes, writes the playlists, removes what no longer belongs, uploads `.scrobbler.log` to Last.fm | `python sync.py` |
| **likes sync** (`likesync/`) | keeps *liked* in step across the Walkman, Last.fm and MusicBee, and writes a **Liked Songs** playlist onto the device | `python -m likesync sync` |

`app.py` is the full-screen front-end both of them share; `tui.py` is the terminal toolkit it is
built from. Everything is standard library only — no pip install, no venv, any Python 3.10+.

```
python sync.py              # full-screen UI (music sync, likes, scrobbles, settings)
python sync.py --classic    # the original scrolling menu, unchanged
python -m likesync status   # likes only, no UI, read-only
python -m unittest discover -s tests
```

---

## The likes sync

### Why it needs three-way state

The Walkman has no WiFi, so it can never call Last.fm itself. Cinder therefore writes a file and a
PC tool carries it — the same shape scrobbling already has:

```
device  /contents/cinder_liked.conf        object ids   (the real store, meaningless off-device)
        /contents/cinder_loved.tsv         artist ⇥ title   ← Cinder exports on every change
        /contents/cinder_liked_import.tsv  artist ⇥ title   → likesync writes, Cinder consumes
        /contents/MUSIC/Liked Songs.m3u8   the playable list on the device
last.fm user.getLovedTracks / track.love / track.unlove
musicbee an exported playlist (and optionally file-tag ratings)
```

A three-way set comparison cannot tell "just loved over here" from "just unliked over there", so
`likesync` keeps a snapshot of each source (`.likesync/state.json`) and diffs each source against
**its own** previous snapshot. The rules that fall out of that:

* **first run is additive** — the union of all three, nothing is ever removed;
* **a source that is not available contributes nothing** — an unplugged Walkman or a Last.fm
  outage is not evidence that anything was unliked;
* **while an import is pending** (Cinder has not merged it yet) the device is additive-only — its
  export still shows the pre-push list, and treating that as removals would push the difference
  back out as unloves;
* **a conflict keeps the like** by default (`conflict: liked`), because losing a hand-curated like
  to an ambiguity is worse than keeping one you meant to drop. `conflict: latest` is the opposite.

### Matching

Everything is keyed on `artist + title`, normalised: case, curly punctuation, `feat.` credits and
re-issue suffixes (`- Remastered 2009`, `(2021 Mix)`, `(Deluxe Edition)`) fold away, while
anything that marks a *different recording* — live, remix, acoustic, demo — stays distinct. When
the exact key misses, the primary artist is tried, then the album-folder artist, which is what
matches Last.fm's `Little Simz feat. Cleo Sol` to the file tagged `Cleo Sol` on a Little Simz
album. On the reference library that resolves **129 of 131** liked tracks; the two that miss are
genuinely not in the library.

The same normalisation exists on the device in `player/cinder-ffi/src/likes.rs` — the two sides
have to agree or nothing lines up.

### Setting it up

1. **Last.fm** — put `LASTFM_API_KEY` / `LASTFM_API_SECRET` in `.env`, then `python -m likesync
   login` (or the Last.fm screen in the UI). The session key is written back to `.env`.
2. **MusicBee** — make a playlist of what you love (an auto-playlist on `Love` or `Rating ≥ 4`
   works), then MusicBee ▸ right-click the playlist ▸ *auto-export as m3u* into the exported
   playlists folder. `likesync` finds anything named `Loved` / `Liked` / `Favourites` on its own,
   or set `musicbee_loved_playlist` explicitly. If MusicBee's own Last.fm love is switched on,
   this step is optional — the loves arrive through Last.fm anyway.
   To read stars from the files instead, set `musicbee_rating_threshold` to e.g. `4`.
3. **The device** — nothing to configure. The `Liked Songs.m3u8` playlist works on any Cinder
   build; the hearts on the device only update on a build that has the liked-import (see below).

Settings live in `likesync.json`; `python -m likesync config key=value` edits them.

### What it writes

| file | when |
|---|---|
| `<device>\MUSIC\Liked Songs.m3u8` | always, per drive, listing only that drive's tracks (a playlist that names a file on the other volume is a dead row) |
| `<device>\cinder_liked_import.tsv` | when `device_import` is on (internal volume only — `cinder_liked.conf` lives at `/contents`) |
| `<playlists>\Liked Songs.m3u8` | when `musicbee_export_playlist` is on |
| Last.fm | `track.love` / `track.unlove`, one call per change, throttled to Last.fm's rate limit |

`sync.py` knows to leave `Liked Songs.m3u8` alone at both ends: it is excluded from the source
playlist scan (otherwise its whole-library span would collapse every album onto one drive) and
from the stale-playlist sweep on the device.

### On the device

Cinder's import (`likes.rs`) replaces the liked set with the resolved rows and renames the file to
`.tsv.done`. It refuses to act on a file with no `# artist` header, or on one whose rows resolve
to nothing — in both cases the file is left in place for the next boot. An empty file *with* the
header is honoured: that means "everything was unliked".

---

## The UI

```
 WALKMAN SYNC                              walkman connected   last.fm Superwils0
 ┌─ Storage ──────────────────┐ ┌─ This sync ────────────────┐
 │ Internal ███████░░░ 41/54G │ │ Net  +6 albums  +1.2 GB    │
 ...
 › 1  Sync music to the Walkman
   2  Album changes (what moves)
   7  Likes: device ⇄ Last.fm ⇄ MusicBee
 ↑↓ move  ⏎ choose  r rescan  / filter  q quit
```

* one write per frame into the alternate screen buffer — no flicker, and your scrollback survives;
* single keypresses, arrow keys, `/` to filter any list, `esc` to go back;
* scans run on a worker thread with a live phase/ETA readout instead of freezing;
* panels stack and the menu shrinks on a small terminal — the menu is never pushed off-screen;
* CJK and accented titles are measured in display cells, so columns line up.

Every screen is a pure `render_*(…) -> list[str]`, which is why `tests/test_tui.py` can render all
of them headlessly against the real planner.

## Tests

```
python -m unittest discover -s tests      # 42 tests: merge rules, tags, keys, TUI rendering
```

The merge-rule tests are the ones that matter: they cover first run, removal propagation, an
absent source, a pending device import, and both conflict policies.
