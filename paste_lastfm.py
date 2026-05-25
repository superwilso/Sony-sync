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
from html.parser import HTMLParser
from pathlib import Path
from urllib import error, parse, request

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"


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


_URL_FETCH_SYSTEM_PROMPT = """\
You are a tracklist extractor with web access.
The user gives you a URL to a DJ set tracklist page (e.g. 1001tracklists.com).
Fetch the page and read every track in the visible tracklist.
Return ONLY a plain-text list, one track per line, in this exact format:

    MM:SS Artist - Title
    H:MM:SS Artist - Title

Rules:
- One track per line, no numbering, no bullets, no headers, no explanation.
- The timecode MUST come first (the cue offset shown on the page).
- If a track has no cue, omit the line.
- If artist/title contains a featured artist, keep it as "Artist ft. Featured".
- Skip placeholder "?" / "ID - ID" lines.
- Do not include markdown fences or any prose before or after the list.
"""


def fetch_tracklist_text_via_gemini(
    url: str, model: str = DEFAULT_GEMINI_MODEL
) -> str:
    """Use Gemini's URL Context tool to fetch + render a JS-heavy tracklist page.

    Returns plain text formatted as `MM:SS Artist - Title` lines, ready to feed
    through ``parse_tracklist_with_gemini``.
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

    print(f"Fetching tracklist from URL via {model_name} (URL Context)…")

    # URL Context tool: Gemini fetches and renders the page server-side, then
    # answers the prompt grounded in the page contents.
    config_kwargs: dict = {
        "system_instruction": _URL_FETCH_SYSTEM_PROMPT,
        "temperature": 0,
    }
    try:
        url_tool = genai_types.Tool(url_context=genai_types.UrlContext())
        config_kwargs["tools"] = [url_tool]
    except (AttributeError, TypeError) as exc:
        raise RuntimeError(
            "Your google-genai version does not support the URL Context tool. "
            "Upgrade: pip install -U google-genai"
        ) from exc

    response = client.models.generate_content(
        model=model_name,
        contents=f"Extract the tracklist from this URL:\n{url}",
        config=genai_types.GenerateContentConfig(**config_kwargs),
    )

    text = (response.text or "").strip()
    # Strip code fences if Gemini added them despite the system prompt.
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    # Drop empty lines + obvious junk preamble lines that lack a timecode.
    cleaned: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not re.match(r"^\d{1,2}:\d{2}(:\d{2})?\b", line):
            continue
        cleaned.append(line)
    if not cleaned:
        raise RuntimeError(
            "Gemini could not extract a tracklist from the URL.\n"
            f"Raw response:\n{text[:500]}"
        )
    return "\n".join(cleaned)


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
# URL / HTML fetching + 1001tracklists extraction
# ---------------------------------------------------------------------------

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.1001tracklists.com/",
}


def fetch_url_html(url: str) -> str:
    req = request.Request(url, headers=_BROWSER_HEADERS, method="GET")
    try:
        with request.urlopen(req, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except error.HTTPError as exc:
        if exc.code in (403, 429, 503):
            raise RuntimeError(
                f"Blocked by site ({exc.code}). Save the page in your browser "
                "(Ctrl+S) and re-run with --html-file PATH, or paste the file path."
            ) from exc
        raise RuntimeError(f"HTTP {exc.code} fetching {url}") from exc


def _seconds_to_cue(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _extract_jsonld_tracks(html: str) -> list[str] | None:
    """Try JSON-LD MusicPlaylist/ItemList. Returns formatted lines or None."""
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for blob in candidates:
            tracks = _jsonld_tracks_from_blob(blob)
            if tracks:
                return tracks
    return None


def _jsonld_tracks_from_blob(blob: dict) -> list[str] | None:
    if not isinstance(blob, dict):
        return None
    # MusicPlaylist: track = [{ "@type": "MusicRecording", "name": "...", "byArtist": {...} }]
    items = blob.get("track") or blob.get("itemListElement")
    if not isinstance(items, list) or not items:
        return None
    lines: list[str] = []
    fallback_offset = 0
    for item in items:
        node = item.get("item") if isinstance(item, dict) and "item" in item else item
        if not isinstance(node, dict):
            continue
        name = (node.get("name") or "").strip()
        if not name or name in {"?", "ID", "ID - ID"}:
            continue
        artist_field = node.get("byArtist")
        artist = ""
        if isinstance(artist_field, dict):
            artist = (artist_field.get("name") or "").strip()
        elif isinstance(artist_field, str):
            artist = artist_field.strip()
        elif isinstance(artist_field, list) and artist_field:
            first = artist_field[0]
            if isinstance(first, dict):
                artist = (first.get("name") or "").strip()
            elif isinstance(first, str):
                artist = first.strip()
        if artist and " - " not in name:
            display = f"{artist} - {name}"
        else:
            display = name
        cue_text = node.get("startTime") or node.get("startOffset")
        if isinstance(cue_text, str) and re.match(r"^\d+:\d+(:\d+)?$", cue_text):
            cue = cue_text
        else:
            cue = _seconds_to_cue(fallback_offset)
            fallback_offset += 180
        lines.append(f"{cue} {display}")
    return lines or None


class _TlpItemParser(HTMLParser):
    """Pull `<div class="tlpItem">` blocks: data-cue attr + nested trackFormat text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []  # (cue, text)
        self._depth = 0
        self._cue: str | None = None
        self._track_depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if "tlpItem" in classes and self._depth == 0:
            self._depth = 1
            self._cue = attr.get("data-cue") or attr.get("data-cuevalue")
            self._buf = []
            self._track_depth = 0
            return
        if self._depth >= 1:
            self._depth += 1
            if "trackFormat" in classes or "trackValue" in classes:
                self._track_depth = self._depth
            if self._cue is None and "cueValueField" in classes:
                # mark cue capture via track_depth-like state: reuse buf with marker
                self._track_depth = max(self._track_depth, self._depth)

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0:
            return
        if self._depth == 1:
            text = " ".join(" ".join(self._buf).split()).strip()
            if text:
                self.items.append((self._cue or "", text))
            self._depth = 0
            self._cue = None
            self._buf = []
            self._track_depth = 0
            return
        self._depth -= 1
        if self._track_depth and self._depth < self._track_depth:
            self._track_depth = 0

    def handle_data(self, data: str) -> None:
        if self._track_depth and self._depth >= self._track_depth:
            self._buf.append(data)


class _HtmlTextStripper(HTMLParser):
    """Collect visible text (skips script/style)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _strip_html_to_text(html: str) -> str:
    stripper = _HtmlTextStripper()
    try:
        stripper.feed(html)
    except Exception:  # noqa: BLE001
        pass
    text = " ".join("".join(stripper.parts).split())
    return text


_JS_SHELL_MARKERS = ("jsAsyncReady", "loadYTIframe", "fwRdy")


def extract_tracklist_text_from_1001(html: str) -> tuple[str, str]:
    """Return (cleaned_tracklist_text, page_title). Raises on no tracks."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    page_title = ""
    if title_match:
        page_title = re.sub(r"\s+", " ", title_match.group(1)).strip()

    lines = _extract_jsonld_tracks(html)
    if not lines:
        parser = _TlpItemParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001
            pass
        rendered: list[str] = []
        fallback_offset = 0
        for cue, text in parser.items:
            if text in {"?", "ID", "ID - ID"} or text.startswith("? - "):
                continue
            if cue and re.match(r"^\d+:\d+(:\d+)?$", cue):
                rendered.append(f"{cue} {text}")
            elif cue and cue.isdigit():
                rendered.append(f"{_seconds_to_cue(int(cue))} {text}")
            else:
                rendered.append(f"{_seconds_to_cue(fallback_offset)} {text}")
                fallback_offset += 180
        lines = rendered

    if not lines:
        is_shell = any(m in html for m in _JS_SHELL_MARKERS) and "tlpItem" not in html
        if is_shell:
            raise RuntimeError(
                "The page is JavaScript-rendered — the initial HTML contains no track data.\n"
                "Workaround:\n"
                "  1. Open the URL in your browser and wait for tracks to load.\n"
                "  2. Press F12 → Elements → right-click <html> → Copy → Copy outerHTML.\n"
                "  3. Save into a file (e.g. tracklist.html) and rerun with:\n"
                "       --html-file tracklist.html\n"
                "  (Ctrl+S \"Webpage, Complete\" also works on most browsers.)"
            )
        # Last resort: hand stripped page text to Gemini and let it find tracks.
        text = _strip_html_to_text(html)
        if text:
            return text, page_title
        raise RuntimeError(
            "Could not find any tracks in the page. "
            "Save the page locally and try --html-file, or paste the tracklist as text."
        )
    return "\n".join(lines), page_title


def load_input_source(
    raw: str, model: str = DEFAULT_GEMINI_MODEL
) -> tuple[str, str]:
    """Dispatch URL / .html path / raw text. Returns (tracklist_text, label)."""
    stripped = raw.strip()
    if stripped.lower().startswith(("http://", "https://")):
        # JS-rendered tracklist sites (1001tracklists etc.) — fetch via Gemini's
        # URL Context tool so the page is rendered server-side, not parsed raw.
        text = fetch_tracklist_text_via_gemini(stripped, model=model)
        return text, stripped
    candidate = Path(stripped)
    if stripped and candidate.is_file() and candidate.suffix.lower() in {".html", ".htm"}:
        html = candidate.read_text(encoding="utf-8", errors="replace")
        text, title = extract_tracklist_text_from_1001(html)
        return text, (title or str(candidate))
    return raw, "pasted text"


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


_SHORTHAND_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$", re.IGNORECASE)


def parse_start_shorthand(value: str) -> dt.datetime | None:
    """Resolve 'now', '1h', '30m', '2d', 'yesterday', 'last-night' to a datetime."""
    now = dt.datetime.now(LOCAL_TZ)
    v = value.strip().lower()
    if not v:
        return None
    if v == "now":
        return now
    if v in {"yest", "yesterday"}:
        return now - dt.timedelta(days=1)
    if v in {"last-night", "lastnight", "ln"}:
        target = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if target >= now:
            target -= dt.timedelta(days=1)
        return target
    match = _SHORTHAND_PATTERN.match(v)
    if match:
        qty = float(match.group(1))
        unit = match.group(2).lower()
        if unit == "m":
            return now - dt.timedelta(minutes=qty)
        if unit == "h":
            return now - dt.timedelta(hours=qty)
        if unit == "d":
            return now - dt.timedelta(days=qty)
    return None


def read_pasted_tracklist() -> str:
    print(
        "Paste a 1001tracklists URL, a saved .html path, "
        "or a tracklist (blank line to finish):"
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            if lines:
                break
            continue
        # URL or single-line file path: accept on the spot, no blank-line wait.
        stripped = line.strip()
        if not lines and (
            stripped.lower().startswith(("http://", "https://"))
            or (Path(stripped).is_file() and Path(stripped).suffix.lower() in {".html", ".htm"})
        ):
            return stripped
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interactive time picker
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"


def _read_key() -> str:
    """Single-key read on Windows; line-mode fallback elsewhere."""
    if _IS_WINDOWS:
        import msvcrt  # type: ignore

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            return {
                "H": "UP",
                "P": "DOWN",
                "K": "LEFT",
                "M": "RIGHT",
            }.get(ch2, "")
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == "\x1b":
            return "ESC"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    # POSIX: line-input mode. Tokens accepted: n,h,H,3,l,y,m,t,j,k,< >,, .,q + ENTER.
    try:
        line = input("key> ").strip()
    except EOFError:
        return "ESC"
    if line == "":
        return "ENTER"
    if line.lower() in {"q", "esc"}:
        return "ESC"
    if line in {"<", "left"}:
        return "LEFT"
    if line in {">", "right"}:
        return "RIGHT"
    if line in {"up"}:
        return "UP"
    if line in {"down"}:
        return "DOWN"
    return line[0]


def _clear_screen() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def _format_relative(target: dt.datetime, now: dt.datetime) -> str:
    delta = target - now
    seconds = int(delta.total_seconds())
    sign = "ago" if seconds < 0 else "from now"
    seconds = abs(seconds)
    if seconds < 60:
        return f"{seconds}s {sign}"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sign}"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m {sign}"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {sign}"


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def pick_start_time(
    duration_seconds: int, dj_label: str, track_count: int
) -> dt.datetime | None:
    """Interactive picker. Returns the chosen *start* datetime (or None on cancel)."""
    duration = dt.timedelta(seconds=max(duration_seconds, 0))
    start = (dt.datetime.now(LOCAL_TZ) - duration).replace(second=0, microsecond=0)
    anchor = "start"  # "start" or "end"
    message = ""

    while True:
        if anchor == "start":
            start_dt = start
            end_dt = start + duration
        else:
            end_dt = start  # `start` variable holds the anchor in end-mode
            start_dt = end_dt - duration

        _clear_screen()
        now = dt.datetime.now(LOCAL_TZ)
        label = dj_label[:60] if dj_label else "tracklist"
        print("=" * 68)
        print(f" Set: {label}")
        print(f"      {track_count} tracks  |  duration {_format_duration(duration_seconds)}")
        print("=" * 68)
        start_marker = ">" if anchor == "start" else " "
        end_marker = ">" if anchor == "end" else " "
        print(
            f" {start_marker} START : {start_dt.strftime('%a %Y-%m-%d  %H:%M')}   "
            f"({_format_relative(start_dt, now)})"
        )
        print(
            f" {end_marker} END   : {end_dt.strftime('%a %Y-%m-%d  %H:%M')}   "
            f"({_format_relative(end_dt, now)})"
        )
        print(f"   Mode  : [{anchor.upper()}-ANCHOR]")
        print("-" * 68)
        print(" Presets:  n=now   h=1h ago   H=2h ago   3=3h ago")
        print("           l=last night (yest 21:00)    y=yesterday now")
        print(" Nudge:    LEFT/RIGHT +/- 15m   j/k +/- 1h")
        print("           UP/DOWN  +/- 1 day   , / .   +/- 1 min")
        print(" Other:    m=toggle start/end anchor   t=type exact")
        print("           ENTER=accept   ESC or q=cancel")
        if message:
            print()
            print(f" {message}")
            message = ""
        print("=" * 68)
        sys.stdout.flush()

        try:
            key = _read_key()
        except KeyboardInterrupt:
            return None

        if key == "ENTER":
            return start_dt
        if key in {"ESC", "q", "Q"}:
            return None
        if key == "n":
            start = now.replace(second=0, microsecond=0)
            anchor = "start"
            continue
        if key == "h":
            start = (now - dt.timedelta(hours=1)).replace(second=0, microsecond=0)
            anchor = "start"
            continue
        if key == "H":
            start = (now - dt.timedelta(hours=2)).replace(second=0, microsecond=0)
            anchor = "start"
            continue
        if key == "3":
            start = (now - dt.timedelta(hours=3)).replace(second=0, microsecond=0)
            anchor = "start"
            continue
        if key == "l":
            target = now.replace(hour=21, minute=0, second=0, microsecond=0)
            if target >= now:
                target -= dt.timedelta(days=1)
            start = target
            anchor = "start"
            continue
        if key == "y":
            start = (now - dt.timedelta(days=1)).replace(second=0, microsecond=0)
            anchor = "start"
            continue
        if key == "LEFT":
            start -= dt.timedelta(minutes=15)
            continue
        if key == "RIGHT":
            start += dt.timedelta(minutes=15)
            continue
        if key == "j":
            start -= dt.timedelta(hours=1)
            continue
        if key == "k":
            start += dt.timedelta(hours=1)
            continue
        if key == "UP":
            start -= dt.timedelta(days=1)
            continue
        if key == "DOWN":
            start += dt.timedelta(days=1)
            continue
        if key == ",":
            start -= dt.timedelta(minutes=1)
            continue
        if key == ".":
            start += dt.timedelta(minutes=1)
            continue
        if key == "m":
            # Toggle anchor: keep the visible start/end pair stable across the flip.
            if anchor == "start":
                start = start + duration  # store the end instant as anchor
                anchor = "end"
            else:
                start = start - duration
                anchor = "start"
            continue
        if key == "t":
            print()
            try:
                typed = input(" Enter time YYYY-MM-DD HH:MM : ").strip()
            except EOFError:
                continue
            if not typed:
                continue
            try:
                start = parse_start_time(typed)
                anchor = "start"
            except ValueError as exc:
                message = f"Invalid: {exc}"
            continue
        # unknown key — redraw with no change


def resolve_start_time(arg_value: str | None) -> dt.datetime | None:
    """Map a --start CLI value to a datetime, or None if no flag given."""
    if arg_value is None:
        return None
    sh = parse_start_shorthand(arg_value)
    if sh is not None:
        return sh
    return parse_start_time(arg_value)


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
    parser.add_argument(
        "--start",
        help=(
            "Listen start time. Accepts 'YYYY-MM-DD HH:MM', 'now', '1h', '30m', "
            "'2d', 'yesterday', or 'last-night'. Omit to open the interactive picker."
        ),
    )
    parser.add_argument(
        "--file", help="Read the tracklist from a text file instead of stdin."
    )
    parser.add_argument(
        "--url",
        help="Fetch a 1001tracklists.com URL and extract the tracklist automatically.",
    )
    parser.add_argument(
        "--html-file",
        dest="html_file",
        help="Read a saved 1001tracklists HTML page (use when the site blocks fetch).",
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

        source_label = ""
        if args.url:
            tracklist_text = fetch_tracklist_text_via_gemini(args.url, model=args.model)
            source_label = args.url
        elif args.html_file:
            html = Path(args.html_file).read_text(encoding="utf-8", errors="replace")
            tracklist_text, page_title = extract_tracklist_text_from_1001(html)
            source_label = page_title or args.html_file
        elif args.file:
            raw = Path(args.file).read_text(encoding="utf-8")
            tracklist_text, source_label = load_input_source(raw, model=args.model)
        else:
            raw = read_pasted_tracklist()
            tracklist_text, source_label = load_input_source(raw, model=args.model)

        if not tracklist_text.strip():
            raise RuntimeError("No tracklist input received.")

        # Parse once with a placeholder anchor; real timestamps applied after picker.
        placeholder_anchor = dt.datetime(2001, 1, 1, tzinfo=LOCAL_TZ)
        entries = parse_tracklist_with_gemini(
            tracklist_text, placeholder_anchor, model=args.model
        )
        last = entries[-1]
        duration_seconds = last.offset_seconds + (last.duration or 240)

        start_time = resolve_start_time(args.start)
        if start_time is None:
            start_time = pick_start_time(
                duration_seconds,
                source_label or "tracklist",
                len(entries),
            )
            if start_time is None:
                print("Cancelled.")
                return 0

        start_unix = int(start_time.timestamp())
        for entry in entries:
            entry.timestamp = start_unix + entry.offset_seconds

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