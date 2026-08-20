"""likesync — keep "liked" in step across the Walkman, Last.fm and MusicBee.

Standalone: `python -m likesync status` / `sync`. Nothing here imports `sync.py`, so the music
copy and the likes sync can be run and reasoned about separately; the TUI just calls into it.
"""
from .config import LikesConfig, load_env            # noqa: F401
from .engine import Plan, apply_plan, build_plan, gather   # noqa: F401
from .session import Session                          # noqa: F401

__version__ = "1.0.0"
