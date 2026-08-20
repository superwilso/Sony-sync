"""likesync tests — `python -m unittest discover tests` (stdlib only, no pytest needed).

The merge rules are the part worth testing: they decide whether a hand-curated list of likes
survives a round trip, and the failure mode of getting them wrong is silent deletion.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from likesync import device as device_mod
from likesync import engine, musicbee
from likesync.config import LikesConfig
from likesync.keys import artist_candidates, loose_key, norm_artist, norm_title, track_key
from likesync.library import Entry, LibraryIndex
from likesync.state import State
from likesync.tags import _from_filename, _parse_rating, _popm_to_stars, _read_id3


class TestKeys(unittest.TestCase):
    def test_remaster_suffixes_collapse(self) -> None:
        base = track_key("The Beatles", "Don't Let Me Down")
        for variant in ("Don't Let Me Down - Remastered 2009",
                        "Don’t Let Me Down (2021 Mix)",
                        "Don't Let Me Down (Remastered)",
                        "Don't Let Me Down - 2009 Remaster"):
            self.assertEqual(track_key("The Beatles", variant), base, variant)

    def test_different_recordings_stay_distinct(self) -> None:
        """The whole risk of normalising is merging two real songs. These must not collapse."""
        base = track_key("Bob Marley", "No Woman No Cry")
        for variant in ("No Woman No Cry (Live)", "No Woman No Cry - Club Mix",
                        "No Woman No Cry (Acoustic)", "No Woman No Cry (Demo)"):
            self.assertNotEqual(track_key("Bob Marley", variant), base, variant)

    def test_feat_is_stripped_from_both_sides(self) -> None:
        self.assertEqual(track_key("Little Simz feat. Cleo Sol", "Woman (feat. Cleo Sol)"),
                         track_key("Little Simz", "Woman"))

    def test_artist_candidates(self) -> None:
        names = artist_candidates("M-Beat; Jamiroquai")
        self.assertIn("m-beat", names)
        self.assertIn("jamiroquai", names)

    def test_case_and_punctuation(self) -> None:
        self.assertEqual(norm_artist("AIR"), norm_artist("Air"))
        self.assertEqual(norm_title("La Femme d’Argent"), norm_title("La Femme d'argent"))
        self.assertEqual(loose_key("America, George Martin", "Ventura Highway"),
                         loose_key("America", "Ventura Highway"))


class TestTags(unittest.TestCase):
    def test_filename_fallback(self) -> None:
        # Built with os.path.join: the fallback splits with the host's own separator, and the
        # library it actually runs against is always on the same host as the walk that found it.
        path = os.path.join("lib", "Artist - Album", "03 - Artist - The Song.flac")
        tags = _from_filename(path)
        self.assertEqual((tags.artist, tags.title, tags.track_no, tags.album),
                         ("Artist", "The Song", 3, "Album"))

    def test_rating_scales(self) -> None:
        self.assertEqual(_parse_rating("0.8"), 4.0)     # FMPS_RATING, 0..1
        self.assertEqual(_parse_rating("4"), 4.0)       # RATING, stars
        self.assertEqual(_parse_rating("80"), 4.0)      # RATING, 0..100
        self.assertEqual(_popm_to_stars(255), 5.0)
        self.assertEqual(_popm_to_stars(0), 0.0)

    def test_id3v23_roundtrip(self) -> None:
        def frame(name: bytes, text: str) -> bytes:
            payload = b"\x00" + text.encode("latin-1")
            return name + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload

        body = frame(b"TPE1", "Portishead") + frame(b"TIT2", "Roads") + frame(b"TRCK", "5/11")
        size = bytes([(len(body) >> shift) & 0x7F for shift in (21, 14, 7, 0)])
        blob = b"ID3\x03\x00\x00" + size + body
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "x.mp3")
            Path(path).write_bytes(blob)
            tags = _read_id3(path)
        assert tags is not None
        self.assertEqual((tags.artist, tags.title, tags.track_no), ("Portishead", "Roads", 5))


def make_index(rows: list[tuple[str, str, str]]) -> LibraryIndex:
    """rows = (folder, artist, title) → an index with plausible relative paths."""
    index = LibraryIndex("SRC")
    for position, (folder, artist, title) in enumerate(rows, start=1):
        index.add(Entry(rel_path=os.path.join(folder, f"{position:02d} - {artist} - {title}.flac"),
                        artist=artist, title=title, album=folder, track_no=position,
                        size=1000, rating=0.0, duration=200))
    return index


class TestLibraryIndex(unittest.TestCase):
    def test_folder_artist_match(self) -> None:
        index = make_index([("Little Simz - SIMBI", "Cleo Sol", "Woman")])
        found = index.resolve("Little Simz feat. Cleo Sol", "Woman")
        self.assertIsNotNone(found)
        self.assertEqual(found.artist, "Cleo Sol")   # type: ignore[union-attr]

    def test_no_match_is_none(self) -> None:
        index = make_index([("A - B", "A", "Song")])
        self.assertIsNone(index.resolve("Someone Else", "Different Song"))


class EngineCase(unittest.TestCase):
    """Engine tests share a scratch device volume and an in-memory library."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.device_root = os.path.join(self.root, "walkman")
        os.makedirs(os.path.join(self.device_root, "Music"), exist_ok=True)
        self.config = LikesConfig(
            source_dir=os.path.join(self.root, "src"),
            playlist_dir=os.path.join(self.root, "playlists"),
            internal_root=self.device_root, sd_root="",
            lastfm_enabled=False, musicbee_export_playlist=False,
        )
        self.index = make_index([("Artist - Album", "Artist", "One"),
                                 ("Artist - Album", "Artist", "Two"),
                                 ("Other - Album", "Other", "Three")])
        self.state = State(path=Path(self.root) / "state.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_device(self, tracks: list[tuple[str, str]]) -> None:
        body = "# artist\ttitle\n" + "".join(f"{a}\t{t}\n" for a, t in tracks)
        Path(self.device_root, "cinder_loved.tsv").write_text(body, encoding="utf-8")

    def views(self, lastfm: dict | None = None, musicbee_tracks: dict | None = None):
        views = engine.gather(self.config, self.index, None, self.state)
        if lastfm is not None:
            views["lastfm"].available = True
            views["lastfm"].tracks = lastfm
        if musicbee_tracks is not None:
            views["musicbee"].available = True
            views["musicbee"].tracks = musicbee_tracks
        return views


class TestMergeRules(EngineCase):
    def test_first_run_is_a_union_and_never_removes(self) -> None:
        self.write_device([("Artist", "One")])
        views = self.views(lastfm={track_key("Artist", "Two"): {"artist": "Artist", "title": "Two"}})
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertEqual(len(plan.desired), 2)
        self.assertEqual(plan.sinks["device"].remove, [])
        self.assertEqual(plan.sinks["lastfm"].remove, [])

    def test_removal_propagates_only_against_a_snapshot(self) -> None:
        one = track_key("Artist", "One")
        two = track_key("Artist", "Two")
        self.state.liked = {one: {"artist": "Artist", "title": "One"},
                            two: {"artist": "Artist", "title": "Two"}}
        self.state.set_snapshot("device", dict(self.state.liked))
        self.state.set_snapshot("lastfm", dict(self.state.liked))

        self.write_device([("Artist", "One")])            # "Two" unliked on the device
        views = self.views(lastfm=dict(self.state.liked))
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertNotIn(two, plan.desired)
        self.assertEqual(plan.sinks["lastfm"].remove, [two])

    def test_source_absent_this_run_causes_no_removals(self) -> None:
        one = track_key("Artist", "One")
        self.state.liked = {one: {"artist": "Artist", "title": "One"}}
        self.state.set_snapshot("device", dict(self.state.liked))
        self.config.internal_root = os.path.join(self.root, "not-plugged-in")
        views = engine.gather(self.config, self.index, None, self.state)
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertIn(one, plan.desired)                  # the unplugged device proves nothing

    def test_pending_import_makes_the_device_additive_only(self) -> None:
        one = track_key("Artist", "One")
        two = track_key("Artist", "Two")
        self.state.liked = {one: {"artist": "Artist", "title": "One"},
                            two: {"artist": "Artist", "title": "Two"}}
        self.state.set_snapshot("device", dict(self.state.liked))
        # The device's export still shows the pre-push list while the import waits to be merged.
        self.write_device([("Artist", "One")])
        Path(self.device_root, "cinder_liked_import.tsv").write_text("x", encoding="utf-8")
        views = engine.gather(self.config, self.index, None, self.state)
        self.assertTrue(views["device"].additive_only)
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertIn(two, plan.desired)

    def test_conflict_liked_wins(self) -> None:
        one = track_key("Artist", "One")
        self.state.liked = {one: {"artist": "Artist", "title": "One"}}
        self.state.set_snapshot("device", dict(self.state.liked))
        self.state.set_snapshot("lastfm", {})
        self.write_device([])                              # unliked on the device
        views = self.views(lastfm={one: {"artist": "Artist", "title": "One"}})  # loved on Last.fm
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertIn(one, plan.conflicts)
        self.assertIn(one, plan.desired)

    def test_conflict_latest_lets_the_removal_win(self) -> None:
        one = track_key("Artist", "One")
        self.config.conflict = "latest"
        self.state.liked = {one: {"artist": "Artist", "title": "One"}}
        self.state.set_snapshot("device", dict(self.state.liked))
        self.state.set_snapshot("lastfm", {})
        self.write_device([])
        views = self.views(lastfm={one: {"artist": "Artist", "title": "One"}})
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertNotIn(one, plan.desired)


class TestDeviceOutputs(EngineCase):
    def test_playlist_only_lists_files_on_that_drive(self) -> None:
        music = os.path.join(self.device_root, "Music")
        present = os.path.join("Artist - Album", "01 - Artist - One.flac")
        os.makedirs(os.path.join(music, "Artist - Album"), exist_ok=True)
        Path(music, present).write_bytes(b"0")
        self.write_device([("Artist", "One"), ("Other", "Three")])
        views = engine.gather(self.config, self.index, None, self.state)
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertEqual(plan.playlist[self.device_root], [present])
        self.assertEqual(len(plan.off_device), 1)         # "Three" is liked but not copied over

        engine.apply_plan(self.config, plan, self.state, self.index, None, dry_run=False)
        written = Path(music, "Liked Songs.m3u8").read_text(encoding="utf-8")
        self.assertIn("Artist - Album/01 - Artist - One.flac", written)
        self.assertNotIn("\\", written.split("\n", 1)[1])  # forward slashes for the indexer

    def test_import_file_holds_the_whole_list_not_the_delta(self) -> None:
        """Cinder replaces its liked set with this file, so it has to be the complete list."""
        one, two = track_key("Artist", "One"), track_key("Artist", "Two")
        self.state.liked = {one: {"artist": "Artist", "title": "One"},
                            two: {"artist": "Artist", "title": "Two"}}
        self.state.set_snapshot("device", {one: self.state.liked[one]})
        self.write_device([("Artist", "One")])          # the device is one track behind

        views = engine.gather(self.config, self.index, None, self.state)
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertEqual(plan.sinks["device"].add, [two])
        engine.apply_plan(self.config, plan, self.state, self.index, None, dry_run=False)
        body = Path(self.device_root, "cinder_liked_import.tsv").read_text(encoding="utf-8")
        rows = sorted(line for line in body.splitlines() if not line.startswith("#"))
        self.assertEqual(rows, ["Artist\tOne", "Artist\tTwo"])

    def test_a_redundant_pending_import_is_cleared(self) -> None:
        """Otherwise the device stays additive-only for ever and can never express an unlike."""
        self.write_device([("Artist", "One")])
        Path(self.device_root, "cinder_liked_import.tsv").write_text(
            "# artist\ttitle\nArtist\tOne\n", encoding="utf-8")
        self.state.liked = {track_key("Artist", "One"): {"artist": "Artist", "title": "One"}}
        self.state.set_snapshot("device", dict(self.state.liked))

        views = engine.gather(self.config, self.index, None, self.state)
        plan = engine.build_plan(self.config, self.index, views, self.state)
        self.assertEqual(plan.sinks["device"].add, [])
        engine.apply_plan(self.config, plan, self.state, self.index, None, dry_run=False)
        self.assertFalse(Path(self.device_root, "cinder_liked_import.tsv").exists())

    def test_import_is_rewritten_when_the_device_is_behind(self) -> None:
        self.write_device([])
        self.state.liked = {track_key("Artist", "One"): {"artist": "Artist", "title": "One"}}
        self.state.set_snapshot("device", {})
        views = engine.gather(self.config, self.index, None, self.state)
        plan = engine.build_plan(self.config, self.index, views, self.state)
        engine.apply_plan(self.config, plan, self.state, self.index, None, dry_run=False)
        body = Path(self.device_root, "cinder_liked_import.tsv").read_text(encoding="utf-8")
        self.assertIn("Artist\tOne", body)

    def test_dry_run_writes_nothing(self) -> None:
        self.write_device([("Artist", "One")])
        views = engine.gather(self.config, self.index, None, self.state)
        plan = engine.build_plan(self.config, self.index, views, self.state)
        engine.apply_plan(self.config, plan, self.state, self.index, None, dry_run=True)
        self.assertFalse(Path(self.device_root, "cinder_liked_import.tsv").exists())
        self.assertFalse((Path(self.root) / "state.json").exists())

    def test_state_round_trip(self) -> None:
        self.write_device([("Artist", "One")])
        views = engine.gather(self.config, self.index, None, self.state)
        plan = engine.build_plan(self.config, self.index, views, self.state)
        engine.apply_plan(self.config, plan, self.state, self.index, None, dry_run=False)
        reloaded = State.load(self.state.path)
        self.assertEqual(set(reloaded.liked), set(plan.desired))
        self.assertFalse(reloaded.first_run)


class TestMusicBee(EngineCase):
    def test_playlist_round_trip(self) -> None:
        playlist_dir = os.path.join(self.root, "playlists")
        os.makedirs(playlist_dir, exist_ok=True)
        source_dir = os.path.join(self.root, "src")
        entries = [self.index.entries[0], self.index.entries[2]]
        target = os.path.join(playlist_dir, "Liked Songs.m3u8")
        musicbee.write_playlist(target, entries, source_dir)

        keys = musicbee.read_loved_playlist(target, source_dir, self.index)
        self.assertEqual(set(keys), {track_key("Artist", "One"), track_key("Other", "Three")})

    def test_find_loved_playlist(self) -> None:
        playlist_dir = os.path.join(self.root, "playlists")
        os.makedirs(playlist_dir, exist_ok=True)
        Path(playlist_dir, "Loved.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
        self.assertTrue(musicbee.find_loved_playlist(playlist_dir).endswith("Loved.m3u8"))


class TestDeviceFiles(unittest.TestCase):
    def test_loved_tsv_parsing_tolerates_junk(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "cinder_loved.tsv").write_text(
                "# comment\n\nArtist\tTitle\nbroken-line\nB\tT\textra\n", encoding="utf-8")
            volume = device_mod.DeviceVolume(root=folder)
            loved = device_mod.read_loved(volume)
        self.assertEqual(set(loved), {track_key("Artist", "Title"), track_key("B", "T")})


if __name__ == "__main__":
    unittest.main(verbosity=2)
