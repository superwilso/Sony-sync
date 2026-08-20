"""Last.fm loved-tracks client.

Last.fm is the hub of this sync, not just a third participant: MusicBee already talks to it (love
in MusicBee is a `track.love`), and the Walkman never can — it has no WiFi, Bluetooth only — so
every path between the device and MusicBee runs through a file on one side and this API on the
other.

Only three calls are used: `user.getLovedTracks` to read, `track.love` / `track.unlove` to write.
The signature scheme is the same one `sync.py` already implements for scrobbling.
"""
from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib import error, parse, request

from .keys import track_key

API_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "likesync/1.0 (+walkman)"
# Last.fm asks for <=5 requests/second averaged. Writes are one call per track, so a 300-track
# first push takes about a minute — that is the API's floor, not this client's.
MIN_INTERVAL = 0.22
RATE_LIMIT_CODE = "29"


class LastfmError(RuntimeError):
    pass


@dataclass
class Lastfm:
    api_key: str
    api_secret: str
    session_key: str = ""
    username: str = ""
    _last_call: float = field(default=0.0, repr=False)

    # ── plumbing ─────────────────────────────────────────────────
    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        self._last_call = time.monotonic()

    def _signature(self, params: dict[str, str]) -> str:
        base = "".join(f"{key}{params[key]}" for key in sorted(params))
        return hashlib.md5((base + self.api_secret).encode("utf-8")).hexdigest()

    def _call(self, params: dict[str, str], post: bool, signed: bool, retries: int = 3) -> ET.Element:
        payload = dict(params)
        payload["api_key"] = self.api_key
        if signed:
            payload["sk"] = self.session_key
            payload["api_sig"] = self._signature(payload)

        body = parse.urlencode(payload).encode("utf-8")
        for attempt in range(retries + 1):
            self._throttle()
            try:
                if post:
                    req = request.Request(
                        API_URL, data=body, method="POST",
                        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                                 "User-Agent": USER_AGENT})
                else:
                    req = request.Request(f"{API_URL}?{parse.urlencode(payload)}",
                                          headers={"User-Agent": USER_AGENT})
                with request.urlopen(req, timeout=30) as response:
                    raw = response.read()
            except error.HTTPError as exc:
                # 429/5xx are worth retrying; a 4xx that isn't 429 is a real error.
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise LastfmError(f"HTTP {exc.code} from Last.fm") from exc
            except (error.URLError, OSError) as exc:
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise LastfmError(f"network error talking to Last.fm: {exc}") from exc

            try:
                root = ET.fromstring(raw)
            except ET.ParseError as exc:
                raise LastfmError("Last.fm returned a response that is not XML") from exc

            if root.attrib.get("status") == "ok":
                return root
            node = root.find("error")
            code = node.attrib.get("code", "?") if node is not None else "?"
            message = (node.text or "").strip() if node is not None else "unknown error"
            if code == RATE_LIMIT_CODE and attempt < retries:
                time.sleep(2 ** attempt + 1)
                continue
            raise LastfmError(f"Last.fm error {code}: {message}")
        raise LastfmError("Last.fm request failed after retries")

    # ── auth ─────────────────────────────────────────────────────
    def authenticate(self, username: str, password: str) -> tuple[str, str]:
        root = self._call({"method": "auth.getMobileSession",
                           "username": username, "password": password},
                          post=True, signed=False if not self.api_secret else True)
        session = root.find("session")
        if session is None:
            raise LastfmError("authentication returned no session")
        name = (session.findtext("name") or "").strip()
        key = (session.findtext("key") or "").strip()
        if not (name and key):
            raise LastfmError("authentication returned an incomplete session")
        self.username, self.session_key = name, key
        return name, key

    # ── read ─────────────────────────────────────────────────────
    def loved_tracks(self, progress=None) -> dict[str, dict]:
        """Every loved track, paged. → {track_key: {"artist","title","when"}}"""
        if not self.username:
            raise LastfmError("no Last.fm username configured")
        loved: dict[str, dict] = {}
        page, pages = 1, 1
        while page <= pages:
            root = self._call({"method": "user.getLovedTracks", "user": self.username,
                               "limit": "1000", "page": str(page)}, post=False, signed=False)
            container = root.find("lovedtracks")
            if container is None:
                break
            pages = int(container.attrib.get("totalPages", "1") or 1)
            for track in container.findall("track"):
                title = (track.findtext("name") or "").strip()
                artist = (track.findtext("artist/name") or "").strip()
                if not (artist and title):
                    continue
                when = track.find("date")
                loved[track_key(artist, title)] = {
                    "artist": artist, "title": title,
                    "when": int(when.attrib.get("uts", "0")) if when is not None else 0,
                }
            if progress:
                progress(page, pages, len(loved))
            page += 1
        return loved

    # ── write ────────────────────────────────────────────────────
    def love(self, artist: str, title: str) -> None:
        self._call({"method": "track.love", "artist": artist, "track": title},
                   post=True, signed=True)

    def unlove(self, artist: str, title: str) -> None:
        self._call({"method": "track.unlove", "artist": artist, "track": title},
                   post=True, signed=True)


def from_env(env: dict | None = None) -> Lastfm | None:
    import os
    source = env or os.environ
    key = source.get("LASTFM_API_KEY", "").strip()
    secret = source.get("LASTFM_API_SECRET", "").strip()
    if not (key and secret):
        return None
    return Lastfm(api_key=key, api_secret=secret,
                  session_key=source.get("LASTFM_SESSION_KEY", "").strip(),
                  username=source.get("LASTFM_USERNAME", "").strip())
