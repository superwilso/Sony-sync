"""Standalone CLI: `python -m likesync <command>`.

    status                 what each side holds and what a sync would change   (read-only)
    plan                   the same, listing every track that would move       (read-only)
    sync [-y] [-n]         apply it (-n = dry run, -y = no confirmation)
    playlist               rewrite the liked playlists only, change nothing else
    login                  sign in to Last.fm and store the session key in .env
    config [key=value ...] show or edit likesync.json

Exit status is 0 on success, 1 on a handled error, 2 on bad usage — so it can be scripted.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

if __package__ in (None, ""):        # allow `python likesync/__main__.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from likesync.config import LikesConfig
from likesync.lastfm import LastfmError
from likesync.session import Session

GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m")


def _enable_colour() -> None:
    import os
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        globals().update(GREEN="", RED="", YELLOW="", CYAN="", DIM="", BOLD="", RESET="")


def status_line(message: str) -> None:
    print(f"{DIM}  {message}{RESET}")


def print_sources(session: Session, plan) -> None:
    print(f"\n{BOLD}Sources{RESET}")
    for name in ("device", "lastfm", "musicbee"):
        view = plan.views[name]
        mark = f"{GREEN}ok {RESET}" if view.available else f"{DIM}-- {RESET}"
        extra = f" {DIM}({view.note}){RESET}" if view.note else ""
        print(f"  {mark} {name:<9} {view.count:>4} liked{extra}")


def print_plan(plan, verbose: bool) -> None:
    print(f"\n{BOLD}Merged list{RESET}: {len(plan.desired)} liked track(s)")
    for name in ("device", "lastfm", "musicbee"):
        sink = plan.sinks[name]
        if not sink.enabled:
            print(f"  {DIM}{name:<9} skipped — {sink.note}{RESET}")
            continue
        added = f"{GREEN}+{len(sink.add)}{RESET}" if sink.add else "+0"
        removed = f"{RED}-{len(sink.remove)}{RESET}" if sink.remove else "-0"
        print(f"  {name:<9} {added}  {removed}")
        if verbose:
            for key in sink.add:
                print(f"      {GREEN}+{RESET} {plan.label(key)}")
            for key in sink.remove:
                print(f"      {RED}-{RESET} {plan.label(key)}")

    for root, paths in plan.playlist.items():
        print(f"  playlist  {root} → {len(paths)} track(s)")
    if plan.conflicts:
        print(f"  {YELLOW}{len(plan.conflicts)} conflict(s){RESET} "
              f"(liked in one place, unliked in another)")
        if verbose:
            for key in plan.conflicts:
                print(f"      {YELLOW}!{RESET} {plan.label(key)}")
    for note in plan.notes:
        print(f"  {DIM}{note}{RESET}")


def command_status(session: Session, args: list[str]) -> int:
    plan = session.refresh(status_line)
    print_sources(session, plan)
    print_plan(plan, verbose="-v" in args or "plan" in args)
    if plan.total_changes == 0:
        print(f"\n{GREEN}Everything is in sync.{RESET}")
    else:
        print(f"\n{plan.total_changes} change(s) pending — run `python -m likesync sync`.")
    return 0


def command_sync(session: Session, args: list[str]) -> int:
    dry_run = "-n" in args or "--dry-run" in args
    assume_yes = "-y" in args
    plan = session.refresh(status_line)
    print_sources(session, plan)
    print_plan(plan, verbose="-v" in args)

    if plan.total_changes == 0 and not plan.playlist:
        print(f"\n{GREEN}Nothing to do.{RESET}")
        return 0
    if not dry_run and not assume_yes:
        answer = input(f"\nApply this? [y/N]: ").strip().lower()
        if answer != "y":
            print("Cancelled.")
            return 0

    result = session.apply(dry_run=dry_run, status=status_line)
    print()
    for line in result.logs:
        print(f"  {line}")
    if result.lastfm_failed:
        print(f"  {RED}{len(result.lastfm_failed)} Last.fm call(s) failed:{RESET}")
        for key, reason in result.lastfm_failed[:10]:
            print(f"    {plan.label(key)}: {reason}")
    print(f"\n{'Dry run — nothing was written.' if dry_run else GREEN + 'Done.' + RESET}")
    return 0


def command_playlist(session: Session, args: list[str]) -> int:
    """Rewrite the playlists from the state we already have — no network, no state change."""
    session.config.device_import = False
    session.config.lastfm_enabled = False
    plan = session.refresh(status_line)
    result = session.apply(dry_run="-n" in args, status=status_line)
    for line in result.logs:
        print(f"  {line}")
    return 0


def command_login(session: Session, args: list[str]) -> int:
    if session.lastfm is None:
        print(f"{RED}Set LASTFM_API_KEY and LASTFM_API_SECRET in .env first.{RESET}")
        return 1
    username = input(f"Last.fm username [{session.lastfm.username or 'none'}]: ").strip() \
        or session.lastfm.username
    password = getpass.getpass("Last.fm password: ")
    try:
        name = session.sign_in(username, password)
    except (LastfmError, RuntimeError) as exc:
        print(f"{RED}{exc}{RESET}")
        return 1
    print(f"{GREEN}Signed in as {name}; session key saved to .env{RESET}")
    return 0


def command_config(session: Session, args: list[str]) -> int:
    config = session.config
    assignments = [arg for arg in args if "=" in arg]
    for assignment in assignments:
        key, _, value = assignment.partition("=")
        key = key.strip()
        if not hasattr(config, key):
            print(f"{RED}unknown setting: {key}{RESET}")
            return 2
        current = getattr(config, key)
        if isinstance(current, bool):
            setattr(config, key, value.strip().lower() in ("1", "true", "yes", "on"))
        elif isinstance(current, float):
            setattr(config, key, float(value))
        elif isinstance(current, list):
            setattr(config, key, [part.strip() for part in value.split(",") if part.strip()])
        else:
            setattr(config, key, value.strip())
    if assignments:
        config.save()
        print(f"{GREEN}Saved likesync.json{RESET}")
    for key, value in vars(config).items():
        print(f"  {key:<28} {value}")
    return 0


COMMANDS = {
    "status": command_status,
    "plan": command_status,
    "sync": command_sync,
    "playlist": command_playlist,
    "login": command_login,
    "config": command_config,
}


def main(argv: list[str]) -> int:
    _enable_colour()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    command = COMMANDS.get(argv[0])
    if command is None:
        print(f"{RED}unknown command: {argv[0]}{RESET}\n")
        print(__doc__)
        return 2

    session = Session(config=LikesConfig.load())
    if "--no-lastfm" in argv:
        session.config.lastfm_enabled = False
    for argument in argv:
        if argument.startswith("--conflict="):
            session.config.conflict = argument.split("=", 1)[1]
    try:
        return command(session, argv[1:])
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1
    except (OSError, LastfmError, RuntimeError) as exc:
        print(f"{RED}error: {exc}{RESET}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
