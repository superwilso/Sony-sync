"""The MusicBee side.

MusicBee keeps its library in `MusicBeeLibrary.mbl`, an undocumented binary that it rewrites while
it runs — reading it is a reverse-engineering job with a moving target, and writing it is out of
the question. So this module uses the two interfaces MusicBee actually offers to other programs:

1. **A playlist file.** MusicBee exports any playlist (including an auto-playlist on `Love` or
   `Rating`) as `.m3u`/`.m3u8`, and re-imports one from disk. That is the read path, and it is
   also how the merged list gets back in front of you as "Liked Songs".
2. **File tags.** With "save ratings to file tags" on, MusicBee writes POPM/FMPS_RATING into the
   files themselves, which `tags.py` already reads. Optional second read path, off by default —
   the reference library currently has no ratings in its tags.

And a third route, indirectly: MusicBee's own Last.fm connection. A love in MusicBee is a
`track.love`, so it arrives here through `lastfm.py` with no MusicBee involvement at all. If that
is switched on in MusicBee, the playlist below is a convenience rather than a requirement.
"""
from __future__ import annotations

import os
from pathlib import Path

from .keys import track_key
from .library import Entry, LibraryIndex

PLAYLIST_EXT = (".m3u", ".m3u8")


def read_loved_playlist(path: str, source_dir: str, index: LibraryIndex) -> dict[str, dict]:
    """Resolve a MusicBee-exported playlist into track keys.

    Its lines are paths (the exported ones are relative to the playlist's own folder, e.g.
    `..\\my music lossless\\Artist - Album\\01 - Artist - Song.flac`), so each line is resolved to
    a file and then keyed from that file's tags — never from the filename alone, because the tag
    is what the device and Last.fm both saw.
    """
    playlist = Path(path)
    if not playlist.is_file():
        return {}
    try:
        body = playlist.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}

    source_root = Path(source_dir).resolve(strict=False)
    by_rel = {os.path.normcase(entry.rel_path): entry for entry in index.entries}
    loved: dict[str, dict] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip().strip('"')
        if not line or line.startswith("#"):
            continue
        entry = _resolve_line(line, playlist.parent, source_root, by_rel)
        if entry is None or not (entry.artist and entry.title):
            continue
        loved[track_key(entry.artist, entry.title)] = {"artist": entry.artist, "title": entry.title}
    return loved


def _resolve_line(line: str, playlist_dir: Path, source_root: Path,
                  by_rel: dict[str, Entry]) -> Entry | None:
    candidate = Path(line.replace("\\", os.sep).replace("/", os.sep))
    for base in (playlist_dir, source_root):
        try:
            resolved = candidate if candidate.is_absolute() else (base / candidate)
            relative = os.path.relpath(resolved.resolve(strict=False), source_root)
        except (OSError, ValueError):
            continue
        if relative.startswith(".."):
            continue
        found = by_rel.get(os.path.normcase(os.path.normpath(relative)))
        if found is not None:
            return found
    return None


def read_rated(index: LibraryIndex, threshold: float) -> dict[str, dict]:
    """Tag ratings at or above `threshold` stars, as a second MusicBee read path."""
    loved: dict[str, dict] = {}
    for entry in index.rated_at_least(threshold):
        if entry.artist and entry.title:
            loved[track_key(entry.artist, entry.title)] = {"artist": entry.artist, "title": entry.title}
    return loved


def write_playlist(path: str, entries: list[Entry], source_dir: str, dry_run: bool = False) -> int:
    """Write the merged liked list where MusicBee can pick it up.

    Paths are written relative to the playlist folder in Windows form, matching MusicBee's own
    exports byte for byte in style, so the file it reads back looks like one it wrote.
    """
    target = Path(path)
    lines = []
    for entry in entries:
        absolute = Path(source_dir) / entry.rel_path
        try:
            relative = os.path.relpath(absolute, target.parent)
        except ValueError:            # different drive: fall back to the absolute path
            relative = str(absolute)
        lines.append(relative.replace("/", "\\"))

    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text("#EXTM3U\n" + "\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    return len(lines)


def find_loved_playlist(playlist_dir: str) -> str:
    """Best guess at the MusicBee loved playlist: a file named for loves/likes/favourites."""
    if not os.path.isdir(playlist_dir):
        return ""
    wanted = ("loved", "love", "liked", "likes", "favourite", "favorite", "hearts")
    for name in sorted(os.listdir(playlist_dir)):
        stem, ext = os.path.splitext(name)
        if ext.lower() in PLAYLIST_EXT and stem.strip().casefold() in wanted:
            return os.path.join(playlist_dir, name)
    return ""
