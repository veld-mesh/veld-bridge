"""The seams between core logic and the outside world, plus in-memory fakes.

The bridge never calls meshtastic or the WA socket directly; it calls these.
Tests (and CI) use the fakes; the Pi uses mesh_serial.SerialMesh and
wa_socket.WaSocketServer, which implement the same methods.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Protocol


class MeshError(RuntimeError):
    pass


class Clock(Protocol):
    def now(self) -> float: ...


class Mesh(Protocol):
    def send_dm(self, text: str) -> int:
        """Queue one DM with wantAck to the pocket node; return its packet id.

        Raises MeshError if the radio is not connected. Acks come back via
        Bridge.on_mesh_ack(packet_id, from_num, error_reason).
        """
        ...


class WhatsApp(Protocol):
    """Fire-and-forget requests; results come back via Bridge.on_wa_result(req, ...)."""

    def send(self, req: int, chat_id: str, text: str) -> None: ...

    def mark_seen(self, req: int, chat_id: str) -> None: ...

    def get_contacts(self, req: int) -> None: ...


class SystemClock:
    def now(self) -> float:
        import time

        return time.time()


@dataclass
class FakeClock:
    t: float = 1_790_000_000.0  # 2026-09-21, a fixed point so tests are stable

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class FakeMesh:
    """Records every DM. Tests ack/NAK them by calling the bridge directly."""

    sent: list[tuple[int, str]] = field(default_factory=list)
    connected: bool = True
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1000))

    def send_dm(self, text: str) -> int:
        if not self.connected:
            raise MeshError("serial not connected")
        pid = next(self._ids)
        self.sent.append((pid, text))
        return pid

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.sent]

    def last(self) -> tuple[int, str]:
        return self.sent[-1]


@dataclass
class FakeWhatsApp:
    calls: list[tuple] = field(default_factory=list)

    def send(self, req: int, chat_id: str, text: str) -> None:
        self.calls.append(("send", req, chat_id, text))

    def mark_seen(self, req: int, chat_id: str) -> None:
        self.calls.append(("markSeen", req, chat_id))

    def get_contacts(self, req: int) -> None:
        self.calls.append(("getContacts", req))

    def of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]
