"""Compaction, byte-based truncation and chunking. Pure functions, no I/O.

Every size here is UTF-8 bytes, never characters.
"""

from __future__ import annotations

import re

ELLIPSIS = "…"  # 3 bytes

_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001ffff"  # emoticons, pictographs, flags, transport, supplemental
    "☀-➿"          # misc symbols + dingbats
    "⌀-⏿"          # watch, hourglass, media keys
    "⬀-⯿"          # stars, arrows used as emoji
    "︀-️"          # variation selectors
    "‍"                 # zero-width joiner
    "⃣"                 # keycap
    "\U000e0020-\U000e007f"  # tag sequences (subdivision flags)
    "〰〽㊗㊙"
    "]+"
)
_WS = re.compile(r"\s+")


def blen(s: str) -> int:
    return len(s.encode("utf-8"))


def clean(s: str) -> str:
    """Strip emoji, collapse all whitespace (incl. newlines) to single spaces."""
    return _WS.sub(" ", _EMOJI.sub(" ", s or "")).strip()


def cut_bytes(s: str, limit: int) -> str:
    """Longest prefix of s that fits in `limit` bytes, never splitting a character."""
    if limit <= 0:
        return ""
    raw = s.encode("utf-8")
    if len(raw) <= limit:
        return s
    return raw[:limit].decode("utf-8", errors="ignore")


def truncate(s: str, limit: int) -> str:
    """Fit s into `limit` bytes, ending in … if anything was cut."""
    if blen(s) <= limit:
        return s
    return cut_bytes(s, limit - blen(ELLIPSIS)).rstrip() + ELLIPSIS


def first_name(name: str) -> str:
    return name.split(" ", 1)[0] if name else name


def fmt_duration(seconds: float | int | None) -> str:
    s = int(seconds or 0)
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def media_label(
    kind: str | None,
    *,
    filename: str | None = None,
    duration: float | None = None,
    lat: float | None = None,
    lon: float | None = None,
    transcript: str | None = None,
) -> str | None:
    """WA media kind (normalised by the Node adapter) -> bracketed label."""
    if not kind or kind == "text":
        return None
    if kind == "photo":
        return "[photo]"
    if kind == "video":
        return "[video]"
    if kind == "gif":
        return "[gif]"
    if kind == "sticker":
        return "[sticker]"
    if kind == "voice" or kind == "audio":
        if transcript:
            return f"[voice] {clean(transcript)}"
        return f"[voice {fmt_duration(duration)}]" if duration else "[voice]"
    if kind == "document":
        name = clean(filename or "")
        if name.lower().endswith(".pdf"):
            return f"[pdf: {name}]"
        return f"[doc: {name}]" if name else "[doc]"
    if kind == "location":
        if lat is None or lon is None:
            return "[loc]"
        return f"[loc] {lat:.5f},{lon:.5f}"
    if kind == "contact":
        return "[contact]"
    return f"[{clean(kind)}]"


def body_of(text: str, label: str | None) -> str:
    """The single-line body we relay: label first, then cleaned text/caption."""
    t = clean(text)
    if label:
        return f"{label} {t}".strip()
    return t


def compact_line(short_id: int, name: str, body: str, budget: int) -> str:
    """`#<n> <Name>: <text>` within `budget` bytes.

    Full name if the whole line fits, else first name; then truncate the text.
    """
    name = clean(name) or "?"
    line = f"#{short_id} {name}: {body}"
    if blen(line) <= budget:
        return line
    short = first_name(name)
    head = f"#{short_id} {short}: "
    if blen(head) > budget // 2:  # absurdly long single-word name
        head = f"#{short_id} {truncate(short, budget // 2 - 6)}: "
    return head + truncate(body, budget - blen(head))


def full_text(short_id: int, name: str, body: str) -> str:
    return f"#{short_id} {clean(name) or '?'}: {body}"


def _split_point(s: str, limit: int) -> int:
    """Char index to split s so s[:i] fits `limit` bytes, preferring a space."""
    piece = cut_bytes(s, limit)
    if len(piece) == len(s):
        return len(s)
    space = piece.rfind(" ")
    # Only back off to a word break if it doesn't waste more than ~1/4 of the packet.
    if space > 0 and blen(piece[space:]) <= limit // 4:
        return space
    return len(piece)


def chunk(text: str, budget: int) -> list[str]:
    """Split into packets of <= budget bytes. Multi-part packets get a `[i/n] ` prefix."""
    if blen(text) <= budget:
        return [text]
    n = 2
    while True:
        prefix_len = blen(f"[{n}/{n}] ")
        parts: list[str] = []
        rest = text
        while rest:
            i = _split_point(rest, budget - prefix_len)
            if i == 0:  # can't happen with sane budgets; guard against infinite loop
                raise ValueError("budget too small to chunk")
            parts.append(rest[:i].strip())
            rest = rest[i:].lstrip()
        if len(parts) <= n or len(str(len(parts))) == len(str(n)):
            total = len(parts)
            return [f"[{i}/{total}] {p}" for i, p in enumerate(parts, 1)]
        n = len(parts)


def fit_list(head: str, items: list[str], tail: str, budget: int, sep: str = ", ") -> str:
    """`head` + as many items as fit + `tail`; overflow collapses into `+N`."""
    for k in range(len(items), -1, -1):
        shown = items[:k]
        more = len(items) - k
        body = sep.join(shown + ([f"+{more}"] if more else []))
        line = f"{head}{body}{tail}"
        if blen(line) <= budget:
            return line
    return truncate(f"{head}{tail}", budget)
