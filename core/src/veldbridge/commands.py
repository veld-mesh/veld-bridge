"""Parse DMs from the pocket node into commands. Pure; no lookups happen here."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Read:
    """`r`, `r 4`, `r sam`. Exactly one of short_id/name, or neither for next queued."""
    short_id: int | None = None
    name: str | None = None


@dataclass(frozen=True)
class Reply:
    """`@4 text`, `@sam text`, or bare text (target None = last chat read)."""
    text: str
    short_id: int | None = None
    name: str | None = None


@dataclass(frozen=True)
class ListWaiting:
    pass


@dataclass(frozen=True)
class Mute:
    name: str
    on: bool


@dataclass(frozen=True)
class AllMode:
    on: bool


@dataclass(frozen=True)
class Status:
    pass


@dataclass(frozen=True)
class Ping:
    pass


@dataclass(frozen=True)
class Pause:
    """`pause` / `resume`: hold relaying WhatsApp to the mesh until resumed."""
    on: bool


@dataclass(frozen=True)
class Invalid:
    message: str


Command = Read | Reply | ListWaiting | Mute | AllMode | Status | Ping | Pause | Invalid

_AT = re.compile(r"^@(\S+)(?:\s+(.*))?$", re.S)


def _target(tok: str) -> tuple[int | None, str | None]:
    # Phones add punctuation: "@thabo. hi" or "@4, ok" still mean thabo / #4.
    tok = tok.lstrip("#").rstrip(".,:;!?")
    if tok.isdigit():
        return int(tok), None
    return None, tok


def parse(raw: str) -> Command | None:
    """None for empty input. Keywords are case-insensitive (phones auto-capitalise)."""
    s = (raw or "").strip()
    if not s:
        return None
    words = s.split()
    head = words[0].lower()
    args = words[1:]

    if head == "r" and len(args) <= 1:
        if not args:
            return Read()
        sid, name = _target(args[0])
        return Read(short_id=sid, name=name)
    if head == "l" and not args:
        return ListWaiting()
    if head == "s" and not args:
        return Status()
    if head == "ping" and not args:
        return Ping()
    if head in ("pause", "resume") and not args:
        return Pause(on=head == "pause")
    if head == "all" and len(args) == 1 and args[0].lower() in ("on", "off"):
        return AllMode(on=args[0].lower() == "on")
    if head in ("mute", "unmute") and len(args) == 1:
        return Mute(name=args[0], on=head == "mute")

    m = _AT.match(s)
    if m:
        sid, name = _target(m.group(1))
        text = (m.group(2) or "").strip()
        if not text:
            return Invalid("? nothing to send — @name text")
        return Reply(text=text, short_id=sid, name=name)

    return Reply(text=s)
