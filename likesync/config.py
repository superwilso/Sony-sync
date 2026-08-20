"""Configuration for the likes sync.

Deliberately separate from `sync.py`'s `AppConfig`: this component is standalone — it runs on its
own (`python -m likesync`), it can run when no music sync is planned, and it must not drag in the
copy planner. It reads the same `.env` for Last.fm credentials so there is one place to sign in,
and it keeps its own `likesync.json` for the settings only it has.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
SETTINGS_PATH = REPO_ROOT / "likesync.json"
STATE_DIR = REPO_ROOT / ".likesync"

# Written by Cinder on the device (player/cinder-ffi/src/lib.rs, liked_export_tsv).
DEVICE_LOVED_TSV = "cinder_loved.tsv"
# Read by Cinder on the next boot; this package writes it.
DEVICE_IMPORT_TSV = "cinder_liked_import.tsv"
# The playlist the device (and anything else that reads the drive) can actually play.
LIKED_PLAYLIST = "Liked Songs.m3u8"


def load_env(path: Path = ENV_PATH) -> None:
    """Same loader shape as sync.py — KEY="value" lines, existing environment wins."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def save_env_value(key: str, value: str, path: Path = ENV_PATH) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    written = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            out.append(f'{key}="{value}"')
            written = True
        else:
            out.append(line)
    if not written:
        out.append(f'{key}="{value}"')
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value


# ───────────────────────────────────────────────────────── drive detection

def _iter_windows_drives() -> list[str]:
    import ctypes
    drives: list[str] = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()   # type: ignore[attr-defined]
    for index in range(26):
        if mask & (1 << index):
            drives.append(f"{chr(65 + index)}:\\")
    return drives


def _volume_label(drive_root: str) -> str:
    import ctypes
    from ctypes import wintypes
    name = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    serial, max_comp, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(   # type: ignore[attr-defined]
        ctypes.c_wchar_p(drive_root), name, len(name), ctypes.byref(serial),
        ctypes.byref(max_comp), ctypes.byref(flags), filesystem, len(filesystem))
    return name.value.strip() if ok else ""


def detect_walkman_roots() -> tuple[str, str]:
    """(internal_root, sd_root) — the *drive* roots, not the Music folders.

    The liked list, the loved export and the import file all live at the drive root next to
    `.scrobbler.log`, because that is where Cinder writes them (`/contents/...` is the root of
    the volume Windows mounts). Overridable for testing outside Windows.
    """
    internal = os.environ.get("LIKESYNC_INTERNAL", "")
    sd = os.environ.get("LIKESYNC_SD", "")
    if internal or sd:
        return internal, sd
    if os.name != "nt":
        return "", ""

    internal, sd = "D:\\", "E:\\"
    candidates: list[tuple[str, str, int]] = []
    import shutil
    for drive in _iter_windows_drives():
        try:
            total = shutil.disk_usage(drive).total
        except OSError:
            continue
        candidates.append((drive, _volume_label(drive), total))

    walkman = next((d for d, label, _ in candidates if label.upper() == "WALKMAN"), None)
    if walkman:
        internal = walkman
    card = next((d for d, label, total in candidates
                 if d != walkman and (label.upper() == "32 GB" or 28 * 1024**3 <= total <= 36 * 1024**3)), None)
    if card:
        sd = card
    return internal, sd


# ───────────────────────────────────────────────────────── settings

@dataclass
class LikesConfig:
    source_dir: str = r"C:\Users\ABDPa\Music\my music lossless"
    playlist_dir: str = r"C:\Users\ABDPa\Music\Exported Playlists"
    internal_root: str = ""
    sd_root: str = ""
    # MusicBee hands its loves over as an exported playlist: point MusicBee at a playlist
    # (an auto-playlist on Love/Rating works well) and tick "auto-export as m3u".
    musicbee_loved_playlist: str = ""
    # Also treat a file tag rating of >= this many stars as a like (0 disables).
    # MusicBee only writes ratings into files when "save ratings to file tags" is on.
    musicbee_rating_threshold: float = 0.0
    # Write the merged list back out as a playlist MusicBee can show.
    musicbee_export_playlist: bool = True
    lastfm_enabled: bool = True
    # Conflict rule when a track was liked in one place and unliked in another since the last
    # run. "liked" keeps it (nothing is ever silently lost); "latest" trusts the newest change.
    conflict: str = "liked"
    # Emit the liked playlist onto each device drive, listing that drive's liked tracks.
    device_playlist: bool = True
    # Write cinder_liked_import.tsv so the on-device heart matches (needs Cinder >= the
    # liked-import build; harmless on older ones, which simply ignore the file).
    device_import: bool = True
    excluded_artists: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "LikesConfig":
        config = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key, value in data.items():
                if hasattr(config, key):
                    setattr(config, key, value)
        except (OSError, ValueError):
            pass
        # Path overrides, for running this on a machine that is not the Windows box (tests,
        # WSL) without editing the saved settings.
        for env_key, attribute in (("LIKESYNC_SOURCE", "source_dir"),
                                   ("LIKESYNC_PLAYLISTS", "playlist_dir")):
            override = os.environ.get(env_key, "")
            if override:
                setattr(config, attribute, override)
        if not config.internal_root or not config.sd_root:
            internal, sd = detect_walkman_roots()
            config.internal_root = config.internal_root or internal
            config.sd_root = config.sd_root or sd
        return config

    def save(self, path: Path = SETTINGS_PATH) -> None:
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @property
    def device_roots(self) -> list[str]:
        return [root for root in (self.internal_root, self.sd_root) if root and os.path.isdir(root)]

    @property
    def lastfm_ready(self) -> bool:
        return all(os.environ.get(key, "").strip() for key in
                   ("LASTFM_API_KEY", "LASTFM_API_SECRET", "LASTFM_SESSION_KEY"))
