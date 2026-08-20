"""Track identity.

Three systems have to agree on what "the same song" is, and none of them share an id:

* the **device** writes `cinder_loved.tsv` — `artist \t title`, taken from the Sony MediaStore
  tags, and nothing else (object ids are meaningless off-device, which is why Cinder exports
  this file separately from `cinder_liked.conf`);
* **Last.fm** returns whatever the scrobbler sent, which is the same tag text but often with a
  different feat./remaster suffix;
* **MusicBee** knows the file path, which the other two never see.

So the only key all three can produce is *artist + title*, normalised hard enough to survive
punctuation and suffix drift and no harder. Every widening of this function is a chance to merge
two genuinely different songs, which silently loses a like — so the noise list below is short and
deliberately excludes anything that marks a *different recording* (live, remix, acoustic, demo).
"""
from __future__ import annotations

import re
import unicodedata

# Curly punctuation → ASCII. Tag editors, Last.fm and Sony's scanner all disagree here.
_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
    " ": " ", "​": "",
})

# Suffixes that mark the same recording under a different release. NOT remix/live/acoustic/
# demo/instrumental — those are different recordings and must stay distinct.
_NOISE = re.compile(
    r"""\s*[\(\[\-–—]\s*(
        \d{4}\s+)?(re-?master(ed)?(\s+\d{4})?
        | remaster
        | explicit(\s+version)?
        | clean(\s+version)?
        | album\s+version
        | single\s+version
        | original\s+version
        | bonus\s+track
        | deluxe(\s+edition)?
        | expanded(\s+edition)?
        | anniversary(\s+edition)?
        | mono | stereo
        | \d{4}\s+(stereo\s+|mono\s+)?mix
        | (stereo|mono)\s+mix(\s+\d{4})?
    )\s*[\)\]]?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# "feat." in every spelling anyone uses. Last.fm strips it from the artist and keeps it in the
# title about half the time, so it is removed from both sides.
_FEAT = re.compile(
    r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring|with)\s+[^\)\]]*[\)\]]?\s*$",
    re.IGNORECASE,
)

_MULTI_ARTIST = re.compile(r"\s*(?:;|/|\bvs\.?\b|\band\b|&|,)\s*", re.IGNORECASE)


def _base(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").translate(_PUNCT)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split())


def norm_title(value: str) -> str:
    text = _base(value)
    # Repeat: "Song (Remastered) (feat. X)" needs both passes, in either order.
    for _ in range(3):
        stripped = _FEAT.sub("", _NOISE.sub("", text)).strip()
        if stripped == text:
            break
        text = stripped or text
    return text.casefold()


def norm_artist(value: str) -> str:
    text = _FEAT.sub("", _base(value)).strip()
    return text.casefold()


def primary_artist(value: str) -> str:
    """First credited artist only — the fallback key when a collaboration is spelled differently.

    "A & B", "A feat. B" and "A, B" are one track credited three ways across the three systems;
    matching on the first name recovers it. Used ONLY as a second pass, after the exact key
    misses, because it also merges genuinely different collaborations.
    """
    parts = _MULTI_ARTIST.split(norm_artist(value), maxsplit=1)
    return parts[0].strip() if parts else norm_artist(value)


def track_key(artist: str, title: str) -> str:
    """The canonical id used everywhere in this package. Unit separator can't occur in a tag."""
    return f"{norm_artist(artist)}␟{norm_title(title)}"


def loose_key(artist: str, title: str) -> str:
    """Second-pass key: primary artist only. Never written to state, only used for matching."""
    return f"{primary_artist(artist)}␟{norm_title(title)}"


def artist_candidates(value: str) -> list[str]:
    """Every artist a credit string could reasonably mean, most specific first.

    "M-Beat; Jamiroquai" is one track filed under either name depending on who tagged it, and
    "Little Simz feat. Cleo Sol" is filed on the album under *Cleo Sol* — the featured artist is
    the track artist and the album artist is the other one. Trying each part in turn recovers
    both, and because the title still has to match exactly it does not widen matching much.
    """
    full = norm_artist(value)
    seen = [full] if full else []
    for part in _MULTI_ARTIST.split(full):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    # The un-stripped form too, for artists whose name genuinely contains "and"/"&".
    raw = _base(value).casefold()
    if raw and raw not in seen:
        seen.append(raw)
    return seen


def split_key(key: str) -> tuple[str, str]:
    artist, _, title = key.partition("␟")
    return artist, title
