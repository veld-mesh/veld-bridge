"""Fuzzy contact lookup for `@sam`, `r sam`, `mute sam`."""

from __future__ import annotations

import unicodedata

from .text import clean


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", clean(s)).casefold()
    return "".join(c for c in s if not unicodedata.combining(c))


def match(query: str, candidates: dict[str, str]) -> list[tuple[str, str]]:
    """candidates: chat_id -> display name. Returns [] (none), [one] or the ambiguous set.

    Tiers, first non-empty wins: exact full name, prefix of any word,
    substring. So `sam` picks "Sam" if there is one, but is ambiguous
    between "Sam Smith" and "Samantha K" (per the brief).
    """
    q = _norm(query).lstrip("@")
    if not q:
        return []
    items = [(cid, name, _norm(name)) for cid, name in candidates.items() if name]
    tiers = [
        lambda n: n == q,
        lambda n: any(w.startswith(q) for w in n.split(" ")),
        lambda n: q in n,
    ]
    for tier in tiers:
        hits = [(cid, name) for cid, name, n in items if tier(n)]
        if hits:
            return sorted(hits, key=lambda h: h[1].casefold())
    return []
