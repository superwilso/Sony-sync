"""Minimal, dependency-free tag reader (FLAC / MP3-ID3 / MP4) with an on-disk cache.

Why not mutagen: this package has to run on whatever Python happens to be installed on the
Windows box next to MusicBee, with no venv and no pip step, exactly like `sync.py` does today.
Only four fields are ever needed — artist, title, album, track number — plus the rating, which
is the one field MusicBee can hand over through the files themselves.

Only the header of each file is read (a few KB), never the audio, and every result is cached by
(size, mtime) so a rescan of a 300-album library is a JSON load rather than 4000 file opens.
`mutagen` is used automatically when it IS importable, because it handles the long tail of
broken tags better than this does.
"""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional, never required
    import mutagen  # type: ignore
except Exception:  # pragma: no cover - depends on the host
    mutagen = None

AUDIO_EXT = (".flac", ".mp3", ".m4a", ".aac", ".alac", ".wav")


@dataclass
class Tags:
    artist: str = ""
    title: str = ""
    album: str = ""
    album_artist: str = ""
    track_no: int = 0
    rating: float = 0.0          # 0..5 stars, 0 = unrated
    duration: int = 0            # seconds, 0 = unknown
    source: str = ""             # "flac" | "id3" | "mp4" | "mutagen" | "filename"

    @property
    def best_artist(self) -> str:
        return self.artist or self.album_artist


# ───────────────────────────────────────────────────────────── FLAC

def _read_flac(path: str) -> Tags | None:
    with open(path, "rb") as handle:
        if handle.read(4) != b"fLaC":
            return None
        tags = Tags(source="flac")
        while True:
            header = handle.read(4)
            if len(header) < 4:
                break
            last = header[0] & 0x80
            block_type = header[0] & 0x7F
            length = int.from_bytes(header[1:4], "big")
            body = handle.read(length)
            if block_type == 0 and len(body) >= 18:      # STREAMINFO → duration
                bits = int.from_bytes(body[10:18], "big")
                rate = bits >> 44
                total = bits & ((1 << 36) - 1)
                if rate:
                    tags.duration = int(total / rate)
            elif block_type == 4:                        # VORBIS_COMMENT
                _apply_vorbis(tags, body)
            if last:
                break
    return tags


def _apply_vorbis(tags: Tags, body: bytes) -> None:
    offset = 0
    if len(body) < 4:
        return
    vendor_len = struct.unpack_from("<I", body, offset)[0]
    offset += 4 + vendor_len
    if offset + 4 > len(body):
        return
    count = struct.unpack_from("<I", body, offset)[0]
    offset += 4
    for _ in range(count):
        if offset + 4 > len(body):
            return
        size = struct.unpack_from("<I", body, offset)[0]
        offset += 4
        chunk = body[offset:offset + size]
        offset += size
        try:
            name, _, value = chunk.decode("utf-8", "replace").partition("=")
        except Exception:
            continue
        _set_field(tags, name.upper(), value)


def _set_field(tags: Tags, name: str, value: str) -> None:
    value = value.strip()
    if not value:
        return
    if name == "ARTIST" and not tags.artist:
        tags.artist = value
    elif name == "TITLE" and not tags.title:
        tags.title = value
    elif name == "ALBUM" and not tags.album:
        tags.album = value
    elif name in ("ALBUMARTIST", "ALBUM ARTIST") and not tags.album_artist:
        tags.album_artist = value
    elif name in ("TRACKNUMBER", "TRACK") and not tags.track_no:
        head = value.split("/")[0].strip()
        tags.track_no = int(head) if head.isdigit() else 0
    elif name in ("RATING", "FMPS_RATING") and not tags.rating:
        tags.rating = _parse_rating(value)


def _parse_rating(value: str) -> float:
    """MusicBee writes FMPS_RATING as 0..1 and RATING as 0..5 or 0..100. Normalise to stars."""
    try:
        number = float(value)
    except ValueError:
        return 0.0
    if number <= 1.0:
        return round(number * 5, 2)
    if number <= 5.0:
        return number
    return round(number / 20.0, 2)


# ───────────────────────────────────────────────────────────── ID3 (MP3)

_ID3_TEXT = {
    "TPE1": "artist", "TIT2": "title", "TALB": "album",
    "TPE2": "album_artist", "TRCK": "track_no",
    "TP1": "artist", "TT2": "title", "TAL": "album", "TRK": "track_no",
}


def _decode_text(payload: bytes) -> str:
    if not payload:
        return ""
    encoding, rest = payload[0], payload[1:]
    try:
        if encoding == 0:
            text = rest.decode("latin-1", "replace")
        elif encoding == 1:
            text = rest.decode("utf-16", "replace")
        elif encoding == 2:
            text = rest.decode("utf-16-be", "replace")
        else:
            text = rest.decode("utf-8", "replace")
    except Exception:
        return ""
    return text.split("\x00")[0].strip()


def _read_id3(path: str) -> Tags | None:
    with open(path, "rb") as handle:
        header = handle.read(10)
        if len(header) < 10 or header[:3] != b"ID3":
            return None
        major = header[3]
        flags = header[5]
        size = _syncsafe(header[6:10])
        body = handle.read(size)

    tags = Tags(source="id3")
    offset = 0
    if flags & 0x40:                       # extended header, skip it
        if major == 4:
            offset += _syncsafe(body[0:4])
        else:
            offset += 4 + int.from_bytes(body[0:4], "big")

    id_len, size_len = (3, 3) if major == 2 else (4, 4)
    while offset + id_len + size_len <= len(body):
        frame_id = body[offset:offset + id_len].decode("latin-1", "replace")
        if not frame_id.strip("\x00"):
            break
        raw_size = body[offset + id_len:offset + id_len + size_len]
        if major == 4:
            frame_size = _syncsafe(raw_size)
        else:
            frame_size = int.from_bytes(raw_size, "big")
        offset += id_len + size_len + (0 if major == 2 else 2)   # v2.3/2.4 have 2 flag bytes
        payload = body[offset:offset + frame_size]
        offset += frame_size
        if frame_size <= 0:
            break

        field_name = _ID3_TEXT.get(frame_id)
        if field_name:
            text = _decode_text(payload)
            if field_name == "track_no":
                head = text.split("/")[0].strip()
                if head.isdigit() and not tags.track_no:
                    tags.track_no = int(head)
            elif text and not getattr(tags, field_name):
                setattr(tags, field_name, text)
        elif frame_id in ("POPM", "POP") and not tags.rating:
            _, _, rest = payload.partition(b"\x00")
            if rest:
                tags.rating = _popm_to_stars(rest[0])
    return tags


def _popm_to_stars(byte: int) -> float:
    """POPM is 0..255 with de-facto anchor points; MusicBee/Windows use the same ladder."""
    for threshold, stars in ((0, 0.0), (1, 1.0), (64, 2.0), (128, 3.0), (196, 4.0), (255, 5.0)):
        if byte <= threshold:
            return stars
    return 5.0


def _syncsafe(raw: bytes) -> int:
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
    return value


# ───────────────────────────────────────────────────────────── MP4 / M4A

_MP4_FIELDS = {b"\xa9ART": "artist", b"\xa9nam": "title", b"\xa9alb": "album", b"aART": "album_artist"}


def _read_mp4(path: str) -> Tags | None:
    tags = Tags(source="mp4")
    with open(path, "rb") as handle:
        if not _mp4_walk(handle, 0, os.path.getsize(path), tags, depth=0):
            return None
    return tags


def _mp4_walk(handle, start: int, end: int, tags: Tags, depth: int) -> bool:
    """Descend moov→udta→meta→ilst only; every other branch is skipped without reading it."""
    containers = {b"moov", b"udta", b"meta", b"ilst"}
    found = False
    offset = start
    while offset + 8 <= end and depth < 6:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            break
        size = int.from_bytes(header[0:4], "big")
        atom = header[4:8]
        if size == 1:                                    # 64-bit size
            size = int.from_bytes(handle.read(8), "big")
            body_start = offset + 16
        else:
            body_start = offset + 8
        if size < 8:
            break
        if atom in containers:
            found = True
            inner = body_start + (4 if atom == b"meta" else 0)  # meta has a version/flags word
            _mp4_walk(handle, inner, offset + size, tags, depth + 1)
        elif atom in _MP4_FIELDS or atom == b"trkn":
            handle.seek(body_start)
            payload = handle.read(min(size - (body_start - offset), 4096))
            _apply_mp4_item(tags, atom, payload)
        offset += size
    return found


def _apply_mp4_item(tags: Tags, atom: bytes, payload: bytes) -> None:
    # payload is one or more child atoms; the one that matters is 'data'.
    cursor = 0
    while cursor + 8 <= len(payload):
        size = int.from_bytes(payload[cursor:cursor + 4], "big")
        kind = payload[cursor + 4:cursor + 8]
        if size < 8:
            return
        value = payload[cursor + 16:cursor + size]      # skip version/flags + locale
        if kind == b"data":
            if atom == b"trkn" and len(value) >= 4:
                if not tags.track_no:
                    tags.track_no = int.from_bytes(value[2:4], "big")
            else:
                text = value.decode("utf-8", "replace").strip()
                field_name = _MP4_FIELDS.get(atom)
                if field_name and text and not getattr(tags, field_name):
                    setattr(tags, field_name, text)
            return
        cursor += size


# ───────────────────────────────────────────────────────────── dispatch + cache

def _from_filename(path: str) -> Tags:
    """Last resort: `NN - Artist - Title.ext`, then `Artist - Title.ext`, then the album folder.

    The reference library is named exactly this way, so an unreadable or untagged file still
    lands on the right key instead of dropping out of the sync.
    """
    stem = Path(path).stem
    parts = [p.strip() for p in stem.split(" - ")]
    tags = Tags(source="filename")
    if len(parts) >= 3 and parts[0].isdigit():
        tags.track_no = int(parts[0])
        tags.artist, tags.title = parts[1], " - ".join(parts[2:])
    elif len(parts) == 2:
        tags.artist, tags.title = parts[0], parts[1]
    else:
        tags.title = stem
    folder = Path(path).parent.name
    if " - " in folder:
        artist, _, album = folder.partition(" - ")
        tags.album = album.strip()
        tags.artist = tags.artist or artist.strip()
    return tags


def _read_mutagen(path: str) -> Tags | None:
    if mutagen is None:
        return None
    try:
        audio = mutagen.File(path, easy=True)   # type: ignore[attr-defined]
    except Exception:
        return None
    if audio is None:
        return None
    def first(name: str) -> str:
        try:
            value = audio.get(name) or []
        except Exception:
            return ""
        return str(value[0]).strip() if value else ""
    tags = Tags(source="mutagen")
    tags.artist = first("artist")
    tags.title = first("title")
    tags.album = first("album")
    tags.album_artist = first("albumartist")
    head = first("tracknumber").split("/")[0]
    tags.track_no = int(head) if head.isdigit() else 0
    try:
        tags.duration = int(getattr(audio.info, "length", 0) or 0)
    except Exception:
        tags.duration = 0
    return tags if (tags.artist or tags.title) else None


def read_tags(path: str) -> Tags:
    suffix = Path(path).suffix.lower()
    reader = {".flac": _read_flac, ".mp3": _read_id3,
              ".m4a": _read_mp4, ".aac": _read_mp4, ".alac": _read_mp4}.get(suffix)
    tags: Tags | None = None
    try:
        if reader is not None:
            tags = reader(path)
        if tags is None or not (tags.artist and tags.title):
            better = _read_mutagen(path)
            if better is not None and better.artist and better.title:
                tags = better
    except (OSError, struct.error, ValueError):
        tags = None

    fallback = _from_filename(path)
    if tags is None:
        return fallback
    # Fill only what the file did not carry — never overwrite a real tag with a filename guess.
    tags.artist = tags.artist or fallback.artist
    tags.title = tags.title or fallback.title
    tags.album = tags.album or fallback.album
    tags.track_no = tags.track_no or fallback.track_no
    return tags


@dataclass
class TagCache:
    """(size, mtime)-keyed tag cache. A changed file is re-read; an unchanged one never is."""
    path: Path
    entries: dict[str, list] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    dirty: bool = False

    @classmethod
    def load(cls, path: str | Path) -> "TagCache":
        cache = cls(path=Path(path))
        try:
            data = json.loads(cache.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == 1:
                cache.entries = data.get("entries", {})
        except (OSError, ValueError):
            pass
        return cache

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": 1, "entries": self.entries}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass

    def get(self, path: str, size: int | None = None, mtime: float | None = None) -> Tags:
        key = os.path.normcase(path)
        if size is None or mtime is None:
            try:
                stat = os.stat(path)
                size, mtime = stat.st_size, stat.st_mtime
            except OSError:
                return _from_filename(path)
        cached = self.entries.get(key)
        if cached and cached[0] == size and abs(cached[1] - mtime) < 2:
            self.hits += 1
            return Tags(artist=cached[2], title=cached[3], album=cached[4],
                        album_artist=cached[5], track_no=cached[6], rating=cached[7],
                        duration=cached[8], source=cached[9])
        self.misses += 1
        tags = read_tags(path)
        self.entries[key] = [size, mtime, tags.artist, tags.title, tags.album,
                             tags.album_artist, tags.track_no, tags.rating,
                             tags.duration, tags.source]
        self.dirty = True
        return tags

    def prune(self, live_paths: set[str]) -> None:
        """Drop entries for files that no longer exist, so the cache can't grow forever."""
        live = {os.path.normcase(p) for p in live_paths}
        stale = [key for key in self.entries if key not in live]
        for key in stale:
            del self.entries[key]
        if stale:
            self.dirty = True
