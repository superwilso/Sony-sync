"""Index of the source library: track key → file.

Everything downstream needs this. A like arrives as `artist \t title` and has to become a path
before it can go into a playlist, and a rating lives on a file and has to become a key before it
can be compared with Last.fm. One scandir pass plus the tag cache does both.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .keys import artist_candidates, loose_key, norm_title, track_key
from .tags import TagCache, AUDIO_EXT


@dataclass
class Entry:
    rel_path: str          # relative to the source dir, e.g. "Artist - Album\\01 - Artist - Song.flac"
    artist: str
    title: str
    album: str
    track_no: int
    size: int
    rating: float
    duration: int

    @property
    def folder(self) -> str:
        parts = Path(self.rel_path).parts
        return parts[0] if parts else ""


class LibraryIndex:
    def __init__(self, source_dir: str) -> None:
        self.source_dir = source_dir
        self.entries: list[Entry] = []
        self.by_key: dict[str, list[Entry]] = {}
        self.by_loose: dict[str, list[Entry]] = {}
        # (album-folder artist, title). The reference library is foldered "Artist - Album", and a
        # featured-artist track inside it is tagged to the *guest* ("02 - Cleo Sol - Woman.flac"
        # inside "Little Simz - Sometimes I Might Be Introvert"), while Last.fm calls the same
        # track "Little Simz feat. Cleo Sol". This index is what joins those two.
        self.by_folder: dict[str, list[Entry]] = {}

    # ── build ────────────────────────────────────────────────────
    @classmethod
    def build(
        cls,
        source_dir: str,
        cache: TagCache,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> "LibraryIndex":
        index = cls(source_dir)
        files = list(_walk_audio(source_dir))
        total = len(files)
        for position, (path, size, mtime) in enumerate(files, start=1):
            tags = cache.get(path, size, mtime)
            entry = Entry(
                rel_path=os.path.normpath(os.path.relpath(path, source_dir)),
                artist=tags.best_artist, title=tags.title, album=tags.album,
                track_no=tags.track_no, size=size, rating=tags.rating, duration=tags.duration,
            )
            index.add(entry)
            if progress and (position % 64 == 0 or position == total):
                progress(position, total, entry.folder)
        cache.prune({path for path, _, _ in files})
        return index

    def add(self, entry: Entry) -> None:
        self.entries.append(entry)
        if not (entry.artist and entry.title):
            return
        self.by_key.setdefault(track_key(entry.artist, entry.title), []).append(entry)
        self.by_loose.setdefault(loose_key(entry.artist, entry.title), []).append(entry)
        folder_artist = entry.folder.split(" - ")[0] if " - " in entry.folder else ""
        if folder_artist:
            self.by_folder.setdefault(track_key(folder_artist, entry.title), []).append(entry)

    # ── lookup ───────────────────────────────────────────────────
    def resolve(self, artist: str, title: str) -> Entry | None:
        """Widest-to-narrowest match; the title always has to be exact.

        Order matters — each step is a weaker claim than the one before it, and the first hit
        wins, so a track that matches on its own tags never gets stolen by a folder-artist guess.
        Ties inside a step break on the shortest path, which prefers the canonical album over a
        compilation or a `(1)` duplicate.
        """
        candidates = [(self.by_key, track_key(artist, title)),
                      (self.by_loose, loose_key(artist, title))]
        title_norm = norm_title(title)
        for name in artist_candidates(artist):
            candidates.append((self.by_key, f"{name}\u241f{title_norm}"))
            candidates.append((self.by_folder, f"{name}\u241f{title_norm}"))
        for table, key in candidates:
            matches = table.get(key)
            if matches:
                return min(matches, key=lambda e: (len(e.rel_path), e.rel_path))
        return None

    def resolve_key(self, key: str) -> Entry | None:
        matches = self.by_key.get(key)
        if matches:
            return min(matches, key=lambda e: (len(e.rel_path), e.rel_path))
        return None

    def rated_at_least(self, stars: float) -> list[Entry]:
        if stars <= 0:
            return []
        return [entry for entry in self.entries if entry.rating >= stars]

    def key_for(self, entry: Entry) -> str:
        return track_key(entry.artist, entry.title)

    def __len__(self) -> int:
        return len(self.entries)


def _walk_audio(root: str) -> Iterable[tuple[str, int, float]]:
    """scandir walk that carries size/mtime out with it — the stat is already cached by the OS
    from the directory read, so the tag cache can be checked without a second syscall per file."""
    if not os.path.isdir(root):
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for item in iterator:
                    try:
                        if item.is_dir(follow_symlinks=False):
                            stack.append(item.path)
                        elif item.is_file(follow_symlinks=False) and item.name.lower().endswith(AUDIO_EXT):
                            stat = item.stat()
                            yield item.path, stat.st_size, stat.st_mtime
                    except OSError:
                        continue
        except OSError:
            continue
