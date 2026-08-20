"""Three-way likes sync: device ⇄ Last.fm ⇄ MusicBee.

The rule the whole thing turns on: **a source is only allowed to cause an unlike if it was seen
in the previous run.** Comparing each source against its own last snapshot is what separates "this
was just loved over here" from "this was just unliked over there" — set arithmetic across the
three current lists cannot tell those apart, and getting it wrong deletes a hand-curated list.

Everything else follows from that:

* a source that is **not available** this run (Walkman unplugged, Last.fm off, no MusicBee
  playlist) contributes nothing and its snapshot is left untouched — it is not evidence of an
  unlike;
* the **first** run has no snapshots, so it is purely additive: the union of all three, no
  deletions, in either direction;
* while an import written for the device is **still pending** (Cinder has not consumed it yet),
  the device is additive only — its export still shows the pre-push list, and treating that as
  removals would push the difference back out as unloves.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from . import device as device_mod
from . import musicbee as musicbee_mod
from .config import LikesConfig
from .keys import split_key
from .library import Entry, LibraryIndex
from .lastfm import Lastfm, LastfmError
from .state import State

SOURCES = ("device", "lastfm", "musicbee")
Progress = Callable[[str, int, int], None]


@dataclass
class SourceView:
    name: str
    available: bool = False
    tracks: dict[str, dict] = field(default_factory=dict)
    note: str = ""
    additive_only: bool = False      # present, but not trusted to prove a removal this run

    @property
    def count(self) -> int:
        return len(self.tracks)


@dataclass
class SinkPlan:
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    enabled: bool = True
    note: str = ""

    @property
    def touched(self) -> int:
        return len(self.add) + len(self.remove)


@dataclass
class Plan:
    desired: dict[str, dict] = field(default_factory=dict)
    views: dict[str, SourceView] = field(default_factory=dict)
    added_by: dict[str, set[str]] = field(default_factory=dict)
    removed_by: dict[str, set[str]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    sinks: dict[str, SinkPlan] = field(default_factory=dict)
    matched: dict[str, Entry] = field(default_factory=dict)     # key -> library file
    unmatched: list[str] = field(default_factory=list)          # liked, but no file locally
    playlist: dict[str, list[str]] = field(default_factory=dict)  # volume root -> rel paths
    off_device: list[str] = field(default_factory=list)         # matched, but not on any drive
    first_run: bool = False
    notes: list[str] = field(default_factory=list)

    def label(self, key: str) -> str:
        meta = self.desired.get(key) or {}
        if meta:
            return f"{meta.get('artist', '')} — {meta.get('title', '')}"
        artist, title = split_key(key)
        return f"{artist} — {title}"

    @property
    def total_changes(self) -> int:
        return sum(sink.touched for sink in self.sinks.values() if sink.enabled)


@dataclass
class ApplyResult:
    loved: int = 0
    unloved: int = 0
    lastfm_failed: list[tuple[str, str]] = field(default_factory=list)
    device_written: list[str] = field(default_factory=list)
    playlists_written: dict[str, int] = field(default_factory=dict)
    musicbee_written: int = 0
    logs: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.logs.append(message)


# ────────────────────────────────────────────────────────── gather

def gather(
    config: LikesConfig,
    index: LibraryIndex,
    lastfm: Lastfm | None,
    state: State,
    progress: Progress | None = None,
) -> dict[str, SourceView]:
    views: dict[str, SourceView] = {name: SourceView(name=name) for name in SOURCES}

    # ── device ──────────────────────────────────────────────────
    volumes = [volume for volume in device_mod.volumes(config.internal_root, config.sd_root)
               if volume.present]
    if volumes:
        view = views["device"]
        view.available = True
        view.tracks = device_mod.read_all(volumes)
        pending = [volume for volume in volumes if volume.import_pending]
        if pending:
            view.additive_only = True
            view.note = ("import from the last run is still on the device — Cinder has not "
                         "merged it yet, so removals here are not trusted")
        else:
            view.note = f"{len(volumes)} volume(s)"
    else:
        views["device"].note = "no Walkman volume found"
    if progress:
        progress("device", views["device"].count, views["device"].count)

    # ── Last.fm ─────────────────────────────────────────────────
    view = views["lastfm"]
    if not config.lastfm_enabled:
        view.note = "disabled in settings"
    elif lastfm is None or not lastfm.username:
        view.note = "not signed in"
    else:
        try:
            view.tracks = lastfm.loved_tracks(
                progress=(lambda page, pages, count: progress("lastfm", count, count))
                if progress else None)
            view.available = True
            view.note = f"as {lastfm.username}"
        except LastfmError as exc:
            view.note = str(exc)

    # ── MusicBee ────────────────────────────────────────────────
    view = views["musicbee"]
    playlist_path = config.musicbee_loved_playlist or musicbee_mod.find_loved_playlist(config.playlist_dir)
    collected: dict[str, dict] = {}
    notes: list[str] = []
    if playlist_path and os.path.isfile(playlist_path):
        collected.update(musicbee_mod.read_loved_playlist(playlist_path, config.source_dir, index))
        notes.append(os.path.basename(playlist_path))
        view.available = True
    if config.musicbee_rating_threshold > 0:
        rated = musicbee_mod.read_rated(index, config.musicbee_rating_threshold)
        collected.update(rated)
        notes.append(f"{len(rated)} rated >= {config.musicbee_rating_threshold:g}★")
        view.available = True
    view.tracks = collected
    view.note = ", ".join(notes) if notes else "no loved playlist and no tag ratings"
    if progress:
        progress("musicbee", view.count, view.count)

    return views


# ────────────────────────────────────────────────────────── plan

def build_plan(config: LikesConfig, index: LibraryIndex, views: dict[str, SourceView],
               state: State) -> Plan:
    plan = Plan(views=views, first_run=state.first_run)

    added_by: dict[str, set[str]] = {}
    removed_by: dict[str, set[str]] = {}
    for name, view in views.items():
        if not view.available:
            continue
        previous = state.snapshot(name).keys
        seen_before = bool(previous) or name in state.snapshots
        for key in view.tracks:
            if not seen_before or key not in previous:
                added_by.setdefault(key, set()).add(name)
        if seen_before and not view.additive_only:
            for key in previous:
                if key not in view.tracks:
                    removed_by.setdefault(key, set()).add(name)

    # Start from the last agreed truth; on a first run that is the union of what exists now.
    desired: dict[str, dict] = dict(state.liked)
    if not desired:
        for name in SOURCES:
            view = views[name]
            if view.available:
                for key, meta in view.tracks.items():
                    desired.setdefault(key, {"artist": meta["artist"], "title": meta["title"]})

    conflicts: list[str] = []
    for key in set(added_by) | set(removed_by):
        adders = added_by.get(key, set())
        removers = removed_by.get(key, set())
        if adders and removers:
            conflicts.append(key)
            if config.conflict != "latest":
                removers = set()          # "liked wins" — never lose a like to an ambiguity
        if adders and not removers:
            meta = _meta_for(key, views, adders)
            desired.setdefault(key, meta)
        elif removers:
            desired.pop(key, None)

    plan.desired = dict(sorted(desired.items(), key=lambda item: (
        item[1].get("artist", "").casefold(), item[1].get("title", "").casefold())))
    plan.added_by, plan.removed_by, plan.conflicts = added_by, removed_by, sorted(conflicts)

    # ── what each sink needs ────────────────────────────────────
    for name in SOURCES:
        view = views[name]
        sink = SinkPlan(enabled=view.available)
        if not view.available:
            sink.note = view.note
        else:
            sink.add = sorted(key for key in plan.desired if key not in view.tracks)
            sink.remove = sorted(key for key in view.tracks if key not in plan.desired)
        plan.sinks[name] = sink

    if not config.lastfm_enabled:
        plan.sinks["lastfm"].enabled = False
    # MusicBee's playlist is rewritten wholesale, so its "changes" are informational only.
    if not config.musicbee_export_playlist:
        plan.sinks["musicbee"].enabled = False

    # ── resolve to files, then to drives ────────────────────────
    volumes = [volume for volume in device_mod.volumes(config.internal_root, config.sd_root)
               if volume.present]
    for volume in volumes:
        plan.playlist[volume.root] = []

    for key, meta in plan.desired.items():
        entry = index.resolve(meta.get("artist", ""), meta.get("title", ""))
        if entry is None:
            plan.unmatched.append(key)
            continue
        plan.matched[key] = entry
        placed = False
        for volume in volumes:
            if os.path.isfile(os.path.join(volume.music_dir, entry.rel_path)):
                plan.playlist[volume.root].append(entry.rel_path)
                placed = True
                break        # one drive holds it; sync.py never puts the same file on both
        if volumes and not placed:
            plan.off_device.append(key)

    if plan.first_run:
        plan.notes.append("First run: nothing is removed anywhere — the three lists are merged.")
    if plan.unmatched:
        plan.notes.append(f"{len(plan.unmatched)} liked track(s) have no file in the library; "
                          "they stay on Last.fm but cannot go into a playlist.")
    if plan.off_device:
        plan.notes.append(f"{len(plan.off_device)} liked track(s) are in the library but not on "
                          "the Walkman — run the music sync to copy them.")
    return plan


def _meta_for(key: str, views: dict[str, SourceView], sources: set[str]) -> dict:
    """Prefer the device's spelling, then MusicBee's, then Last.fm's.

    The device and MusicBee both read the file's own tags; Last.fm shows whatever was scrobbled
    first by anyone, which is the least trustworthy of the three.
    """
    for name in ("device", "musicbee", "lastfm"):
        if name in sources:
            meta = views[name].tracks.get(key)
            if meta:
                return {"artist": meta["artist"], "title": meta["title"]}
    artist, title = split_key(key)
    return {"artist": artist, "title": title}


# ────────────────────────────────────────────────────────── apply

def apply_plan(
    config: LikesConfig,
    plan: Plan,
    state: State,
    index: LibraryIndex,
    lastfm: Lastfm | None,
    dry_run: bool = True,
    progress: Progress | None = None,
) -> ApplyResult:
    result = ApplyResult()

    # ── Last.fm ─────────────────────────────────────────────────
    sink = plan.sinks["lastfm"]
    lastfm_ok = True
    if sink.enabled and lastfm is not None and (sink.add or sink.remove):
        total = sink.touched
        done = 0
        for key in sink.add:
            meta = plan.desired[key]
            done += 1
            if progress:
                progress(f"love {meta['artist']} — {meta['title']}", done, total)
            if dry_run:
                result.loved += 1
                continue
            try:
                lastfm.love(meta["artist"], meta["title"])
                result.loved += 1
            except LastfmError as exc:
                lastfm_ok = False
                result.lastfm_failed.append((key, str(exc)))
        for key in sink.remove:
            meta = plan.views["lastfm"].tracks.get(key, {})
            done += 1
            if progress:
                progress(f"unlove {meta.get('artist', '')} — {meta.get('title', '')}", done, total)
            if dry_run:
                result.unloved += 1
                continue
            try:
                lastfm.unlove(meta.get("artist", ""), meta.get("title", ""))
                result.unloved += 1
            except LastfmError as exc:
                lastfm_ok = False
                result.lastfm_failed.append((key, str(exc)))
        result.log(f"Last.fm: {result.loved} loved, {result.unloved} unloved, "
                   f"{len(result.lastfm_failed)} failed")

    # ── device ──────────────────────────────────────────────────
    volumes = [volume for volume in device_mod.volumes(config.internal_root, config.sd_root)
               if volume.present]
    tracks = [plan.desired[key] for key in plan.desired]
    device_sink = plan.sinks["device"]
    device_needs_import = bool(device_sink.add or device_sink.remove)
    for position, volume in enumerate(volumes):
        # The liked store lives at /contents (the internal volume); the SD card has no copy.
        if config.device_import and position == 0:
            if device_needs_import:
                device_mod.write_import(volume, tracks, dry_run)
                result.device_written.append(str(volume.import_path))
                result.log(f"Device: wrote {len(tracks)} track(s) to {volume.import_path.name} "
                           f"(Cinder merges it on its next start)")
            elif volume.import_pending:
                # The device already holds exactly this list, so the pending import is a no-op —
                # and leaving it there would keep the device marked additive-only for ever, which
                # means an unlike made ON the device could never propagate. Clear it instead.
                if not dry_run:
                    try:
                        volume.import_path.unlink()
                    except OSError:
                        pass
                result.log(f"Device: dropped a redundant {volume.import_path.name} "
                           f"(the device is already in sync)")
        if config.device_playlist:
            rel_paths = plan.playlist.get(volume.root, [])
            if rel_paths:
                device_mod.write_playlist(volume, rel_paths, dry_run)
                result.playlists_written[volume.root] = len(rel_paths)
                result.log(f"Device: {volume.playlist_path} — {len(rel_paths)} track(s)")
            elif device_mod.remove_playlist(volume, dry_run):
                result.log(f"Device: removed empty {volume.playlist_path.name}")

    # ── MusicBee ────────────────────────────────────────────────
    if config.musicbee_export_playlist:
        target = os.path.join(config.playlist_dir, "Liked Songs.m3u8")
        entries = [plan.matched[key] for key in plan.desired if key in plan.matched]
        result.musicbee_written = musicbee_mod.write_playlist(
            target, entries, config.source_dir, dry_run)
        result.log(f"MusicBee: {target} — {result.musicbee_written} track(s)")

    # ── state ───────────────────────────────────────────────────
    if not dry_run:
        state.liked = dict(plan.desired)
        state.last_sync = time.time()
        # Each snapshot records what that source is expected to hold NOW. Last.fm only advances
        # if every write landed — a half-applied push must be retried, not forgotten.
        if plan.views["lastfm"].available and lastfm_ok:
            state.set_snapshot("lastfm", plan.desired)
        elif plan.views["lastfm"].available:
            merged = dict(plan.views["lastfm"].tracks)
            failed = {key for key, _ in result.lastfm_failed}
            for key, meta in plan.desired.items():
                if key not in failed:
                    merged[key] = meta
            state.set_snapshot("lastfm", merged)
        if plan.views["device"].available:
            # The device's own export still shows the old list until Cinder consumes the import;
            # `additive_only` on the next run is what keeps that from reading as removals.
            state.set_snapshot("device", plan.desired if config.device_import
                               else plan.views["device"].tracks)
        if plan.views["musicbee"].available or config.musicbee_export_playlist:
            state.set_snapshot("musicbee", plan.desired if config.musicbee_export_playlist
                               else plan.views["musicbee"].tracks)
        state.save()
    return result
