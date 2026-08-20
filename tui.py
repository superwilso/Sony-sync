"""Terminal UI primitives — stdlib only.

`sync.py` runs on whatever Python is installed next to MusicBee on the Windows box, with no venv
and no pip step, so this is written against nothing but the standard library. It is not a widget
framework: it is the four things a full-screen console app actually needs.

1. **One write per frame.** Every screen is built as a list of strings and written in a single
   `sys.stdout.write` with the cursor parked at home and each line cleared to its end. The old
   front-end called `cls` between screens, which is what made it flicker and lose scrollback.
2. **Single keypresses.** `msvcrt.getwch` on Windows, `termios` raw mode elsewhere, both mapped
   onto the same small vocabulary ("up", "enter", "q", …), so the screens never care which.
3. **Width-correct truncation.** Album and artist names in this library include CJK; padding to a
   character count would leave the columns ragged, so everything measures display cells.
4. **Rendering separated from output.** Every `render_*` returns lines. That is what makes the
   screens testable without a terminal — see `tests/test_tui.py`.
"""
from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────── colour

def _enable_vt() -> bool:
    """Windows 10+ consoles need VT processing switched on before ANSI means anything."""
    if os.name != "nt":
        return sys.stdout.isatty()
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


COLOUR = _enable_vt()

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
REVERSE = "\033[7m"

FG = {
    "default": "\033[39m", "black": "\033[30m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "white": "\033[37m", "grey": "\033[90m", "bright_red": "\033[91m",
    "bright_green": "\033[92m", "bright_yellow": "\033[93m", "bright_cyan": "\033[96m",
    "amber": "\033[38;5;214m",     # Cinder's accent, and the device's own palette
}
BG = {"default": "\033[49m", "selected": "\033[48;5;238m", "bar": "\033[48;5;236m"}


def style(text: str, fg: str | None = None, bg: str | None = None,
          bold: bool = False, dim: bool = False, reverse: bool = False) -> str:
    if not COLOUR:
        return text
    prefix = ""
    if fg:
        prefix += FG.get(fg, "")
    if bg:
        prefix += BG.get(bg, "")
    if bold:
        prefix += BOLD
    if dim:
        prefix += DIM
    if reverse:
        prefix += REVERSE
    return f"{prefix}{text}{RESET}" if prefix else text


# ─────────────────────────────────────────────────────────── width

def cell_width(text: str) -> int:
    """Display width, counting CJK/emoji as two columns and dropping ANSI runs."""
    width = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\033":                       # skip an escape sequence
            end = text.find("m", index)
            index = len(text) if end == -1 else end + 1
            continue
        if unicodedata.combining(char):
            index += 1
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        index += 1
    return width


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """Cut to `width` display cells. Assumes `text` carries no ANSI (style after truncating)."""
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    budget = width - cell_width(ellipsis)
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + step > budget:
            break
        out.append(char)
        used += step
    return "".join(out) + ellipsis


def pad(text: str, width: int, align: str = "left") -> str:
    filler = max(width - cell_width(text), 0)
    if align == "right":
        return " " * filler + text
    if align == "center":
        left = filler // 2
        return " " * left + text + " " * (filler - left)
    return text + " " * filler


def fit(text: str, width: int, align: str = "left") -> str:
    return pad(truncate(text, width), width, align)


# ─────────────────────────────────────────────────────────── chrome

def bar(fraction: float, width: int, filled: str = "█", empty: str = "░") -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    full = int(round(fraction * width))
    return filled * full + empty * (width - full)


def gauge(used: int, total: int, width: int, warn_at: float = 0.9) -> str:
    """A capacity bar that colours itself: green, amber past 75%, red past `warn_at`."""
    fraction = (used / total) if total else 0.0
    colour = "green" if fraction < 0.75 else ("amber" if fraction < warn_at else "red")
    return style(bar(fraction, width), fg=colour)


def human_size(size_bytes: float) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def rule(width: int, char: str = "─") -> str:
    return char * max(width, 0)


@dataclass
class Panel:
    """A titled box. Rows are pre-styled strings; the box measures them by display width."""
    title: str
    rows: list[str] = field(default_factory=list)
    accent: str = "cyan"
    footer: str = ""

    def render(self, width: int) -> list[str]:
        inner = max(width - 2, 1)
        head = f"┌─ {truncate(self.title, inner - 3)} "
        head = head + "─" * max(width - cell_width(head) - 1, 0) + "┐"
        lines = [style(head, fg=self.accent)]
        for row in self.rows:
            lines.append(style("│", fg=self.accent) + fit(row, inner) + style("│", fg=self.accent))
        if self.footer:
            lines.append(style("│", fg=self.accent) + fit(style(self.footer, dim=True), inner)
                         + style("│", fg=self.accent))
        lines.append(style("└" + "─" * inner + "┘", fg=self.accent))
        return lines


def side_by_side(left: list[str], right: list[str], left_width: int, gap: int = 1) -> list[str]:
    """Join two rendered blocks column-wise, padding the shorter one."""
    height = max(len(left), len(right))
    out = []
    for index in range(height):
        left_line = left[index] if index < len(left) else ""
        right_line = right[index] if index < len(right) else ""
        out.append(pad(left_line, left_width) + " " * gap + right_line)
    return out


def key_hint(pairs: list[tuple[str, str]], width: int) -> str:
    parts = [f"{style(key, fg='amber', bold=True)} {label}" for key, label in pairs]
    return fit("  ".join(parts), width)


# ─────────────────────────────────────────────────────────── input

_WIN_KEYS = {
    "H": "up", "P": "down", "K": "left", "M": "right",
    "G": "home", "O": "end", "I": "pgup", "Q": "pgdn", "S": "delete",
}
_ANSI_KEYS = {
    "A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
    "5~": "pgup", "6~": "pgdn", "3~": "delete",
}


def decode_ansi(sequence: str) -> str:
    """Map the tail of a CSI sequence ("[B", "[5~", "[1;5D") onto a key name, or "" if unknown."""
    if not sequence.startswith("["):
        return ""
    body = sequence[1:]
    if body in _ANSI_KEYS:
        return _ANSI_KEYS[body]
    # Modified keys arrive as "1;5C" (ctrl+right) and friends; the final letter still identifies it.
    if body and body[-1] in _ANSI_KEYS:
        return _ANSI_KEYS[body[-1]]
    return ""


def read_key() -> str:
    """One keypress, normalised. Returns 'up'/'down'/'left'/'right'/'enter'/'esc'/'tab'/
    'backspace'/'home'/'end'/'pgup'/'pgdn'/'delete', or the literal character."""
    if os.name == "nt":
        import msvcrt
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            return _WIN_KEYS.get(msvcrt.getwch(), "")
        if char == "\r":
            return "enter"
        if char == "\x1b":
            return "esc"
        if char == "\t":
            return "tab"
        if char in ("\x08", "\x7f"):
            return "backspace"
        if char == "\x03":
            raise KeyboardInterrupt
        return char

    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # os.read, NOT sys.stdin.read: the buffered reader swallows the rest of an escape
        # sequence into its own buffer, select() then sees an empty fd, and every arrow key
        # reads as a bare Esc — which quits the screen instead of moving the cursor.
        first = os.read(fd, 1).decode("utf-8", "replace")
        if first == "\x1b":
            if not select.select([fd], [], [], 0.05)[0]:
                return "esc"
            rest = os.read(fd, 8).decode("utf-8", "replace")
            return decode_ansi(rest)
        if first in ("\r", "\n"):
            return "enter"
        if first == "\t":
            return "tab"
        if first in ("\x7f", "\x08"):
            return "backspace"
        if first == "\x03":
            raise KeyboardInterrupt
        return first
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# ─────────────────────────────────────────────────────────── screen

class Screen:
    """Owns the terminal: alternate buffer, hidden cursor, one write per frame."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout
        self.active = False
        self._last_height = 0

    # size ---------------------------------------------------------
    @property
    def size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(100, 30))
        return max(size.columns, 60), max(size.lines, 20)

    # lifecycle ----------------------------------------------------
    def enter(self) -> None:
        if COLOUR:
            self.stream.write("\033[?1049h\033[?25l")     # alt buffer, hide cursor
            self.stream.flush()
        self.active = True

    def leave(self) -> None:
        if COLOUR:
            self.stream.write("\033[?25h\033[?1049l")     # show cursor, restore buffer
            self.stream.flush()
        self.active = False

    def __enter__(self) -> "Screen":
        self.enter()
        return self

    def __exit__(self, *exc) -> None:
        self.leave()

    # drawing ------------------------------------------------------
    def draw(self, lines: list[str]) -> None:
        width, height = self.size
        body = []
        for line in lines[:height - 1]:
            body.append(line + ("\033[K" if COLOUR else ""))
        # Wipe any rows the previous, taller frame left behind.
        for _ in range(max(self._last_height - len(body), 0)):
            body.append("\033[K" if COLOUR else " " * width)
        self._last_height = len(lines[:height - 1])
        prefix = "\033[H" if COLOUR else "\n"
        self.stream.write(prefix + "\n".join(body))
        self.stream.flush()

    def prompt(self, question: str) -> str:
        """A line of typed input at the bottom of the screen (rare — most input is single-key)."""
        width, height = self.size
        if COLOUR:
            self.stream.write(f"\033[{height};1H\033[K\033[?25h")
        self.stream.write(question)
        self.stream.flush()
        try:
            answer = input()
        except EOFError:
            answer = ""
        if COLOUR:
            self.stream.write("\033[?25l")
            self.stream.flush()
        return answer


# ─────────────────────────────────────────────────────────── list view

@dataclass
class ListView:
    """Scrollable, filterable list. Owns only selection state — rendering is pure."""
    items: list[str] = field(default_factory=list)
    index: int = 0
    offset: int = 0
    filter_text: str = ""
    filtering: bool = False

    @property
    def visible_items(self) -> list[tuple[int, str]]:
        if not self.filter_text:
            return list(enumerate(self.items))
        needle = self.filter_text.casefold()
        return [(position, item) for position, item in enumerate(self.items)
                if needle in item.casefold()]

    def clamp(self, height: int) -> None:
        total = len(self.visible_items)
        self.index = max(0, min(self.index, max(total - 1, 0)))
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + height:
            self.offset = self.index - height + 1
        self.offset = max(0, min(self.offset, max(total - height, 0)))

    def selected(self) -> int | None:
        rows = self.visible_items
        if not rows or self.index >= len(rows):
            return None
        return rows[self.index][0]

    def handle(self, key: str, height: int) -> bool:
        """Returns True if the key was consumed by the list."""
        if self.filtering:
            if key == "enter":
                self.filtering = False
            elif key == "esc":
                self.filtering = False
                self.filter_text = ""
            elif key == "backspace":
                self.filter_text = self.filter_text[:-1]
            elif len(key) == 1 and key.isprintable():
                self.filter_text += key
            else:
                return False
            self.index = 0
            self.offset = 0
            return True

        if key == "up":
            self.index -= 1
        elif key == "down":
            self.index += 1
        elif key == "pgup":
            self.index -= height
        elif key == "pgdn":
            self.index += height
        elif key == "home":
            self.index = 0
        elif key == "end":
            self.index = len(self.visible_items) - 1
        elif key == "/":
            self.filtering = True
            self.filter_text = ""
        else:
            return False
        self.clamp(height)
        return True

    def render(self, width: int, height: int, marker: str = "›") -> list[str]:
        self.clamp(height)
        rows = self.visible_items
        lines: list[str] = []
        for position in range(self.offset, min(self.offset + height, len(rows))):
            _, text = rows[position]
            selected = position == self.index
            prefix = f"{marker} " if selected else "  "
            line = fit(prefix + text, width)
            lines.append(style(line, bg="selected", bold=True) if selected else line)
        while len(lines) < height:
            lines.append(" " * width)
        return lines

    def status(self, width: int) -> str:
        rows = self.visible_items
        position = f"{self.index + 1}/{len(rows)}" if rows else "0/0"
        if self.filtering:
            return fit(f"/{self.filter_text}▌   {position}", width)
        if self.filter_text:
            return fit(f"filter: {self.filter_text}   {position}   (esc clears)", width)
        return fit(position, width)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
