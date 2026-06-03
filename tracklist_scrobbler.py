"""
tracklist_scrobbler.py — paste a 1001tracklists text export, pick a time in a TUI,
and scrobble the set to Last.fm. Pure parser, no external APIs (no Gemini).

Usage:
    python tracklist_scrobbler.py --file set.txt
    python tracklist_scrobbler.py --clip          # read tracklist from clipboard
    type set.txt | python tracklist_scrobbler.py  # piped stdin
    python tracklist_scrobbler.py                 # paste, end with a line "." or Ctrl+Z

    python tracklist_scrobbler.py --login         # create+save a Last.fm session key

.env keys needed (same file the other scripts use):
    LASTFM_API_KEY, LASTFM_API_SECRET     – from last.fm/api/account/create
    LASTFM_USERNAME, LASTFM_SESSION_KEY   – saved automatically by --login

Input format (1001tracklists "copy as text"):
    Set Title @ Venue ...
    DJ Name played:
    01. Artist - Title [LABEL]
    w/ Other Artist - Overlay Title [LABEL]
    [03:16] Artist - Title
    ...

Scrobble rules (Last.fm canonical):
    * track.scrobble has no featured-artist field, so "ft./feat." artists are moved
      into the track title as "Title (feat. X)". Co-artist joins ("A & B", "A vs. B")
      stay in the artist field, because that is how Last.fm credits collab releases.
    * "w/" overlay lines are layered mashup parts; off by default. Toggle them on in
      the picker if you want each one scrobbled at its parent track's time.
    * "ID - ID" placeholders are skipped by default.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib import error, parse, request

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
DEFAULT_TRACK_SECONDS = 180  # fallback length for tracks with no cue gap


# ---------------------------------------------------------------------------
# .env helpers (mirrors the other scripts in this repo)
# ---------------------------------------------------------------------------

def load_local_env(env_path: str = ".env") -> None:
    path = Path(env_path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def save_env_value(key: str, value: str, env_path: str = ".env") -> None:
    path = Path(env_path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated = False
    next_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            next_lines.append(f'{key}="{value}"')
            updated = True
        else:
            next_lines.append(line)
    if not updated:
        next_lines.append(f'{key}="{value}"')
    path.write_text("\n".join(next_lines) + "\n", encoding="utf-8")
    os.environ[key] = value


@dataclass
class LastfmConfig:
    api_key: str
    api_secret: str
    username: str
    session_key: str


def read_config() -> LastfmConfig:
    return LastfmConfig(
        api_key=os.environ.get("LASTFM_API_KEY", "").strip(),
        api_secret=os.environ.get("LASTFM_API_SECRET", "").strip(),
        username=os.environ.get("LASTFM_USERNAME", "").strip(),
        session_key=os.environ.get("LASTFM_SESSION_KEY", "").strip(),
    )


# ---------------------------------------------------------------------------
# Last.fm API
# ---------------------------------------------------------------------------

def build_lastfm_api_sig(params: dict[str, str], api_secret: str) -> str:
    signature_base = "".join(f"{key}{params[key]}" for key in sorted(params))
    return hashlib.md5((signature_base + api_secret).encode("utf-8")).hexdigest()


def post_lastfm(params: dict[str, str]) -> ET.Element:
    body = parse.urlencode(params).encode("utf-8")
    req = request.Request(
        LASTFM_API_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    with request.urlopen(req, timeout=30) as response:
        payload = response.read()
    return ET.fromstring(payload)


def require_credentials(config: LastfmConfig, include_session: bool) -> None:
    missing = []
    if not config.api_key:
        missing.append("LASTFM_API_KEY")
    if not config.api_secret:
        missing.append("LASTFM_API_SECRET")
    if include_session and not config.session_key:
        missing.append("LASTFM_SESSION_KEY")
    if missing:
        raise RuntimeError(
            "Missing " + ", ".join(missing)
            + ". Add them to .env, or run with --login after setting the API key and secret."
        )


def fetch_lastfm_session(config: LastfmConfig, username: str, password: str) -> tuple[str, str]:
    params = {
        "method": "auth.getMobileSession",
        "username": username,
        "password": password,
        "api_key": config.api_key,
    }
    params["api_sig"] = build_lastfm_api_sig(params, config.api_secret)
    root = post_lastfm(params)
    if root.attrib.get("status") != "ok":
        node = root.find("error")
        message = (node.text or "").strip() if node is not None else "unknown error"
        code = node.attrib.get("code", "?") if node is not None else "?"
        raise RuntimeError(f"Last.fm error {code}: {message}")
    session = root.find("session")
    name = session.find("name") if session is not None else None
    key = session.find("key") if session is not None else None
    if name is None or key is None or not name.text or not key.text:
        raise RuntimeError("Last.fm did not return a usable session key.")
    return name.text.strip(), key.text.strip()


def login(config: LastfmConfig) -> None:
    require_credentials(config, include_session=False)
    username = input(f"Last.fm username [{config.username or 'not set'}]: ").strip() or config.username
    if not username:
        raise RuntimeError("A Last.fm username is required.")
    password = getpass.getpass("Last.fm password: ").strip()
    if not password:
        raise RuntimeError("A Last.fm password is required.")
    authed, session_key = fetch_lastfm_session(config, username, password)
    save_env_value("LASTFM_USERNAME", authed)
    save_env_value("LASTFM_SESSION_KEY", session_key)
    print(f"Authenticated as {authed}. Session saved to .env")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class Track:
    kind: str                 # "main" or "overlay"
    artist: str               # credited act, e.g. "Daft Punk vs. Marvin Gaye"
    title: str                # title with "(feat. X)" already folded in
    cue: int | None           # seconds offset from the source, or None
    raw: str
    is_id: bool = False
    selected: bool = True
    offset: int = 0           # computed play offset (seconds from set start)
    duration: int = 0         # computed play duration (seconds)


# Leading "[03:16]" or "[1:06:30]" cue marker.
_CUE_RE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*")
# Leading "01. " track number.
_NUM_RE = re.compile(r"^(\d{1,3})\.\s*")
# Leading "w/ " overlay marker.
_OVERLAY_RE = re.compile(r"^w/\s+", re.IGNORECASE)
# Trailing " [LABEL]" record-label tags (one or more).
_LABEL_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
# Featured-artist separator: split credit from featured guests.
_FEAT_RE = re.compile(r"\s+(?:ft\.?|feat\.?|featuring)\s+", re.IGNORECASE)
# Section headers like "Fred again.. played:" and the backlink footer.
_HEADER_RE = re.compile(r"\bplayed:\s*$", re.IGNORECASE)
_BACKLINK_RE = re.compile(r"^(?:please set a backlink|https?://1001\.tl/)", re.IGNORECASE)


def _strip_labels(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _LABEL_RE.sub("", text)
    return text.strip()


def _fold_featured(artist: str, title: str) -> tuple[str, str]:
    """Move 'ft./feat.' guests out of the artist field and into the title."""
    parts = _FEAT_RE.split(artist, maxsplit=1)
    if len(parts) == 2:
        credit, featured = parts[0].strip(), parts[1].strip()
        if featured and "(feat." not in title.lower():
            title = f"{title} (feat. {featured})"
        return credit, title
    return artist.strip(), title


def _split_artist_title(body: str) -> tuple[str, str] | None:
    """Split 'Artist - Title' on the first ' - '. Returns None if no separator."""
    if " - " not in body:
        return None
    artist, title = body.split(" - ", 1)
    return artist.strip(), title.strip()


def parse_tracklist(text: str) -> tuple[str, list[Track]]:
    """Return (set_title, tracks). The first non-empty line is treated as the title."""
    set_title = ""
    tracks: list[Track] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _BACKLINK_RE.match(line) or _HEADER_RE.search(line):
            continue

        overlay = bool(_OVERLAY_RE.match(line))
        body = _OVERLAY_RE.sub("", line, count=1)

        cue: int | None = None
        cue_match = _CUE_RE.match(body)
        if cue_match:
            hours = int(cue_match.group(1)) if cue_match.group(3) else 0
            minutes = int(cue_match.group(2)) if cue_match.group(3) else int(cue_match.group(1))
            seconds = int(cue_match.group(3)) if cue_match.group(3) else int(cue_match.group(2))
            cue = hours * 3600 + minutes * 60 + seconds
            body = _CUE_RE.sub("", body)

        numbered = bool(_NUM_RE.match(body))
        body = _NUM_RE.sub("", body)

        # First meaningful line with no track structure = the set title.
        split = _split_artist_title(_strip_labels(body))
        if split is None:
            if not set_title and not overlay and not numbered and cue is None:
                set_title = line
            continue

        artist_raw, title_raw = split
        artist, title = _fold_featured(artist_raw, title_raw)
        is_id = artist.upper() == "ID" or title.upper() == "ID"

        tracks.append(
            Track(
                kind="overlay" if overlay else "main",
                artist=artist,
                title=title,
                cue=cue,
                raw=line,
                is_id=is_id,
                selected=not is_id,  # IDs start deselected
            )
        )

    return set_title, tracks


def assign_offsets(tracks: list[Track], default_seconds: int) -> int:
    """Lay selected tracks on a timeline. Returns total duration in seconds.

    Main tracks advance the clock. A gap between two cued main tracks gives their
    real spacing; otherwise the fallback length is used. Overlays share their parent
    main track's offset (staggered 1s each so Last.fm keeps their order).
    """
    selected_mains = [t for t in tracks if t.selected and t.kind == "main"]
    offset = 0
    for index, track in enumerate(selected_mains):
        track.offset = offset
        # Duration runs to the next main track. Prefer the real cue gap when both
        # tracks are cued and the cue advanced (it resets between DJ segments).
        if index + 1 < len(selected_mains):
            nxt = selected_mains[index + 1]
            if track.cue is not None and nxt.cue is not None and nxt.cue > track.cue:
                track.duration = nxt.cue - track.cue
            else:
                track.duration = default_seconds
        else:
            track.duration = default_seconds
        offset += track.duration

    # Attach overlays to the most recent main track.
    current_main: Track | None = None
    overlay_index = 0
    for track in tracks:
        if track.kind == "main":
            current_main = track if track.selected else current_main
            overlay_index = 0
            continue
        if track.selected and current_main is not None:
            overlay_index += 1
            track.offset = min(current_main.offset + overlay_index, current_main.offset + max(current_main.duration - 1, 0))
            track.duration = max(current_main.duration - overlay_index, 30)

    return offset


# ---------------------------------------------------------------------------
# Input acquisition
# ---------------------------------------------------------------------------

def read_clipboard() -> str:
    """Read the clipboard via PowerShell (Windows) — no extra dependencies."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not read clipboard: {exc}") from exc


def read_pasted() -> str:
    print("Paste the tracklist text. Finish with a line containing only '.' (or Ctrl+Z then Enter):")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines)


def load_input(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8", errors="replace")
    if args.clip:
        return read_clipboard()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return read_pasted()


# ---------------------------------------------------------------------------
# Terminal key reading
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"


def _read_key() -> str:
    """Single keypress on Windows; line-mode fallback elsewhere."""
    if _IS_WINDOWS:
        import msvcrt  # type: ignore

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            return {
                "H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT",
                "I": "PGUP", "Q": "PGDN", "G": "HOME", "O": "END",
            }.get(ch2, "")
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == " ":
            return "SPACE"
        if ch == "\x1b":
            return "ESC"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\t":
            return "TAB"
        return ch
    line = input("key> ").strip()
    mapping = {"": "ENTER", "w": "UP", "s": "DOWN", "j": "DOWN", "k": "UP",
               " ": "SPACE", "q": "ESC", "esc": "ESC"}
    return mapping.get(line.lower(), line[:1] or "")


def _clear() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def _format_clock(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


# ---------------------------------------------------------------------------
# Track selection TUI
# ---------------------------------------------------------------------------

def select_tracks(tracks: list[Track], set_title: str) -> bool:
    """Arrow keys to move, SPACE to toggle, ENTER to accept. Returns False on cancel."""
    cursor = 0
    top = 0
    view = 18
    message = ""

    while True:
        _clear()
        selected_count = sum(1 for t in tracks if t.selected)
        print("=" * 78)
        print(f" {set_title[:74]}")
        print(f" {selected_count}/{len(tracks)} tracks selected")
        print("=" * 78)

        if cursor < top:
            top = cursor
        elif cursor >= top + view:
            top = cursor - view + 1

        for index in range(top, min(top + view, len(tracks))):
            track = tracks[index]
            pointer = ">" if index == cursor else " "
            box = "[x]" if track.selected else "[ ]"
            cue = _format_clock(track.cue) if track.cue is not None else "  -  "
            prefix = "  w/" if track.kind == "overlay" else "    "
            tag = " (ID)" if track.is_id else ""
            label = f"{track.artist} - {track.title}"
            if len(label) > 52:
                label = label[:51] + "…"
            print(f" {pointer}{box}{prefix} {cue:>7}  {label}{tag}")

        if top + view < len(tracks):
            print(f"      … {len(tracks) - (top + view)} more below")
        print("-" * 78)
        print(" UP/DOWN move   SPACE toggle   PGUP/PGDN page   a=all  z=none")
        print(" w=toggle overlays   i=toggle IDs   ENTER=continue   ESC=cancel")
        if message:
            print(f"\n {message}")
            message = ""
        print("=" * 78)
        sys.stdout.flush()

        try:
            key = _read_key()
        except KeyboardInterrupt:
            return False

        if key == "ENTER":
            if sum(1 for t in tracks if t.selected) == 0:
                message = "Nothing selected."
                continue
            return True
        if key == "ESC":
            return False
        if key in ("UP", "k"):
            cursor = (cursor - 1) % len(tracks)
        elif key in ("DOWN", "j"):
            cursor = (cursor + 1) % len(tracks)
        elif key == "PGUP":
            cursor = max(0, cursor - view)
        elif key == "PGDN":
            cursor = min(len(tracks) - 1, cursor + view)
        elif key == "HOME":
            cursor = 0
        elif key == "END":
            cursor = len(tracks) - 1
        elif key == "SPACE":
            tracks[cursor].selected = not tracks[cursor].selected
        elif key == "a":
            for t in tracks:
                t.selected = True
        elif key == "z":
            for t in tracks:
                t.selected = False
        elif key == "w":
            overlays = [t for t in tracks if t.kind == "overlay"]
            turn_on = not all(t.selected for t in overlays) if overlays else False
            for t in overlays:
                t.selected = turn_on
            message = f"Overlays {'selected' if turn_on else 'deselected'}."
        elif key == "i":
            ids = [t for t in tracks if t.is_id]
            turn_on = not all(t.selected for t in ids) if ids else False
            for t in ids:
                t.selected = turn_on
            message = f"ID tracks {'selected' if turn_on else 'deselected'}."


# ---------------------------------------------------------------------------
# Time picker TUI
# ---------------------------------------------------------------------------

def parse_exact_time(value: str) -> dt.datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    raise ValueError("Use a time like 2026-05-10 19:30")


def _relative(target: dt.datetime, now: dt.datetime) -> str:
    seconds = int((target - now).total_seconds())
    sign = "ago" if seconds < 0 else "from now"
    seconds = abs(seconds)
    if seconds < 60:
        return f"{seconds}s {sign}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m {sign}"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m {sign}"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {sign}"


def pick_time(total_seconds: int, set_title: str, track_count: int) -> dt.datetime | None:
    """Pick the set start. Default anchors the END to now (back-date a finished set)."""
    duration = dt.timedelta(seconds=max(total_seconds, 0))
    anchor = "end"
    pivot = dt.datetime.now(LOCAL_TZ).replace(second=0, microsecond=0)  # holds anchored instant
    message = ""

    while True:
        now = dt.datetime.now(LOCAL_TZ)
        if anchor == "start":
            start_dt, end_dt = pivot, pivot + duration
        else:
            start_dt, end_dt = pivot - duration, pivot

        _clear()
        print("=" * 70)
        print(f" {set_title[:66]}")
        print(f" {track_count} tracks  |  total {_format_duration(total_seconds)}")
        print("=" * 70)
        sm = ">" if anchor == "start" else " "
        em = ">" if anchor == "end" else " "
        print(f" {sm} START : {start_dt.strftime('%a %Y-%m-%d  %H:%M')}   ({_relative(start_dt, now)})")
        print(f" {em} END   : {end_dt.strftime('%a %Y-%m-%d  %H:%M')}   ({_relative(end_dt, now)})")
        print(f"   Anchor: [{anchor.upper()}]")
        print("-" * 70)
        print(" Presets:  e=END now   n=START now   l=last night (21:00)")
        print("           1/2/3=1h/2h/3h ago   y=yesterday")
        print(" Nudge:    LEFT/RIGHT +/-15m   j/k +/-1h   UP/DOWN +/-1d   ,/. +/-1m")
        print(" Other:    m=toggle start/end anchor   t=type exact time")
        print("           ENTER=accept   ESC=cancel")
        if message:
            print(f"\n {message}")
            message = ""
        print("=" * 70)
        sys.stdout.flush()

        try:
            key = _read_key()
        except KeyboardInterrupt:
            return None

        if key == "ENTER":
            return start_dt
        if key == "ESC":
            return None
        if key == "e":
            pivot, anchor = now.replace(second=0, microsecond=0), "end"
        elif key == "n":
            pivot, anchor = now.replace(second=0, microsecond=0), "start"
        elif key == "l":
            target = now.replace(hour=21, minute=0, second=0, microsecond=0)
            if target >= now:
                target -= dt.timedelta(days=1)
            pivot, anchor = target, "start"
        elif key in ("1", "2", "3"):
            pivot = (now - dt.timedelta(hours=int(key))).replace(second=0, microsecond=0)
            anchor = "start"
        elif key == "y":
            pivot, anchor = (now - dt.timedelta(days=1)).replace(second=0, microsecond=0), "start"
        elif key in ("LEFT",):
            pivot -= dt.timedelta(minutes=15)
        elif key in ("RIGHT",):
            pivot += dt.timedelta(minutes=15)
        elif key == "j":
            pivot -= dt.timedelta(hours=1)
        elif key == "k":
            pivot += dt.timedelta(hours=1)
        elif key == "UP":
            pivot -= dt.timedelta(days=1)
        elif key == "DOWN":
            pivot += dt.timedelta(days=1)
        elif key == ",":
            pivot -= dt.timedelta(minutes=1)
        elif key == ".":
            pivot += dt.timedelta(minutes=1)
        elif key == "m":
            # Flip anchor while keeping the visible start/end pair fixed.
            pivot = pivot - duration if anchor == "start" else pivot + duration
            anchor = "end" if anchor == "start" else "start"
        elif key == "t":
            print()
            try:
                typed = input(" Enter time YYYY-MM-DD HH:MM : ").strip()
            except EOFError:
                continue
            if typed:
                try:
                    pivot, anchor = parse_exact_time(typed), "start"
                except ValueError as exc:
                    message = f"Invalid: {exc}"


# ---------------------------------------------------------------------------
# Preview + upload
# ---------------------------------------------------------------------------

def ordered_selected(tracks: list[Track]) -> list[Track]:
    return sorted((t for t in tracks if t.selected), key=lambda t: (t.offset, t.kind == "overlay"))


def preview(tracks: list[Track], start_unix: int) -> None:
    print("\nPreview")
    print("-" * 78)
    for track in ordered_selected(tracks):
        when = dt.datetime.fromtimestamp(start_unix + track.offset, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        mark = "w/" if track.kind == "overlay" else "  "
        print(f" {when}  {mark} {track.artist} - {track.title}  ({track.duration}s)")
    print("-" * 78)
    print(f"{len(ordered_selected(tracks))} scrobble(s) ready.")


def _iter_batches(items: list[Track], size: int = 50) -> list[list[Track]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def submit_batch(batch: list[Track], start_unix: int, config: LastfmConfig) -> tuple[int, int]:
    params: dict[str, str] = {
        "method": "track.scrobble",
        "api_key": config.api_key,
        "sk": config.session_key,
    }
    for index, track in enumerate(batch):
        params[f"artist[{index}]"] = track.artist
        params[f"track[{index}]"] = track.title
        params[f"timestamp[{index}]"] = str(start_unix + track.offset)
        if track.duration > 0:
            params[f"duration[{index}]"] = str(track.duration)
    params["api_sig"] = build_lastfm_api_sig(params, config.api_secret)
    root = post_lastfm(params)
    if root.attrib.get("status") != "ok":
        node = root.find("error")
        message = (node.text or "").strip() if node is not None else "unknown error"
        code = node.attrib.get("code", "?") if node is not None else "?"
        raise RuntimeError(f"Last.fm error {code}: {message}")
    scrobbles = root.find("scrobbles")
    if scrobbles is None:
        raise RuntimeError("Last.fm response did not include scrobble results.")
    return (
        int(scrobbles.attrib.get("accepted", "0")),
        int(scrobbles.attrib.get("ignored", "0")),
    )


def upload(tracks: list[Track], start_unix: int, config: LastfmConfig) -> None:
    require_credentials(config, include_session=True)
    batches = _iter_batches(ordered_selected(tracks))
    accepted_total = ignored_total = 0
    for number, batch in enumerate(batches, start=1):
        accepted, ignored = submit_batch(batch, start_unix, config)
        accepted_total += accepted
        ignored_total += ignored
        print(f"Batch {number}/{len(batches)}: accepted {accepted}, ignored {ignored}")
    print(f"Done. Accepted {accepted_total}, ignored {ignored_total}.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paste a 1001tracklists text export, pick a time in a TUI, scrobble to Last.fm."
    )
    parser.add_argument("--file", help="Read the tracklist from a text file.")
    parser.add_argument("--clip", action="store_true", help="Read the tracklist from the clipboard.")
    parser.add_argument("--login", action="store_true", help="Create and save a Last.fm session key in .env.")
    parser.add_argument(
        "--track-seconds", type=int, default=DEFAULT_TRACK_SECONDS,
        help=f"Fallback length for tracks with no cue gap (default: {DEFAULT_TRACK_SECONDS}).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not upload.")
    return parser.parse_args()


def main() -> int:
    load_local_env()
    args = parse_args()
    config = read_config()

    try:
        if args.login:
            login(config)
            config = read_config()

        text = load_input(args)
        if not text.strip():
            raise RuntimeError("No tracklist input received.")

        set_title, tracks = parse_tracklist(text)
        if not tracks:
            raise RuntimeError("No tracks found. Is this a 1001tracklists text export?")
        set_title = set_title or "tracklist"

        if not select_tracks(tracks, set_title):
            print("Cancelled.")
            return 0

        total = assign_offsets(tracks, args.track_seconds)
        start_time = pick_time(total, set_title, sum(1 for t in tracks if t.selected))
        if start_time is None:
            print("Cancelled.")
            return 0

        start_unix = int(start_time.timestamp())
        _clear()
        preview(tracks, start_unix)

        if args.dry_run:
            print("\nDry run — nothing uploaded.")
            return 0

        confirm = input("\nUpload these plays to Last.fm? [y/N]: ").strip().lower()
        if confirm == "y":
            upload(tracks, start_unix, config)
        else:
            print("Upload cancelled.")

    except (OSError, ET.ParseError, RuntimeError, ValueError, error.URLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
