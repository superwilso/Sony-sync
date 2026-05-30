"""Fetch last 2 weeks of scrobbles and flag malformed 'ft.' / featured-artist notation."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib import parse, request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_page(api_key: str, user: str, from_ts: int, to_ts: int, page: int) -> dict:
    params = {
        "method": "user.getRecentTracks",
        "user": user,
        "api_key": api_key,
        "format": "json",
        "limit": "200",
        "from": str(from_ts),
        "to": str(to_ts),
        "page": str(page),
    }
    url = LASTFM_API_URL + "?" + parse.urlencode(params)
    with request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def fetch_all(api_key: str, user: str, days: int = 14) -> list[dict]:
    now = int(dt.datetime.now().timestamp())
    frm = now - days * 86400
    all_tracks: list[dict] = []
    page = 1
    while True:
        data = fetch_page(api_key, user, frm, now, page)
        rt = data.get("recenttracks", {})
        tracks = rt.get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        # Skip "now playing" track (no date)
        tracks = [t for t in tracks if t.get("date")]
        all_tracks.extend(tracks)
        attr = rt.get("@attr", {})
        total_pages = int(attr.get("totalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return all_tracks


# Patterns suggesting raw/uncleaned featured-artist notation
SUSPICIOUS_PATTERNS = [
    (re.compile(r"\(\s*(ft|feat|featuring)\b", re.I), "ft. inside parens — should be inline"),
    (re.compile(r"\[\s*(ft|feat|featuring)\b", re.I), "ft. inside brackets"),
    (re.compile(r"\bfeat\.?(?!\s*uring)", re.I), "uses 'feat.' instead of 'ft.'"),
    (re.compile(r"\bfeaturing\b", re.I), "uses 'featuring' instead of 'ft.'"),
    (re.compile(r"\bwith\s+[A-Z]"), "'with X' — may be featured artist"),
    (re.compile(r"\bvs\.?\s+", re.I), "'vs.' — may be collab"),
    (re.compile(r"\s{2,}"), "double spaces"),
    (re.compile(r"\bft\s+[A-Z]"), "'ft' missing period"),
    (re.compile(r"[,&]\s*$"), "trailing separator"),
    (re.compile(r"^\s*[,&]"), "leading separator"),
    (re.compile(r"\d{1,2}:\d{2}"), "timestamp leaked into field"),
]


def audit_track(artist: str, title: str) -> list[str]:
    issues: list[str] = []
    for field_name, value in (("artist", artist), ("title", title)):
        for pat, desc in SUSPICIOUS_PATTERNS:
            if pat.search(value):
                issues.append(f"{field_name}: {desc}")
    # Title containing ft. — common case where ft should be in artist field
    if re.search(r"\b(ft\.?|feat\.?|featuring)\b", title, re.I):
        issues.append("title contains 'ft.' — featured artist should be in artist field")
    return issues


def main() -> int:
    load_env()
    api_key = os.environ.get("LASTFM_API_KEY", "").strip()
    user = os.environ.get("LASTFM_USERNAME", "").strip()
    if not api_key or not user:
        print("Missing LASTFM_API_KEY or LASTFM_USERNAME", file=sys.stderr)
        return 1

    days = 14
    print(f"Fetching last {days} days of scrobbles for {user}...")
    tracks = fetch_all(api_key, user, days=days)
    print(f"Got {len(tracks)} scrobbles.\n")

    flagged: list[tuple[dict, list[str]]] = []
    for t in tracks:
        artist = (t.get("artist", {}).get("#text") or "").strip()
        title = (t.get("name") or "").strip()
        issues = audit_track(artist, title)
        if issues:
            flagged.append((t, issues))

    if not flagged:
        print("No issues found.")
        return 0

    print(f"Flagged {len(flagged)} / {len(tracks)} scrobbles:\n")
    print("-" * 80)
    for t, issues in flagged:
        artist = t.get("artist", {}).get("#text", "")
        title = t.get("name", "")
        when = t.get("date", {}).get("#text", "")
        album = t.get("album", {}).get("#text", "")
        print(f"[{when}] {artist} - {title}")
        if album:
            print(f"   album: {album}")
        for iss in issues:
            print(f"   ! {iss}")
        print()

    # Group by issue type for summary
    summary: dict[str, int] = {}
    for _, issues in flagged:
        for i in issues:
            summary[i] = summary.get(i, 0) + 1
    print("-" * 80)
    print("Summary:")
    for k, v in sorted(summary.items(), key=lambda x: -x[1]):
        print(f"  {v:3d}  {k}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
