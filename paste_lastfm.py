"""
Paste a timestamped tracklist and upload it to Last.fm.
Uses Gemini to parse messy tracklist formats automatically.

Dependencies:
    pip install google-genai

.env keys needed:
    LASTFM_API_KEY, LASTFM_API_SECRET          – from last.fm/api/account/create
    LASTFM_USERNAME, LASTFM_SESSION_KEY        – saved automatically by --login
    GEMINI_API_KEY                             – from aistudio.google.com
    GEMINI_MODEL (optional)                    – default: gemini-3.1-flash-lite-preview
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib import error, parse, request

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite-preview"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TrackEntry:
    offset_seconds: int
    artist: str
    title: str
    timestamp: int
    duration: int = 0
    featured_artists: str = ""  # Extracted featured artists for reference


@dataclass
class LastfmConfig:
    api_key: str
    api_secret: str
    username: str
    session_key: str


# ---------------------------------------------------------------------------
# .env helpers
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


def fetch_lastfm_session(
    config: LastfmConfig, username: str, password: str
) -> tuple[str, str]:
    params = {
        "method": "auth.getMobileSession",
        "username": username,
        "password": password,
        "api_key": config.api_key,
    }
    params["api_sig"] = build_lastfm_api_sig(params, config.api_secret)
    root = post_lastfm(params)
    if root.attrib.get("status") != "ok":
        error_node = root.find("error")
        message = (error_node.text or "").strip() if error_node is not None else "unknown error"
        code = error_node.attrib.get("code", "?") if error_node is not None else "?"
        raise RuntimeError(f"Last.fm error {code}: {message}")
    session_node = root.find("session")
    name_node = session_node.find("name") if session_node is not None else None
    key_node = session_node.find("key") if session_node is not None else None
    if name_node is None or key_node is None or not name_node.text or not key_node.text:
        raise RuntimeError("Last.fm did not return a usable session key.")
    return name_node.text.strip(), key_node.text.strip()


def login(config: LastfmConfig) -> None:
    require_credentials(config, include_session=False)
    username = (
        input(f"Last.fm username [{config.username or 'not set'}]: ").strip()
        or config.username
    )
    if not username:
        raise RuntimeError("A Last.fm username is required.")
    password = getpass.getpass("Last.fm password: ").strip()
    if not password:
        raise RuntimeError("A Last.fm password is required.")
    authenticated_username, session_key = fetch_lastfm_session(config, username, password)
    save_env_value("LASTFM_USERNAME", authenticated_username)
    save_env_value("LASTFM_SESSION_KEY", session_key)
    print(f"Authenticated as {authenticated_username}. Session saved to .env")


# ---------------------------------------------------------------------------
# Gemini-powered tracklist parser
# ---------------------------------------------------------------------------

GEMINI_SYSTEM_PROMPT = """\
You are a tracklist parser. The user will paste a timestamped DJ mix or playlist.
Your job is to extract every track as structured data.

Rules:
- Timecodes can be MM:SS or H:MM:SS format.
- The separator between track title and artist varies: it may be " | ", " - ", a newline, or it may be missing entirely — use context to determine which part is the title and which is the artist.
- Artist names sometimes run directly into the next timecode with no space (e.g. "Kučka22:56") — treat everything up to and including the stray text as part of the previous entry's artist field.
- Ignore any lines that are not track entries (headers, URLs, descriptions).
- If a track has featured artists (indicated by "ft.", "feat.", "featuring", "&", "vs.", or "with"), preserve them exactly as they appear.
  Format featured artists consistently: use "ft." as the standard separator, e.g., "Artist A ft. Artist B".
- Preserve the timecode exactly as it appears in the source (e.g. "0:00", "1:03:15").
"""

# JSON Schema passed to Gemini's response_schema — the model is constrained
# to emit exactly this structure; no fences, no stray text, no missing keys.
_TRACK_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "timecode": {
                "type": "string",
                "description": "Original timecode from the tracklist, e.g. '0:00' or '1:03:15'.",
            },
            "title": {
                "type": "string",
                "description": "Track title.",
            },
            "artist": {
                "type": "string",
                "description": "Artist name(s), including featured artists.",
            },
        },
        "required": ["timecode", "title", "artist"],
    },
}


def parse_offset(timecode: str) -> int:
    """Convert a timecode string like '1:03:15' or '3:51' to total seconds."""
    parts = [int(p) for p in timecode.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Invalid timecode: {timecode!r}")


def extract_featured_artist(artist_str: str) -> tuple[str, str]:
    """Split 'Artist ft. Featured' into ('Artist', 'Featured').
    
    Handles: ft., feat., featuring, with, &, vs. (case-insensitive)
    Returns (main_artist, featured_artists) tuple.
    If no featured artist separator found, returns (full_artist, "").
    """
    # Common featured artist patterns
    pattern = r'\s+(ft\.?|feat(?:uring)?|with|\&|vs\.?)\s+'
    parts = re.split(pattern, artist_str, maxsplit=1, flags=re.IGNORECASE)
    
    if len(parts) >= 3:
        main = parts[0].strip()
        featured = parts[2].strip()
        return main, featured
    return artist_str.strip(), ""


def parse_tracklist_with_gemini(
    text: str,
    start_time: dt.datetime,
    model: str = DEFAULT_GEMINI_MODEL,
) -> list[TrackEntry]:
    """Send the raw tracklist to Gemini and get back structured TrackEntry objects.

    Uses Gemini's native structured output (response_schema) so the response is
    guaranteed to be well-formed JSON matching _TRACK_SCHEMA — no post-processing
    or fence-stripping needed.
    """
    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except ImportError:
        raise RuntimeError(
            "google-genai is not installed. Run: pip install google-genai"
        )

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to .env or your environment.")

    model_name = os.environ.get("GEMINI_MODEL", model).strip()
    client = genai.Client(api_key=api_key)

    print(f"Parsing tracklist with {model_name}…")

    response = client.models.generate_content(
        model=model_name,
        contents=text,
        config=genai_types.GenerateContentConfig(
            system_instruction=GEMINI_SYSTEM_PROMPT,
            temperature=0,
            response_mime_type="application/json",
            response_schema=_TRACK_SCHEMA,
        ),
    )

    # With structured output the SDK guarantees valid JSON — parse directly.
    try:
        parsed: list[dict] = json.loads(response.text)
    except json.JSONDecodeError as exc:
        # Should never happen with structured output, but guard defensively.
        raise ValueError(
            f"Gemini structured output was not valid JSON: {exc}\n\nRaw:\n{response.text}"
        ) from exc

    if not parsed:
        raise ValueError("Gemini returned an empty tracklist.")

    # response_schema guarantees all three keys are present in every item.
    entries: list[TrackEntry] = []
    for i, item in enumerate(parsed):
        try:
            offset = parse_offset(item["timecode"])
        except ValueError as exc:
            raise ValueError(f"Track #{i + 1}: bad timecode {item['timecode']!r}: {exc}") from exc
        artist = item["artist"].strip()
        title = item["title"].strip()
        if not artist or not title:
            raise ValueError(f"Track #{i + 1}: artist and title must both be non-empty.")
        timestamp = int((start_time + dt.timedelta(seconds=offset)).timestamp())
        # Extract featured artists for reference/logging
        main_artist, featured = extract_featured_artist(artist)
        entries.append(TrackEntry(offset, artist, title, timestamp, featured_artists=featured))

    # Validate ascending order
    for i in range(1, len(entries)):
        if entries[i].offset_seconds <= entries[i - 1].offset_seconds:
            raise ValueError(
                f"Track timestamps must be in ascending order "
                f"(got {entries[i].offset_seconds}s after {entries[i - 1].offset_seconds}s)."
            )

    # Compute durations from gaps
    for i, entry in enumerate(entries[:-1]):
        entry.duration = max(entries[i + 1].offset_seconds - entry.offset_seconds, 0)

    return entries


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def parse_start_time(value: str) -> dt.datetime:
    formats = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    raise ValueError("Use a start time like 2026-05-10 19:30")


def read_pasted_tracklist() -> str:
    print("Paste the tracklist, then press Enter on a blank line:")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def prompt_start_time() -> dt.datetime:
    while True:
        value = input("Set/listen start time (YYYY-MM-DD HH:MM) or 'now' [now]: ").strip()
        if not value or value.lower() == "now":
            return dt.datetime.now(LOCAL_TZ)
        try:
            return parse_start_time(value)
        except ValueError as exc:
            print(exc)


def preview_entries(entries: list[TrackEntry]) -> None:
    print("\nPreview")
    print("-" * 72)
    for entry in entries:
        played_at = dt.datetime.fromtimestamp(entry.timestamp, LOCAL_TZ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        duration = f"{entry.duration}s" if entry.duration else "unknown"
        print(f"{played_at} | {entry.artist} - {entry.title} | duration {duration}")
    print("-" * 72)
    print(f"{len(entries)} track(s) ready.")


# ---------------------------------------------------------------------------
# Scrobble submission
# ---------------------------------------------------------------------------

def iter_batches(entries: list[TrackEntry], batch_size: int = 50) -> list[list[TrackEntry]]:
    return [entries[i : i + batch_size] for i in range(0, len(entries), batch_size)]


def submit_scrobble_batch(
    entries: list[TrackEntry], config: LastfmConfig, main_artist_only: bool = False
) -> tuple[int, int]:
    params: dict[str, str] = {
        "method": "track.scrobble",
        "api_key": config.api_key,
        "sk": config.session_key,
    }
    for index, entry in enumerate(entries):
        artist = entry.artist
        if main_artist_only:
            artist, _ = extract_featured_artist(artist)
        params[f"artist[{index}]"] = artist
        params[f"track[{index}]"] = entry.title
        params[f"timestamp[{index}]"] = str(entry.timestamp)
        if entry.duration > 0:
            params[f"duration[{index}]"] = str(entry.duration)
    params["api_sig"] = build_lastfm_api_sig(params, config.api_secret)
    root = post_lastfm(params)
    if root.attrib.get("status") != "ok":
        error_node = root.find("error")
        message = (error_node.text or "").strip() if error_node is not None else "unknown error"
        code = error_node.attrib.get("code", "?") if error_node is not None else "?"
        raise RuntimeError(f"Last.fm error {code}: {message}")
    scrobbles_node = root.find("scrobbles")
    if scrobbles_node is None:
        raise RuntimeError("Last.fm response did not include scrobble results.")
    return (
        int(scrobbles_node.attrib.get("accepted", "0")),
        int(scrobbles_node.attrib.get("ignored", "0")),
    )


def upload_entries(entries: list[TrackEntry], config: LastfmConfig, main_artist_only: bool = False) -> None:
    require_credentials(config, include_session=True)
    accepted_total = ignored_total = 0
    for batch_number, batch in enumerate(iter_batches(entries), start=1):
        accepted, ignored = submit_scrobble_batch(batch, config, main_artist_only)
        accepted_total += accepted
        ignored_total += ignored
        print(f"Batch {batch_number}: accepted {accepted}, ignored {ignored}")
    print(f"Done. Accepted {accepted_total}, ignored {ignored_total}.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paste a timestamped tracklist and upload it to Last.fm via Gemini parsing."
    )
    parser.add_argument("--start", help="Listen start time, e.g. 2026-05-10 19:30")
    parser.add_argument(
        "--file", help="Read the tracklist from a text file instead of stdin."
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Create and save a Last.fm session key in .env.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_GEMINI_MODEL,
        help=f"Gemini model to use for parsing (default: {DEFAULT_GEMINI_MODEL}).",
    )
    parser.add_argument(
        "--main-artist-only",
        action="store_true",
        help="Extract only the main artist, ignoring featured artists (recommended for Last.fm artist stats).",
    )
    return parser.parse_args()


def main() -> int:
    load_local_env()
    args = parse_args()
    config = read_config()

    try:
        if args.login:
            login(config)
            config = read_config()

        if args.file:
            tracklist_text = Path(args.file).read_text(encoding="utf-8")
        else:
            tracklist_text = read_pasted_tracklist()

        start_time = parse_start_time(args.start) if args.start else prompt_start_time()
        entries = parse_tracklist_with_gemini(tracklist_text, start_time, model=args.model)
        preview_entries(entries)

        confirm = input("\nUpload these plays to Last.fm? [y/N]: ").strip().lower()
        if confirm == "y":
            upload_entries(entries, config, args.main_artist_only)
        else:
            print("Upload cancelled.")

    except (OSError, ET.ParseError, RuntimeError, ValueError, error.URLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())