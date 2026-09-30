"""Everything the bridge transmits goes through here.

Two jobs:
1. Airtime: at most one packet per `min_packet_interval_s`, highest priority first.
   Consecutive short system replies are coalesced into one packet when they fit.
2. Ack/retry state machine per packet:

     pending --tx--> inflight --ack from pocket node--> acked
                        |
                        +--NAK or 60 s timeout--> waiting --after 1/3/10 min--> (tx again)
                                                     |
                                    schedule exhausted (or retries=False)
                                                     v
                                                   parked

   An ack counts only if errorReason is NONE *and* it came from the pocket node.
   An implicit ack (NONE from another node, e.g. our own gateway hearing a
   rebroadcast) is ignored: we keep waiting for the real one until the timer fires.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

from .text import blen
from .transports import Clock, Mesh, MeshError

log = logging.getLogger(__name__)


class Prio(IntEnum):
    SYS = 0      # confirmations, command replies
    READ = 1     # chunks the owner asked for with `r`
    DIGEST = 2
    MSG = 3      # compact inbound lines
    PROBE = 4


@dataclass
class Packet:
    text: str
    prio: Prio
    ref: object = None            # opaque to the outbox; the bridge's bookkeeping
    retries: bool = True
    seq: int = 0
    state: str = "pending"        # pending | inflight | waiting | acked | parked
    attempts: int = 0
    next_at: float = 0.0
    sent_at: float | None = None
    packet_ids: list[int] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state in ("pending", "inflight", "waiting")


class Outbox:
    def __init__(
        self,
        mesh: Mesh,
        clock: Clock,
        *,
        pocket_node_num: int,
        budget: int,
        min_interval_s: float,
        ack_timeout_s: float,
        retry_schedule_s: tuple[float, ...],
        on_done: Callable[[Packet, bool], None],
    ):
        self.mesh = mesh
        self.clock = clock
        self.pocket = pocket_node_num
        self.budget = budget
        self.min_interval = min_interval_s
        self.ack_timeout = ack_timeout_s
        self.schedule = retry_schedule_s
        self.on_done = on_done
        self.packets: list[Packet] = []
        self.last_tx: float | None = None
        self.last_rtt: float | None = None
        self._seq = itertools.count()

    # -- API --------------------------------------------------------------

    def enqueue(self, text: str, prio: Prio, ref: object = None, retries: bool = True) -> Packet:
        if blen(text) > self.budget:
            raise ValueError(f"packet over budget ({blen(text)} > {self.budget} bytes)")
        p = Packet(text=text, prio=prio, ref=ref, retries=retries, seq=next(self._seq),
                   next_at=self.clock.now())
        self.packets.append(p)
        return p

    def active(self, prio: Prio | None = None) -> list[Packet]:
        return [p for p in self.packets if p.active and (prio is None or p.prio == prio)]

    def on_ack(self, packet_id: int, from_num: int, error_reason: str) -> Packet | None:
        p = next((p for p in self.packets if packet_id in p.packet_ids), None)
        if p is None or not p.active:
            return None
        now = self.clock.now()
        if error_reason == "NONE":
            if from_num != self.pocket:
                log.debug("implicit ack for %s from %x, still waiting", packet_id, from_num)
                return None
            p.state = "acked"
            if p.sent_at is not None:
                self.last_rtt = now - p.sent_at
            self._finish(p, True)
        elif p.state == "inflight":
            log.info("NAK %s for packet %s", error_reason, packet_id)
            self._failed(p, now)
        return p

    def tick(self) -> None:
        now = self.clock.now()
        for p in self.packets:
            if p.state == "inflight" and now - (p.sent_at or now) >= self.ack_timeout:
                log.info("ack timeout for packet %s", p.packet_ids[-1])
                self._failed(p, now)
        if self.last_tx is not None and now - self.last_tx < self.min_interval:
            return
        ready = [p for p in self.packets
                 if p.state in ("pending", "waiting") and p.next_at <= now]
        if not ready:
            return
        ready.sort(key=lambda p: (p.prio, p.seq))
        p = ready[0]
        if p.prio == Prio.SYS and p.state == "pending":
            p = self._coalesce(p, [q for q in ready[1:]
                                   if q.prio == Prio.SYS and q.state == "pending"])
        self._transmit(p, now)

    # -- internals --------------------------------------------------------

    def _coalesce(self, first: Packet, others: list[Packet]) -> Packet:
        for q in others:
            joined = f"{first.text}\n{q.text}"
            if blen(joined) > self.budget:
                break
            first.text = joined
            q.state = "acked"  # absorbed; nothing to report for sys packets
            self.packets.remove(q)
        return first

    def _transmit(self, p: Packet, now: float) -> None:
        try:
            pid = self.mesh.send_dm(p.text)
        except MeshError as e:
            log.warning("mesh send failed: %s", e)
            self.last_tx = now  # still back off; don't hammer a dead port
            p.next_at = now + self.min_interval
            return
        self.last_tx = now
        p.attempts += 1
        p.packet_ids.append(pid)
        p.state = "inflight"
        p.sent_at = now

    def _failed(self, p: Packet, now: float) -> None:
        k = p.attempts - 1  # retries already used
        if p.retries and k < len(self.schedule):
            p.state = "waiting"
            p.next_at = now + self.schedule[k]
        else:
            p.state = "parked"
            self._finish(p, False)

    def _finish(self, p: Packet, ok: bool) -> None:
        self.packets.remove(p)
        self.on_done(p, ok)
