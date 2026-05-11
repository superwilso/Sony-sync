from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib import error, parse, request


LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
TRACK_LINE_RE = re.compile(r"^\s*(?P<time>(?:\d+:)?\d{1,2}:\d{2})\s+(?P<body>.+?)\s*$")


@dataclass
class TrackEntry:
    offset_seconds: int
    artist: str
    title: str
    timestamp: int
    duration: int = 0


@dataclass
class LastfmConfig:
    api_key: str
    api_secret: str
    username: str
    session_key: str


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
            "Missing " + ", ".join(missing) + ". Add them to .env, or run with --login after setting the API key and secret."
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
    username = input(f"Last.fm username [{config.username or 'not set'}]: ").strip() or config.username
    if not username:
        raise RuntimeError("A Last.fm username is required.")

    password = getpass.getpass("Last.fm password: ").strip()
    if not password:
        raise RuntimeError("A Last.fm password is required.")

    authenticated_username, session_key = fetch_lastfm_session(config, username, password)
    save_env_value("LASTFM_USERNAME", authenticated_username)
    save_env_value("LASTFM_SESSION_KEY", session_key)
    print(f"Authenticated as {authenticated_username}. Session saved to .env")


def parse_offset(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"Invalid timestamp: {value}")

    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Invalid timestamp: {value}")
    return hours * 3600 + minutes * 60 + seconds


def parse_start_time(value: str) -> dt.datetime:
    formats = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    raise ValueError("Use a start time like 2026-05-10 19:30")


def split_artist_title(body: str, line_number: int) -> tuple[str, str]:
    if " - " not in body:
        raise ValueError(f"Line {line_number}: expected 'Artist - Track' after the timestamp.")

    artist, title = body.split(" - ", 1)
    artist = " ".join(artist.split())
    title = " ".join(title.split())
    if not artist or not title:
        raise ValueError(f"Line {line_number}: artist and track title must both be present.")
    return artist, title


def parse_tracklist(text: str, start_time: dt.datetime) -> list[TrackEntry]:
    entries: list[TrackEntry] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        match = TRACK_LINE_RE.match(line)
        if not match:
            raise ValueError(f"Line {line_number}: expected a timestamp followed by artist and track.")

        offset = parse_offset(match.group("time"))
        artist, title = split_artist_title(match.group("body"), line_number)
        timestamp = int((start_time + dt.timedelta(seconds=offset)).timestamp())
        entries.append(TrackEntry(offset, artist, title, timestamp))

    if not entries:
        raise ValueError("No tracks were pasted.")

    previous = -1
    for entry in entries:
        if entry.offset_seconds <= previous:
            raise ValueError("Track timestamps must be in ascending order.")
        previous = entry.offset_seconds

    for index, entry in enumerate(entries[:-1]):
        entry.duration = max(entries[index + 1].offset_seconds - entry.offset_seconds, 0)

    return entries


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
        value = input("Set/listen start time (YYYY-MM-DD HH:MM): ").strip()
        if not value:
            print("A start time is needed so Last.fm receives real play times.")
            continue
        try:
            return parse_start_time(value)
        except ValueError as exc:
            print(exc)


def iter_batches(entries: list[TrackEntry], batch_size: int = 50) -> list[list[TrackEntry]]:
    return [entries[index:index + batch_size] for index in range(0, len(entries), batch_size)]


def submit_scrobble_batch(entries: list[TrackEntry], config: LastfmConfig) -> tuple[int, int]:
    params: dict[str, str] = {
        "method": "track.scrobble",
        "api_key": config.api_key,
        "sk": config.session_key,
    }

    for index, entry in enumerate(entries):
        params[f"artist[{index}]"] = entry.artist
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

    return int(scrobbles_node.attrib.get("accepted", "0")), int(scrobbles_node.attrib.get("ignored", "0"))


def preview_entries(entries: list[TrackEntry]) -> None:
    print("\nPreview")
    print("-" * 72)
    for entry in entries:
        played_at = dt.datetime.fromtimestamp(entry.timestamp, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        duration = f"{entry.duration}s" if entry.duration else "unknown"
        print(f"{played_at} | {entry.artist} - {entry.title} | duration {duration}")
    print("-" * 72)
    print(f"{len(entries)} track(s) ready.")


def upload_entries(entries: list[TrackEntry], config: LastfmConfig) -> None:
    require_credentials(config, include_session=True)
    accepted_total = 0
    ignored_total = 0

    for batch_number, batch in enumerate(iter_batches(entries), start=1):
        accepted, ignored = submit_scrobble_batch(batch, config)
        accepted_total += accepted
        ignored_total += ignored
        print(f"Batch {batch_number}: accepted {accepted}, ignored {ignored}")

    print(f"Done. Accepted {accepted_total}, ignored {ignored_total}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Paste a timestamped Artist - Track list and upload it to Last.fm."
    )
    parser.add_argument("--start", help="Listen start time, for example: 2026-05-10 19:30")
    parser.add_argument("--file", help="Read the pasted tracklist from a text file instead of stdin.")
    parser.add_argument("--upload", action="store_true", help="Upload after previewing. Without this, the script previews only.")
    parser.add_argument("--login", action="store_true", help="Create and save a Last.fm session key in .env.")
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
        entries = parse_tracklist(tracklist_text, start_time)
        preview_entries(entries)

        if args.upload:
            confirm = input("\nUpload these plays to Last.fm? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Upload cancelled.")
                return 0
            upload_entries(entries, config)
        else:
            print("\nPreview only. Re-run with --upload when it looks right.")
    except (OSError, ET.ParseError, RuntimeError, ValueError, error.URLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
