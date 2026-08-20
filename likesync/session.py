"""One object that wires the pieces together, so both the CLI and the TUI drive the same code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from . import engine
from .config import LikesConfig, STATE_DIR, load_env
from .lastfm import Lastfm, from_env
from .library import LibraryIndex
from .state import State
from .tags import TagCache

StatusFn = Callable[[str], None]


@dataclass
class Session:
    config: LikesConfig = field(default_factory=LikesConfig.load)
    state: State = field(default=None)          # type: ignore[assignment]
    cache: TagCache = field(default=None)       # type: ignore[assignment]
    index: LibraryIndex | None = None
    lastfm: Lastfm | None = None
    views: dict[str, engine.SourceView] = field(default_factory=dict)
    plan: engine.Plan | None = None

    def __post_init__(self) -> None:
        load_env()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if self.state is None:
            self.state = State.load(STATE_DIR / "state.json")
        if self.cache is None:
            self.cache = TagCache.load(STATE_DIR / "tagcache.json")
        if self.lastfm is None:
            self.lastfm = from_env()

    # ── phases ───────────────────────────────────────────────────
    def build_index(self, status: StatusFn | None = None,
                    progress: Callable[[int, int, str], None] | None = None) -> LibraryIndex:
        if status:
            status(f"Indexing {self.config.source_dir}")
        self.index = LibraryIndex.build(self.config.source_dir, self.cache, progress)
        self.cache.save()
        if status:
            status(f"Indexed {len(self.index)} file(s) "
                   f"({self.cache.hits} cached, {self.cache.misses} read)")
        return self.index

    def refresh(self, status: StatusFn | None = None,
                progress: Callable[[int, int, str], None] | None = None) -> engine.Plan:
        if self.index is None:
            self.build_index(status, progress)
        assert self.index is not None
        if status:
            status("Reading likes from device, Last.fm and MusicBee")
        self.views = engine.gather(
            self.config, self.index, self.lastfm, self.state,
            progress=(lambda name, done, total: status(f"{name}: {done} liked")) if status else None,
        )
        self.plan = engine.build_plan(self.config, self.index, self.views, self.state)
        return self.plan

    def apply(self, dry_run: bool = True, status: StatusFn | None = None) -> engine.ApplyResult:
        if self.plan is None:
            self.refresh(status)
        assert self.plan is not None
        return engine.apply_plan(
            self.config, self.plan, self.state, self.index, self.lastfm, dry_run=dry_run,
            progress=(lambda message, done, total: status(f"[{done}/{total}] {message}"))
            if status else None,
        )

    # ── small helpers the front-ends both want ───────────────────
    @property
    def device_connected(self) -> bool:
        return bool(self.config.device_roots)

    @property
    def lastfm_user(self) -> str:
        return self.lastfm.username if self.lastfm and self.lastfm.username else ""

    def sign_in(self, username: str, password: str) -> str:
        if self.lastfm is None:
            raise RuntimeError("Last.fm API key and secret are not set in .env")
        name, key = self.lastfm.authenticate(username, password)
        from .config import save_env_value
        save_env_value("LASTFM_USERNAME", name)
        save_env_value("LASTFM_SESSION_KEY", key)
        os.environ["LASTFM_SESSION_KEY"] = key
        return name
