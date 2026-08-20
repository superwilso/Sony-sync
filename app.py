"""Walkman Sync — full-screen front-end for `sync.py` and `likesync`.

This file is *only* presentation. Every number it shows comes from `sync.py`'s planner or from
`likesync`'s engine, unchanged; nothing here decides what to copy, delete, love or unlove. That
split is deliberate — the old menu had counting logic sprinkled through its print statements, and
two of them disagreed with the planner.

Run it with `python sync.py` (this is the default front-end) or `python app.py`. The old
line-by-line menu is still there behind `python sync.py --classic`.

    ↑↓ move    ⏎ choose    esc back    / filter a list    q quit
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import sync
import tui
from tui import DIM, ListView, Panel, Screen, fit, gauge, human_size, pad, rule, style, truncate

try:
    from likesync.session import Session as LikesSession
    from likesync.lastfm import LastfmError
    LIKES_AVAILABLE = True
except Exception as exc:                      # the package is optional at runtime
    LIKES_AVAILABLE = False
    LIKES_IMPORT_ERROR = str(exc)


# ───────────────────────────────────────────────────── background work

@dataclass
class TaskProgress:
    message: str = ""
    current: int = 0
    total: int = 0
    eta: float | None = None
    lines: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def note(self, message: str) -> None:
        with self.lock:
            self.message = message
            self.lines.append(message)
            del self.lines[:-200]

    def step(self, current: int, total: int, message: str, eta: float | None = None) -> None:
        with self.lock:
            self.current, self.total, self.message, self.eta = current, total, message, eta
            if message and (not self.lines or self.lines[-1] != message):
                self.lines.append(message)
                del self.lines[:-200]

    def snapshot(self) -> tuple[str, int, int, float | None, list[str]]:
        with self.lock:
            return self.message, self.current, self.total, self.eta, list(self.lines[-12:])


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(int(round(seconds)), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def run_task(screen: Screen, title: str, work, progress: TaskProgress | None = None):
    """Run `work(progress)` off-thread, animating a progress screen until it returns.

    A scan of two USB volumes takes tens of seconds and the old front-end simply froze for the
    duration, which is indistinguishable from a hang. Here the frame keeps moving and says which
    phase it is in.
    """
    progress = progress or TaskProgress()
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = work(progress)
        except BaseException as exc:          # surfaced on the calling thread, never swallowed
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    frame = 0
    while thread.is_alive():
        screen.draw(render_task(screen, title, progress, frame))
        frame += 1
        time.sleep(0.08)
    thread.join()
    screen.draw(render_task(screen, title, progress, frame, finished=True))
    if "error" in box:
        raise box["error"]
    return box.get("result")


def render_task(screen: Screen, title: str, progress: TaskProgress, frame: int,
                finished: bool = False) -> list[str]:
    width, height = screen.size
    message, current, total, eta, recent = progress.snapshot()
    spin = tui.SPINNER[frame % len(tui.SPINNER)]
    lines = [
        style(fit(f" {title}", width), fg="black", bg="bar", bold=True) if tui.COLOUR
        else fit(f" {title}", width),
        "",
    ]
    if total:
        fraction = min(current / total, 1.0)
        bar_width = max(width - 34, 10)
        lines.append(f"  {style(tui.bar(fraction, bar_width), fg='amber')} "
                     f"{current}/{total}  ETA {format_eta(eta)}")
    else:
        lines.append(f"  {spin if not finished else '✓'} working…")
    lines.append(f"  {style(truncate(message, width - 4), dim=True)}")
    lines.append("")
    lines.append(f"  {style('recent', dim=True)}")
    for entry in recent[-(height - 10):]:
        lines.append(f"    {style(truncate(entry, width - 6), dim=True)}")
    return lines


# ───────────────────────────────────────────────────── dashboard

def source_used(report: sync.ScanReport, config: sync.AppConfig, drive: str) -> int:
    scan = report.device_scans.get(drive)
    return scan.total_size if scan is not None else 0


def render_header(app: "App", width: int) -> list[str]:
    device = "connected" if os.path.isdir(app.config.internal_drive) else "not connected"
    device_style = "green" if device == "connected" else "grey"
    user = app.lastfm_user or "not signed in"
    left = " WALKMAN SYNC"
    right = f"{style('walkman', dim=True)} {style(device, fg=device_style)}   " \
            f"{style('last.fm', dim=True)} {user} "
    gap = max(width - tui.cell_width(left) - tui.cell_width(right), 1)
    header = left + " " * gap + right
    return [style(header, bold=True) if tui.COLOUR else header,
            style(rule(width), fg="grey")]


def render_dashboard(app: "App") -> list[str]:
    width, height = app.screen.size
    report, config = app.report, app.config
    lines = render_header(app, width)

    # Two columns need ~34 cells each to stay readable; below that the panels stack full-width.
    narrow = width < 72
    column = width if narrow else max((width - 3) // 2, 30)
    # ── left: what is on the device ─────────────────────────────
    internal_used = source_used(report, config, config.internal_drive) if report else 0
    sd_used = source_used(report, config, config.sd_drive) if report else 0
    device_rows = [
        f"Internal  {gauge(internal_used, config.internal_max_bytes, 18)} "
        f"{human_size(internal_used)} / {config.internal_max_gb:.0f} GB",
        f"SD card   {gauge(sd_used, config.sd_max_bytes, 18)} "
        f"{human_size(sd_used)} / {config.sd_max_gb:.0f} GB",
        "",
        f"Source    {human_size(report.source_total_bytes) if report else '—'}"
        f"   {len(report.plan.source_artists) if report and report.plan else 0} albums",
        f"Excluded  {sync.format_album_exclusion_summary(config.excluded_album_entries)}",
    ]
    device_panel = Panel("Storage", device_rows, accent="cyan").render(column)

    # ── right: what this run would change ───────────────────────
    change_rows: list[str] = []
    if report is None or report.plan is None:
        change_rows.append(style("no scan yet — press r", dim=True))
    else:
        diff = report.album_diff
        if diff is not None:
            net_count = diff.net_count
            net_size = diff.net_size
            sign = "+" if net_count >= 0 else ""
            size_sign = "+" if net_size >= 0 else "-"
            change_rows.append(
                f"Net       {style(f'{sign}{net_count} albums', fg='green' if net_count >= 0 else 'red')}"
                f"  {size_sign}{human_size(abs(net_size))}")
            change_rows.append(
                f"          {style(f'+{len(diff.additions)}', fg='green')} add   "
                f"{style(f'-{len(diff.deletions)}', fg='red')} remove   "
                f"{style(f'~{len(diff.moves)}', fg='yellow')} move")
        change_rows.append("")
        change_rows.append(f"Files to copy   {_pending_files(report)}")
        change_rows.append(f"Files to remove {len(report.stale_music_files)}")
        change_rows.append(f"Playlists       {len(report.plan.playlist_drive)} placed, "
                           f"{len(report.stale_playlists)} stale")
        duplicates = len(report.duplicate_tracks)
        if duplicates:
            change_rows.append(style(f"Duplicates      {duplicates}", fg="yellow"))
        for label, info in sync.overflow_warnings(config, report):
            change_rows.append(style(f"OVERFLOW {label}: short {human_size(info.shortfall_bytes)}",
                                     fg="red", bold=True))
    change_panel = Panel("This sync", change_rows, accent="amber").render(column)
    likes_panel = Panel("Likes", app.likes_summary_rows(), accent="magenta").render(column)
    scrobble_panel = Panel("Scrobbles", app.scrobble_summary_rows(), accent="green").render(column)
    if narrow:
        storage_row = device_panel + change_panel
        likes_row = likes_panel + scrobble_panel
    else:
        storage_row = tui.side_by_side(device_panel, change_panel, column)
        likes_row = tui.side_by_side(likes_panel, scrobble_panel, column)

    # ── menu ────────────────────────────────────────────────────
    # The menu is the one thing that must never be pushed off the bottom, so the panels are the
    # part that gives: on a normal window both rows fit, on a short one the likes/scrobbles row
    # is dropped first and the storage row second. The final slice is a hard guarantee.
    footer = 3 + (1 if app.toast else 0)          # blank + rule + hints (+ toast)
    menu_min = 4
    for section in (storage_row, likes_row):
        if len(lines) + len(section) + footer + menu_min <= height - 1:
            lines += section

    menu_height = max(min(len(app.actions), height - len(lines) - footer - 1), menu_min)
    app.menu.clamp(menu_height)
    lines.append("")
    lines += app.menu.render(width, menu_height)
    lines.append(style(rule(width), fg="grey"))
    hints = [("↑↓", "move"), ("⏎", "choose"), ("r", "rescan"), ("/", "filter"), ("q", "quit")]
    lines.append(tui.key_hint(hints, width))
    if app.toast:
        lines.append(style(fit("  " + app.toast, width), fg="amber"))
    return lines[:height - 1]


def _pending_files(report: sync.ScanReport) -> int:
    return sum(info.files_to_copy for info in report.space_projection.values())


# ───────────────────────────────────────────────────── list screens

def screen_list(app: "App", title: str, rows: list[str], detail=None,
                hints: list[tuple[str, str]] | None = None) -> None:
    """A generic scrollable list screen; `detail(index)` opens a sub-screen on Enter."""
    view = ListView(items=rows)
    while True:
        width, height = app.screen.size
        body_height = max(height - 6, 3)
        lines = render_header(app, width)
        lines.append(style(fit(f" {title}", width), bold=True))
        lines += view.render(width, body_height)
        lines.append(style(rule(width), fg="grey"))
        lines.append(view.status(width))
        lines.append(tui.key_hint(hints or [("↑↓", "move"), ("/", "filter"), ("esc", "back")], width))
        app.screen.draw(lines)

        key = tui.read_key()
        if view.handle(key, body_height):
            continue
        if key in ("esc", "q"):
            return
        if key == "enter" and detail is not None:
            selected = view.selected()
            if selected is not None:
                detail(selected)


def screen_text(app: "App", title: str, rows: list[str]) -> None:
    screen_list(app, title, rows, hints=[("↑↓", "scroll"), ("esc", "back")])


# ───────────────────────────────────────────────────── the app

MenuItem = tuple[str, str, str]      # (hotkey, label, action)


class App:
    def __init__(self) -> None:
        self.config = sync.AppConfig()
        self.screen = Screen()
        self.report: sync.ScanReport | None = None
        self.toast = ""
        self.likes: "LikesSession | None" = None
        self.likes_plan = None
        self.menu = ListView(items=[])
        self._build_menu()

    # ── menu ────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        self.actions: list[MenuItem] = [
            ("1", "Sync music to the Walkman", "sync"),
            ("2", "Album changes (what moves)", "changes"),
            ("3", "Album plan", "albums"),
            ("4", "Playlists", "playlists"),
            ("5", "Pending removals", "removals"),
            ("6", "Duplicates across drives", "duplicates"),
            ("7", "Likes: device ⇄ Last.fm ⇄ MusicBee", "likes"),
            ("8", "Scrobbles: upload to Last.fm", "scrobbles"),
            ("9", "Clean up stale files", "cleanup"),
            ("s", "Settings", "settings"),
            ("l", "Last.fm account", "lastfm"),
            ("q", "Quit", "quit"),
        ]
        self.menu.items = [f"{style(key, fg='amber', bold=True)}  {label}"
                           for key, label, _ in self.actions]

    # ── summaries for the dashboard panels ──────────────────────
    def likes_summary_rows(self) -> list[str]:
        if not LIKES_AVAILABLE:
            return [style(f"likesync unavailable: {LIKES_IMPORT_ERROR}", fg="red")]
        if self.likes_plan is None:
            return [style("not loaded — open Likes (7) to read the three sources", dim=True)]
        plan = self.likes_plan
        rows = [f"Merged    {len(plan.desired)} liked track(s)"]
        for name in ("device", "lastfm", "musicbee"):
            view = plan.views[name]
            sink = plan.sinks[name]
            mark = style("ok", fg="green") if view.available else style("--", dim=True)
            change = ""
            if view.available and sink.enabled and (sink.add or sink.remove):
                change = (f"  {style('+' + str(len(sink.add)), fg='green')}"
                          f" {style('-' + str(len(sink.remove)), fg='red')}")
            rows.append(f"{mark} {name:<9} {view.count:>4}{change}")
        on_device = sum(len(paths) for paths in plan.playlist.values())
        rows.append(f"Playlist  {on_device} track(s) on the Walkman")
        return rows

    def scrobble_summary_rows(self) -> list[str]:
        files = sync.list_scrobble_files(self.config)
        pending = sum(1 for f in files for e in f.entries if e.source_flag.upper() == "L")
        skipped = sum(1 for f in files for e in f.entries if e.source_flag.upper() != "L")
        rows = [f"Logs      {len(files)} file(s)",
                f"Pending   {style(str(pending), fg='amber') if pending else '0'} play(s)",
                f"Skipped   {skipped}"]
        if not files:
            rows.append(style("nothing to upload — plug the Walkman in", dim=True))
        return rows

    # ── scanning ────────────────────────────────────────────────
    def rescan(self) -> None:
        def work(progress: TaskProgress):
            return sync.build_scan_report(self.config, status=progress.note)
        self.report = run_task(self.screen, "Scanning library and device", work)
        if self.report and self.report.plan is None:
            self.toast = "scan failed — check the source and playlist paths in Settings"
        else:
            self.toast = ""

    # ── run loop ────────────────────────────────────────────────
    def run(self) -> None:
        with self.screen:
            self.rescan()
            while True:
                self.screen.draw(render_dashboard(self))
                key = tui.read_key()
                height = max(self.screen.size[1] - 14, 4)
                if self.menu.handle(key, height):
                    continue
                if key in ("q", "esc"):
                    return
                action = None
                if key == "enter":
                    selected = self.menu.selected()
                    if selected is not None:
                        action = self.actions[selected][2]
                else:
                    for hotkey, _, name in self.actions:
                        if key == hotkey:
                            action = name
                            break
                    if key == "r":
                        self.rescan()
                        continue
                if action == "quit":
                    return
                if action:
                    self.dispatch(action)

    def dispatch(self, action: str) -> None:
        handler = getattr(self, f"do_{action}", None)
        if handler is None:
            self.toast = f"not implemented: {action}"
            return
        try:
            handler()
        except KeyboardInterrupt:
            self.toast = "cancelled"
        except OSError as exc:
            self.pause_message("Something went wrong", [str(exc)])

    # ── helpers ─────────────────────────────────────────────────
    def confirm(self, question: str) -> bool:
        answer = self.screen.prompt(f"{question} [y/N]: ").strip().lower()
        return answer == "y"

    def pause_message(self, title: str, rows: list[str]) -> None:
        screen_text(self, title, rows + ["", style("press esc to go back", dim=True)])

    @property
    def lastfm_user(self) -> str:
        return os.environ.get("LASTFM_USERNAME", "") or self.config.lastfm_username

    def ensure_report(self) -> bool:
        if self.report is None or self.report.plan is None:
            self.toast = "no scan data — press r"
            return False
        return True

    def ensure_likes(self) -> bool:
        if not LIKES_AVAILABLE:
            self.pause_message("Likes unavailable", [LIKES_IMPORT_ERROR])
            return False
        if self.likes is None:
            self.likes = LikesSession()
        return True

    # ── actions: music sync ─────────────────────────────────────
    def do_sync(self) -> None:
        if not self.ensure_report():
            return
        preview = run_task(self.screen, "Sync preview", lambda p: sync.sync_library(
            self.config, dry_run=True, progress_callback=p.step))
        rows = result_rows(preview)
        warnings = sync.overflow_warnings(self.config, self.report)
        for label, info in warnings:
            rows.insert(0, style(f"OVERFLOW {label}: short by {human_size(info.shortfall_bytes)}",
                                 fg="red", bold=True))
        screen_text(self, "Sync preview — nothing written yet", rows)
        if not self.confirm("Run the real sync now?"):
            self.toast = "sync cancelled"
            return
        try:
            result = run_task(self.screen, "Syncing", lambda p: sync.sync_library(
                self.config, dry_run=False, progress_callback=p.step))
        except OSError as exc:
            self.pause_message("Sync stopped — not enough free space", [str(exc)])
            self.rescan()
            return
        screen_text(self, "Sync complete", result_rows(result))
        self.rescan()

    def do_cleanup(self) -> None:
        if not self.ensure_report():
            return
        preview = run_task(self.screen, "Cleanup preview", lambda p: sync.cleanup_library(
            self.config, dry_run=True, progress_callback=p.step))
        screen_text(self, "Cleanup preview", result_rows(preview))
        if not self.confirm("Delete the stale files listed above?"):
            return
        result = run_task(self.screen, "Cleaning up", lambda p: sync.cleanup_library(
            self.config, dry_run=False, progress_callback=p.step))
        screen_text(self, "Cleanup complete", result_rows(result))
        self.rescan()

    def do_changes(self) -> None:
        if not self.ensure_report():
            return
        screen_text(self, "Album changes", album_diff_rows(self.config, self.report))

    def do_albums(self) -> None:
        if not self.ensure_report():
            return
        plan = self.report.plan
        folders: list[str] = []
        rows: list[str] = []
        for drive in (self.config.internal_drive, self.config.sd_drive):
            assigned = sorted([f for f, d in plan.assignments.items() if d == drive], key=str.lower)
            total = sum(plan.folder_sizes[f] for f in assigned)
            rows.append(style(f"{sync.drive_label(drive, self.config)}  "
                              f"{len(assigned)} albums, {human_size(total)}", bold=True))
            folders.append("")
            for folder in assigned:
                playlists = len(plan.folder_playlists.get(folder, set()))
                rows.append(f"  {fit(folder, 46)} {human_size(plan.folder_sizes[folder]):>10}"
                            f"  {playlists} playlist(s)")
                folders.append(folder)

        def detail(index: int) -> None:
            folder = folders[index]
            if not folder:
                return
            info = [f"Planned drive : {sync.drive_label(plan.assignments.get(folder, ''), self.config)}",
                    f"Size          : {human_size(plan.folder_sizes.get(folder, 0))}", ""]
            info += [f"playlist: {name}" for name in
                     sorted(plan.folder_playlists.get(folder, set()), key=str.lower)]
            info.append("")
            info += [f"  {name}" for name in sync.sample_folder_files(self.config.source_dir, folder, 20)]
            screen_text(self, folder, info)

        screen_list(self, "Album plan", rows, detail,
                    hints=[("↑↓", "move"), ("⏎", "open"), ("/", "filter"), ("esc", "back")])

    def do_playlists(self) -> None:
        if not self.ensure_report():
            return
        plan = self.report.plan
        rows = []
        for name in sorted(plan.playlist_names, key=str.lower):
            drive = plan.playlist_drive.get(name)
            tracks = plan.playlist_entries.get(name, [])
            label = sync.drive_label(drive, self.config) if drive else style("unplaced", fg="yellow")
            rows.append(f"{fit(name, 44)} {len(tracks):>4} tracks   {label}")
        screen_text(self, "Playlists", rows or ["no playlists found"])

    def do_removals(self) -> None:
        if not self.ensure_report():
            return
        rows = [style(f"Music files ({len(self.report.stale_music_files)})", bold=True)]
        rows += [f"  {path}" for path in self.report.stale_music_files]
        rows.append(style(f"Playlists ({len(self.report.stale_playlists)})", bold=True))
        rows += [f"  {path}" for path in self.report.stale_playlists]
        screen_text(self, "Pending removals", rows)

    def do_duplicates(self) -> None:
        if not self.ensure_report():
            return
        rows = [f"  {path}" for path in self.report.duplicate_tracks] or ["none"]
        screen_text(self, f"Duplicates across drives ({len(self.report.duplicate_tracks)})", rows)

    # ── actions: scrobbles ──────────────────────────────────────
    def do_scrobbles(self) -> None:
        files = sync.list_scrobble_files(self.config)
        if not files:
            self.pause_message("Scrobbles", ["No scrobble log found on the Walkman."])
            return
        rows: list[str] = []
        pending = 0
        for scrobble_file in files:
            rows.append(style(f"{scrobble_file.device_label}: {scrobble_file.path}", bold=True))
            for entry in scrobble_file.entries:
                listened = entry.source_flag.upper() == "L"
                pending += listened
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.timestamp))
                mark = style("upload", fg="green") if listened else style("skip  ", dim=True)
                rows.append(f"  {mark} {when}  {fit(entry.artist, 24)} {truncate(entry.track, 34)}")
        screen_text(self, f"Scrobbles — {pending} to upload", rows)
        if pending == 0 or not self.confirm("Upload these plays to Last.fm and clear them?"):
            return
        result = run_task(self.screen, "Uploading scrobbles", lambda p: sync.upload_scrobbles(
            self.config, dry_run=False, progress_callback=p.step, retries=3))
        screen_text(self, "Scrobble upload complete", result_rows(result))

    # ── actions: likes ──────────────────────────────────────────
    def do_likes(self) -> None:
        if not self.ensure_likes():
            return
        assert self.likes is not None
        if self.likes_plan is None:
            self.refresh_likes()
        # One view for the whole screen, not one per frame — a fresh ListView each pass would
        # reset the scroll position on every keypress.
        view = ListView()
        while True:
            plan = self.likes_plan
            if plan is None:
                return
            view.items = likes_rows(plan)
            width, height = self.screen.size
            lines = render_header(self, width)
            lines.append(style(fit(" Likes — device ⇄ Last.fm ⇄ MusicBee", width), bold=True))
            body = max(height - 8, 4)
            lines += view.render(width, body)
            lines.append(style(rule(width), fg="grey"))
            lines.append(view.status(width))
            lines.append(tui.key_hint(
                [("↑↓", "scroll"), ("a", "apply"), ("d", "dry run"), ("r", "reload"),
                 ("p", "playlists only"), ("c", "conflict rule"), ("esc", "back")], width))
            self.screen.draw(lines)

            key = tui.read_key()
            if view.handle(key, body):
                continue
            if key in ("esc", "q"):
                return
            if key == "r":
                self.refresh_likes()
            elif key == "d":
                self.apply_likes(dry_run=True)
            elif key == "a":
                if self.confirm("Apply these likes everywhere (writes to Last.fm and the device)?"):
                    self.apply_likes(dry_run=False)
            elif key == "p":
                self.likes.config.device_import = False
                self.likes.config.lastfm_enabled = False
                self.apply_likes(dry_run=False)
                self.likes = None
                self.ensure_likes()
                self.refresh_likes()
            elif key == "c":
                config = self.likes.config
                config.conflict = "latest" if config.conflict == "liked" else "liked"
                config.save()
                self.toast = f"conflict rule: {config.conflict} wins"
                self.refresh_likes()

    def refresh_likes(self) -> None:
        assert self.likes is not None

        def work(progress: TaskProgress):
            return self.likes.refresh(
                status=progress.note,
                progress=lambda done, total, folder: progress.step(done, total, folder))
        self.likes_plan = run_task(self.screen, "Reading likes", work)

    def apply_likes(self, dry_run: bool) -> None:
        assert self.likes is not None
        result = run_task(self.screen, "Dry run" if dry_run else "Applying likes",
                          lambda p: self.likes.apply(dry_run=dry_run, status=p.note))
        rows = [style(line, dim=dry_run) for line in result.logs]
        if result.lastfm_failed:
            rows.append(style(f"{len(result.lastfm_failed)} Last.fm call(s) failed", fg="red"))
            rows += [f"  {key}: {reason}" for key, reason in result.lastfm_failed[:20]]
        rows.append("")
        rows.append(style("dry run — nothing was written" if dry_run else "applied", dim=True))
        screen_text(self, "Likes", rows)
        if not dry_run:
            self.refresh_likes()

    # ── actions: settings ───────────────────────────────────────
    def do_settings(self) -> None:
        fields = [
            ("Source library", "source_dir", "path"),
            ("Playlist folder", "playlist_dir", "path"),
            ("Internal music path", "internal_drive", "path"),
            ("SD card music path", "sd_drive", "path"),
            ("Internal limit (GB)", "internal_max_gb", "float"),
            ("Internal safety buffer (GB)", "internal_safety_buffer_gb", "float"),
            ("SD limit (GB)", "sd_max_gb", "float"),
            ("Excluded albums", "excluded_albums", "albums"),
            ("Re-detect Walkman drives", "", "detect"),
        ]
        view = ListView(items=[])
        while True:
            view.items = [f"{fit(label, 30)} {style(str(getattr(self.config, attr, '')), dim=True)}"
                          if attr else style(label, fg="cyan")
                          for label, attr, _ in fields]
            width, height = self.screen.size
            lines = render_header(self, width)
            lines.append(style(fit(" Settings", width), bold=True))
            body = max(height - 7, 4)
            lines += view.render(width, body)
            lines.append(style(rule(width), fg="grey"))
            lines.append(tui.key_hint([("⏎", "edit"), ("esc", "back")], width))
            self.screen.draw(lines)

            key = tui.read_key()
            if view.handle(key, body):
                continue
            if key in ("esc", "q"):
                return
            if key != "enter":
                continue
            index = view.selected()
            if index is None:
                continue
            label, attr, kind = fields[index]
            if kind == "detect":
                sync.autodetect_storage_paths(self.config)
                self.toast = f"internal {self.config.internal_drive}, sd {self.config.sd_drive}"
                continue
            if kind == "albums":
                self.edit_exclusions()
                continue
            answer = self.screen.prompt(f"{label} [{getattr(self.config, attr)}]: ").strip()
            if not answer:
                continue
            if kind == "float":
                try:
                    setattr(self.config, attr, float(answer))
                except ValueError:
                    self.toast = "not a number"
            else:
                setattr(self.config, attr, answer)

    def edit_exclusions(self) -> None:
        """Pick excluded albums from the real source list, with a filter — no typing names."""
        albums, _ = sync.list_selectable_album_folders(self.config.source_dir)
        chosen = {sync.canonical_folder_key(name)
                  for name in sync.parse_album_exclusion_entries(self.config.excluded_albums)}
        view = ListView(items=[])
        while True:
            view.items = [
                f"[{'x' if sync.canonical_folder_key(name) in chosen else ' '}] {name}"
                for name in albums]
            width, height = self.screen.size
            lines = render_header(self, width)
            lines.append(style(fit(f" Excluded albums — {len(chosen)} selected", width), bold=True))
            body = max(height - 8, 4)
            lines += view.render(width, body)
            lines.append(style(rule(width), fg="grey"))
            lines.append(view.status(width))
            lines.append(tui.key_hint(
                [("space/⏎", "toggle"), ("/", "filter"), ("s", "save"), ("esc", "cancel")], width))
            self.screen.draw(lines)

            key = tui.read_key()
            if view.handle(key, body):
                continue
            if key == "esc":
                return
            if key in ("enter", " "):
                index = view.selected()
                if index is not None:
                    folder_key = sync.canonical_folder_key(albums[index])
                    chosen.symmetric_difference_update({folder_key})
            elif key == "s":
                selected = [name for name in albums if sync.canonical_folder_key(name) in chosen]
                value = ", ".join(sorted(selected, key=str.lower))
                self.config.excluded_albums = value
                try:
                    sync.save_env_value("SYNC_EXCLUDED_ALBUMS", value)
                    self.toast = f"saved {len(selected)} exclusion(s)"
                except OSError as exc:
                    self.toast = f"could not save: {exc}"
                self.rescan()
                return

    def do_lastfm(self) -> None:
        while True:
            width, height = self.screen.size
            rows = [
                f"User    {self.lastfm_user or style('not signed in', fg='yellow')}",
                f"API key {sync.mask_secret(self.config.lastfm_api_key)}",
                f"Secret  {sync.mask_secret(self.config.lastfm_api_secret)}",
                f"Session {'ready' if os.environ.get('LASTFM_SESSION_KEY') else style('missing', fg='yellow')}",
            ]
            lines = render_header(self, width)
            lines.append(style(fit(" Last.fm account", width), bold=True))
            lines += [fit("  " + row, width) for row in rows]
            lines.append("")
            lines.append(tui.key_hint([("i", "sign in"), ("k", "api key"), ("s", "api secret"),
                                       ("esc", "back")], width))
            self.screen.draw(lines)

            key = tui.read_key()
            if key in ("esc", "q"):
                return
            if key == "i":
                self.sign_in()
            elif key == "k":
                value = self.screen.prompt("Last.fm API key: ").strip()
                if value:
                    self.config.lastfm_api_key = value
                    sync.save_env_value("LASTFM_API_KEY", value)
            elif key == "s":
                value = self.screen.prompt("Last.fm API secret: ").strip()
                if value:
                    self.config.lastfm_api_secret = value
                    sync.save_env_value("LASTFM_API_SECRET", value)

    def sign_in(self) -> None:
        import getpass
        username = self.screen.prompt(f"Last.fm username [{self.lastfm_user}]: ").strip() \
            or self.lastfm_user
        if not username:
            self.toast = "a username is required"
            return
        self.screen.leave()
        try:
            password = getpass.getpass("Last.fm password: ")
        finally:
            self.screen.enter()
        if not password:
            self.toast = "cancelled"
            return
        try:
            name, session_key = sync.fetch_lastfm_session(self.config, username, password)
        except Exception as exc:
            self.pause_message("Sign-in failed", [str(exc)])
            return
        self.config.lastfm_username, self.config.lastfm_session_key = name, session_key
        sync.save_env_value("LASTFM_USERNAME", name)
        sync.save_env_value("LASTFM_SESSION_KEY", session_key)
        self.toast = f"signed in as {name}"
        self.likes = None
        self.likes_plan = None


# ───────────────────────────────────────────────────── row builders

def result_rows(result: sync.SyncResult) -> list[str]:
    rows = [
        f"Files copied       {result.files_copied}",
        f"Playlists written  {result.playlists_written}",
        f"Files deleted      {result.files_deleted}",
        f"Playlists deleted  {result.playlists_deleted}",
    ]
    if result.scrobbles_uploaded or result.scrobbles_rejected:
        rows += [f"Scrobbles uploaded {result.scrobbles_uploaded}",
                 f"Scrobbles rejected {result.scrobbles_rejected}"]
    rows += ["", style("activity", dim=True)]
    rows += [f"  {line}" for line in result.logs]
    return rows


def album_diff_rows(config: sync.AppConfig, report: sync.ScanReport) -> list[str]:
    diff = report.album_diff
    if diff is None:
        return ["no diff available"]
    rows: list[str] = []
    sign = "+" if diff.net_count >= 0 else ""
    rows.append(style(f"Net change: {sign}{diff.net_count} album(s), "
                      f"{'+' if diff.net_size >= 0 else '-'}{human_size(abs(diff.net_size))}", bold=True))
    if diff.additions:
        rows.append(style(f"+++ {len(diff.additions)} addition(s), "
                          f"{human_size(diff.additions_size)}", fg="green", bold=True))
        for folder, size, dest in diff.additions:
            rows.append(style(f"  + {fit(folder, 44)} {human_size(size):>10}  → "
                              f"{sync.drive_label(dest, config)}", fg="green"))
    if diff.deletions:
        rows.append(style(f"--- {len(diff.deletions)} deletion(s), "
                          f"{human_size(diff.deletions_size)}", fg="red", bold=True))
        for folder, size, src in diff.deletions:
            rows.append(style(f"  - {fit(folder, 44)} {human_size(size):>10}  ← "
                              f"{sync.drive_label(src, config)}", fg="red"))
    if diff.moves:
        rows.append(style(f"~~~ {len(diff.moves)} move(s), net-neutral", fg="yellow", bold=True))
        for folder, size, from_drive, to_drive in diff.moves:
            rows.append(style(f"  ~ {fit(folder, 44)} {sync.drive_label(from_drive, config)} → "
                              f"{sync.drive_label(to_drive, config)}", fg="yellow"))
    if diff.unchanged_count:
        rows.append(style(f"{diff.unchanged_count} album(s) unchanged", dim=True))
    return rows


def likes_rows(plan) -> list[str]:
    rows = [style("Sources", bold=True)]
    for name in ("device", "lastfm", "musicbee"):
        view = plan.views[name]
        mark = style("ok", fg="green") if view.available else style("--", dim=True)
        note = style(f"({view.note})", dim=True) if view.note else ""
        rows.append(f"  {mark} {fit(name, 10)} {view.count:>4} liked  {note}")
    rows.append("")
    rows.append(style(f"Merged list: {len(plan.desired)} liked track(s)", bold=True))
    for name in ("device", "lastfm", "musicbee"):
        sink = plan.sinks[name]
        if not sink.enabled:
            rows.append(style(f"  {fit(name, 10)} skipped — {sink.note}", dim=True))
            continue
        rows.append(f"  {fit(name, 10)} {style('+' + str(len(sink.add)), fg='green')}  "
                    f"{style('-' + str(len(sink.remove)), fg='red')}")
        for key in sink.add[:200]:
            rows.append(style(f"      + {plan.label(key)}", fg="green"))
        for key in sink.remove[:200]:
            rows.append(style(f"      - {plan.label(key)}", fg="red"))
    for root, paths in plan.playlist.items():
        rows.append(f"  playlist   {root} → {len(paths)} track(s)")
    if plan.conflicts:
        rows.append(style(f"  {len(plan.conflicts)} conflict(s) — liked in one place, "
                          f"unliked in another", fg="yellow"))
        for key in plan.conflicts[:50]:
            rows.append(style(f"      ! {plan.label(key)}", fg="yellow"))
    for note in plan.notes:
        rows.append(style(f"  {note}", dim=True))
    return rows


def main() -> int:
    app = App()
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if app.screen.active:
            app.screen.leave()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
