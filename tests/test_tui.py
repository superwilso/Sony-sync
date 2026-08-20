"""TUI tests — every screen is rendered headlessly and checked for shape.

A terminal UI is usually untested because it needs a terminal. This one does not: `render_*`
returns a list of strings and `Screen` is the only thing that touches stdout, so a stub screen
with a fixed size is enough to prove that every screen builds, fits its width, and shows the
numbers the planner produced. That is what stops a formatting change from crashing the app in
front of the user.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_mod
import sync
import tui


class StubScreen:
    """Fixed-size screen that records frames instead of writing them."""

    def __init__(self, width: int = 100, height: int = 32) -> None:
        self._size = (width, height)
        self.frames: list[list[str]] = []
        self.active = False
        self.answers: list[str] = []

    @property
    def size(self):
        return self._size

    def draw(self, lines):
        self.frames.append(list(lines))

    def prompt(self, question: str) -> str:
        return self.answers.pop(0) if self.answers else ""

    def enter(self):
        self.active = True

    def leave(self):
        self.active = False


def build_fixture(tmp: str) -> sync.AppConfig:
    """A miniature library, playlist and pair of drives, so the real planner has real input."""
    source = os.path.join(tmp, "src")
    playlists = os.path.join(tmp, "playlists")
    internal = os.path.join(tmp, "internal", "Music")
    sd = os.path.join(tmp, "sd", "Music")
    for folder in (source, playlists, internal, sd):
        os.makedirs(folder, exist_ok=True)

    for album, tracks in (("Artist - One", 3), ("Other - Two", 2), ("高中正義 - Three", 1)):
        os.makedirs(os.path.join(source, album), exist_ok=True)
        for number in range(1, tracks + 1):
            name = f"{number:02d} - {album.split(' - ')[0]} - Song {number}.flac"
            Path(source, album, name).write_bytes(b"0" * 4096)

    Path(playlists, "Mix.m3u8").write_text(
        "..\\src\\Artist - One\\01 - Artist - Song 1.flac\n", encoding="utf-8")
    # One stale album already on the device, so removals are non-empty.
    os.makedirs(os.path.join(internal, "Gone - Album"), exist_ok=True)
    Path(internal, "Gone - Album", "01 - Gone - Old.flac").write_bytes(b"0" * 2048)

    return sync.AppConfig(source_dir=source, playlist_dir=playlists,
                          internal_drive=internal, sd_drive=sd,
                          internal_max_gb=1, sd_max_gb=1)


class TestPrimitives(unittest.TestCase):
    def test_cell_width_counts_wide_characters(self) -> None:
        self.assertEqual(tui.cell_width("高中"), 4)
        self.assertEqual(tui.cell_width("abc"), 3)

    def test_style_does_not_affect_width(self) -> None:
        self.assertEqual(tui.cell_width(tui.style("abc", fg="red", bold=True)), 3)

    def test_fit_is_exact(self) -> None:
        for text in ("short", "高中正義 - The Rainbow Goblins", "x" * 200):
            self.assertEqual(tui.cell_width(tui.fit(text, 20)), 20, text)

    def test_panel_lines_share_one_width(self) -> None:
        lines = tui.Panel("Title", ["a", "高中正義", "c"]).render(40)
        self.assertTrue(all(tui.cell_width(line) == 40 for line in lines))

    def test_bar_and_gauge(self) -> None:
        self.assertEqual(tui.bar(0.5, 10), "█████░░░░░")
        self.assertEqual(tui.cell_width(tui.gauge(1, 2, 10)), 10)


class TestListView(unittest.TestCase):
    def setUp(self) -> None:
        self.view = tui.ListView(items=[f"item {n}" for n in range(50)])

    def test_scroll_keeps_selection_visible(self) -> None:
        for _ in range(30):
            self.view.handle("down", 10)
        self.assertEqual(self.view.index, 30)
        self.assertLessEqual(self.view.offset, self.view.index)
        self.assertGreater(self.view.offset + 10, self.view.index)

    def test_filter_narrows_and_resets(self) -> None:
        self.view.handle("/", 10)
        for char in "item 4":
            self.view.handle(char, 10)
        self.assertEqual(len(self.view.visible_items), 11)     # 4, 40..49
        self.view.handle("esc", 10)
        self.assertEqual(len(self.view.visible_items), 50)

    def test_selected_maps_back_to_the_original_index(self) -> None:
        self.view.handle("/", 10)
        for char in "item 42":
            self.view.handle(char, 10)
        self.assertEqual(self.view.selected(), 42)

    def test_render_is_padded_to_height(self) -> None:
        lines = self.view.render(40, 12)
        self.assertEqual(len(lines), 12)


class TestScreens(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = build_fixture(self.tmp.name)
        self.app = app_mod.App()
        self.app.config = self.config
        self.app.screen = StubScreen()          # type: ignore[assignment]
        self.app.report = sync.build_scan_report(self.config)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dashboard_renders_and_fits(self) -> None:
        lines = app_mod.render_dashboard(self.app)
        width, height = self.app.screen.size
        self.assertLessEqual(len(lines), height)
        for line in lines:
            self.assertLessEqual(tui.cell_width(line), width, line)
        text = "\n".join(lines)
        self.assertIn("WALKMAN SYNC", text)
        self.assertIn("Storage", text)
        self.assertIn("Likes", text)

    def test_dashboard_fits_a_small_terminal(self) -> None:
        """60x20 is the floor `Screen.size` clamps to; nothing may spill past it."""
        self.app.screen = StubScreen(60, 20)     # type: ignore[assignment]
        lines = app_mod.render_dashboard(self.app)
        self.assertLessEqual(len(lines), 20)
        for line in lines:
            self.assertLessEqual(tui.cell_width(line), 60, line)

    def test_dashboard_fits_with_colour_on(self) -> None:
        """Styled frames must measure the same — width is counted in cells, not bytes."""
        original = tui.COLOUR
        tui.COLOUR = True
        try:
            lines = app_mod.render_dashboard(self.app)
            for line in lines:
                self.assertLessEqual(tui.cell_width(line), self.app.screen.size[0], repr(line))
            self.assertIn("\033[", "".join(lines))     # colour really was emitted
        finally:
            tui.COLOUR = original

    def test_dashboard_before_any_scan(self) -> None:
        self.app.report = None
        lines = app_mod.render_dashboard(self.app)
        self.assertIn("no scan yet", "\n".join(lines))

    def test_album_diff_rows_mention_every_change(self) -> None:
        rows = app_mod.album_diff_rows(self.config, self.app.report)
        text = "\n".join(rows)
        self.assertIn("Net change", text)
        self.assertIn("Artist - One", text)      # an addition
        self.assertIn("Gone - Album", text)      # a deletion

    def test_task_screen_shows_progress(self) -> None:
        progress = app_mod.TaskProgress()
        progress.step(3, 10, "copying something.flac", eta=42.0)
        lines = app_mod.render_task(self.app.screen, "Syncing", progress, frame=0)
        text = "\n".join(lines)
        self.assertIn("3/10", text)
        self.assertIn("00:42", text)
        self.assertIn("copying something.flac", text)

    def test_result_rows_include_the_activity_log(self) -> None:
        result = sync.sync_library(self.config, dry_run=True)
        rows = app_mod.result_rows(result)
        self.assertTrue(any("Files copied" in row for row in rows))
        self.assertTrue(len(rows) > 5)

    def test_run_task_returns_the_worker_result(self) -> None:
        value = app_mod.run_task(self.app.screen, "test", lambda progress: 42)
        self.assertEqual(value, 42)
        self.assertTrue(self.app.screen.frames)

    def test_run_task_propagates_errors(self) -> None:
        def boom(progress):
            raise OSError("disk went away")
        with self.assertRaises(OSError):
            app_mod.run_task(self.app.screen, "test", boom)


class TestLikesScreen(unittest.TestCase):
    def test_likes_rows_render(self) -> None:
        from likesync import engine
        from likesync.config import LikesConfig
        from likesync.library import Entry, LibraryIndex
        from likesync.state import State

        with tempfile.TemporaryDirectory() as tmp:
            device_root = os.path.join(tmp, "walkman")
            os.makedirs(os.path.join(device_root, "Music"))
            Path(device_root, "cinder_loved.tsv").write_text(
                "# artist\ttitle\nArtist\tSong\n", encoding="utf-8")
            config = LikesConfig(source_dir=os.path.join(tmp, "src"),
                                 playlist_dir=os.path.join(tmp, "pl"),
                                 internal_root=device_root, sd_root="",
                                 lastfm_enabled=False, musicbee_export_playlist=False)
            index = LibraryIndex("src")
            index.add(Entry(rel_path=os.path.join("Artist - Album", "01 - Artist - Song.flac"),
                            artist="Artist", title="Song", album="Album", track_no=1,
                            size=10, rating=0.0, duration=100))
            state = State(path=Path(tmp) / "state.json")
            views = engine.gather(config, index, None, state)
            plan = engine.build_plan(config, index, views, state)

        rows = app_mod.likes_rows(plan)
        text = "\n".join(rows)
        self.assertIn("Sources", text)
        self.assertIn("device", text)
        self.assertIn("Merged list: 1 liked track(s)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
