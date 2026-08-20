"""Sync state — the thing that makes this a *sync* and not a merge.

Without a record of what each side looked like last time, a track that is present in Last.fm and
absent on the device is ambiguous: it was either just loved on Last.fm (→ push it to the device)
or just unliked on the device (→ unlove it on Last.fm). Comparing each side against its own
previous snapshot resolves that, which is why every source's snapshot is stored separately.

The file is JSON and human-readable on purpose: it is the only record of a hand-curated list, and
a person must be able to look at it — and fix it — without this program.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE_VERSION = 2


@dataclass
class Snapshot:
    """What one source looked like at the end of the last successful run."""
    keys: dict[str, dict] = field(default_factory=dict)   # track_key -> {"artist","title"}
    taken: float = 0.0

    def to_json(self) -> dict:
        return {"taken": self.taken, "keys": self.keys}

    @classmethod
    def from_json(cls, data: dict | None) -> "Snapshot":
        if not isinstance(data, dict):
            return cls()
        keys = data.get("keys")
        return cls(keys=keys if isinstance(keys, dict) else {}, taken=float(data.get("taken", 0.0)))


@dataclass
class State:
    path: Path
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    liked: dict[str, dict] = field(default_factory=dict)   # the merged truth after the last run
    last_sync: float = 0.0

    @classmethod
    def load(cls, path: str | Path) -> "State":
        state = cls(path=Path(path))
        try:
            data = json.loads(state.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return state
        if not isinstance(data, dict) or data.get("version", 0) > STATE_VERSION:
            return state
        state.snapshots = {name: Snapshot.from_json(value)
                           for name, value in (data.get("snapshots") or {}).items()}
        liked = data.get("liked")
        state.liked = liked if isinstance(liked, dict) else {}
        state.last_sync = float(data.get("last_sync", 0.0))
        return state

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "last_sync": self.last_sync,
            "liked": self.liked,
            "snapshots": {name: snap.to_json() for name, snap in self.snapshots.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)   # atomic: a power cut can't leave a half-written list

    def snapshot(self, name: str) -> Snapshot:
        return self.snapshots.get(name, Snapshot())

    def set_snapshot(self, name: str, keys: dict[str, dict]) -> None:
        self.snapshots[name] = Snapshot(keys=dict(keys), taken=time.time())

    @property
    def first_run(self) -> bool:
        """No snapshot yet → every source is additive; nothing is ever treated as an unlike."""
        return not self.snapshots
