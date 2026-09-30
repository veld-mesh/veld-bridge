"""All bridge behaviour. Event handlers + a 1 Hz `tick()`; no threads, no I/O of its own.

Inputs:  on_wa_message / on_wa_state / on_wa_result / on_wa_contacts   (from wa/)
         on_wa_chat_read / on_wa_own    (owner using WhatsApp on their phone -> pause relay)
         on_mesh_packet / on_mesh_ack                                  (from the T-Beam)
         (channel texts starting 🆘 / ✅ from any node -> WhatsApp SOS alerts)
         tick()                                                        (timers)
Outputs: the Mesh and WhatsApp transports, via the Outbox for anything on air.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from . import commands as C
from .config import Config
from .db import Message, Store
from .match import match
from .outbox import Outbox, Packet, Prio
from .text import (
    body_of,
    chunk,
    clean,
    compact_line,
    first_name,
    fit_list,
    full_text,
    media_label,
    truncate,
)
from .transports import Clock, Mesh, WhatsApp

log = logging.getLogger(__name__)

IGNORED_SUFFIXES = ("@broadcast", "@newsletter")
WA_DOWN_ALERT_S = 600  # a restart takes ~1 min; 10 min down is worth a mesh DM
OWN_READ_GRACE_S = 30  # unread->0 this soon after our own send/markSeen is us, not the owner
OWN_STALE_S = 120      # an own message older than this is history sync, not the owner now
# SOS-button nodes send "🆘 SOS Bert! lat,lon https://maps..." every 2 min until
# "✅ SOS Bert cancelled". WhatsApp gets the first, then an update at most this often.
SOS_PREFIX = "🆘"
SOS_CANCEL_PREFIX = "✅"
SOS_UPDATE_S = 600


@dataclass
class Compact:
    message_id: int


@dataclass
class FullRead:
    message_id: int
    prev_state: str
    remaining: int
    failed: bool = False


@dataclass
class Probe:
    pass


class Bridge:
    def __init__(self, cfg: Config, store: Store, mesh: Mesh, wa: WhatsApp, clock: Clock):
        self.cfg = cfg
        self.store = store
        self.wa = wa
        self.clock = clock
        self.tz = ZoneInfo(cfg.timezone)
        self.budget = cfg.mesh.byte_budget
        self.pocket = cfg.mesh.pocket_node_num
        self.outbox = Outbox(
            mesh, clock,
            pocket_node_num=self.pocket,
            budget=self.budget,
            min_interval_s=cfg.mesh.min_packet_interval_s,
            ack_timeout_s=cfg.mesh.ack_timeout_s,
            retry_schedule_s=cfg.mesh.retry_schedule_s,
            on_done=self._packet_done,
        )
        self.started_at = clock.now()
        self.wa_state = "DISCONNECTED"
        self.wa_down_since: float | None = self.started_at
        self.mesh_connected = False
        self.last_heard: float | None = None
        self.last_rx: dict | None = None
        self.held = False            # flush hit the cap; the rest waits for `r`
        self.last_probe_at: float | None = None
        self.last_flush_at: float | None = None
        self.probe_acked_at: float | None = None
        self.wa_next_at = 0.0
        self.pending_seen: set[str] = set()
        # chat id -> when the bridge itself sent/marked seen there (zeroes unread too)
        self._touched: dict[str, float] = {}
        self._req = 0
        self._wa_reqs: dict[int, tuple] = {}
        self._seen_packets: deque[int] = deque(maxlen=200)
        self.phone_active_at: float | None = None  # owner last read/sent on their phone
        self._was_paused = False
        self._sos_last: dict[int, float] = {}   # node num -> last SOS forwarded to WA
        self._sos_outbound: set[int] = set()    # outbound ids that are SOS alerts
        store.recover()

    # =====================================================================
    # WhatsApp -> mesh
    # =====================================================================

    def relay_all(self) -> bool:
        return bool(self.store.get_setting("all", self.cfg.relay.all))

    def muted(self) -> set[str]:
        return set(self.store.get_setting("muted", list(self.cfg.relay.muted)))

    def allowlist(self) -> dict[str, str]:
        """Favourites and relayed groups. config.yaml seeds it; the admin page edits it."""
        rows = self.store.get_setting("allowlist")
        if rows is None:
            return {e.chat_id: e.name for e in self.cfg.relay.allowlist}
        return dict(rows)

    def set_allowed(self, chat_id: str, name: str, on: bool) -> None:
        allow = self.allowlist()
        if on:
            allow[chat_id] = name
        else:
            allow.pop(chat_id, None)
        self.store.set_setting("allowlist", sorted(allow.items(), key=lambda kv: kv[1].lower()))

    def set_muted(self, chat_id: str, on: bool) -> None:
        muted = self.muted()
        (muted.add if on else muted.discard)(chat_id)
        self.store.set_setting("muted", sorted(muted))

    def mark_seen_mode(self) -> str:
        return self.store.get_setting("mark_seen", self.cfg.relay.mark_seen)

    def wants(self, chat_id: str, is_group: bool, aliases: tuple[str, ...] = ()) -> bool:
        """`aliases`: the same person's other WA id (@c.us <-> @lid). A favourite or
        mute saved under one id must still apply when they write from the other."""
        ids = {chat_id, *aliases}
        if chat_id.endswith(IGNORED_SUFFIXES) or ids & self.muted():
            return False
        if ids & self.allowlist().keys():
            return True
        return not is_group and self.relay_all()

    def on_wa_message(self, m: dict) -> Message | None:
        chat_id = m["chatId"]
        is_group = bool(m.get("isGroup")) or chat_id.endswith("@g.us")
        aliases = tuple(a for a in (m.get("aliases") or ()) if isinstance(a, str))
        if m.get("fromMe") or not self.wants(chat_id, is_group, aliases):
            if not is_group:
                log.info("wa not relayed (filter) from %s", chat_id)
            return None
        now = self.clock.now()
        if self._stale(m.get("timestamp"), now):
            log.info("wa skipped old message from %s", chat_id)
            return None
        person = clean(m.get("senderName") or m.get("pushname") or "") or \
            self.store.contact_name(m.get("author") or chat_id) or chat_id.split("@")[0]
        if is_group:
            group = clean(m.get("chatName") or "") or self.allowlist().get(chat_id, "group")
            chat_name, sender = group, f"{first_name(person)}/{first_name(group)}"
        else:
            chat_name = sender = person
            self.store.upsert_contact(chat_id, m.get("senderName"), m.get("pushname"),
                                      False, now)
        label = media_label(m.get("kind"), filename=m.get("filename"),
                            duration=m.get("duration"), lat=m.get("lat"), lon=m.get("lon"))
        msg = self.store.add_message(
            wa_msg_id=m["id"], day=self._day(now), chat_id=chat_id, chat_name=chat_name,
            sender_name=sender, text=m.get("body") or "", media_kind=m.get("kind"),
            media_label=label, received_at=now,
        )
        if msg is None:
            return None
        log.info("wa in #%s from %s", msg.short_id, chat_id)
        log.debug("wa body #%s: %r", msg.short_id, msg.text)
        if self._live():
            self._send_compact(msg)
        return msg

    def _stale(self, sent_at: float | None, now: float) -> bool:
        """Replayed history: sent before the first link, or older than max_age_s."""
        if sent_at is None:
            return False
        linked_at = self.store.get_setting("linked_at", self.started_at)
        return sent_at < linked_at or now - sent_at > self.cfg.relay.max_age_s

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, self.tz).date().isoformat()

    def reachable(self) -> bool:
        return (self.last_heard is not None
                and self.clock.now() - self.last_heard < self.cfg.mesh.reachable_window_s)

    def _live(self) -> bool:
        """Deliver new messages straight away? Only if the owner is around, not paging,
        and not paused (by command, or because the owner is on WhatsApp on their phone)."""
        return self.reachable() and not self.held and self.paused() is None

    # =====================================================================
    # pause: the owner has WhatsApp on their phone, so the mesh stays quiet
    # =====================================================================

    def phone_pause_on(self) -> bool:
        return bool(self.store.get_setting("phone_pause", self.cfg.relay.phone_pause_s > 0))

    def phone_pause_left(self) -> float | None:
        """Seconds until the phone pause lapses, or None if it isn't holding."""
        if not self.phone_pause_on() or self.phone_active_at is None:
            return None
        left = self.phone_active_at + self.cfg.relay.phone_pause_s - self.clock.now()
        return left if left > 0 else None

    def paused(self) -> str | None:
        """'manual' (pause command / admin page), 'phone' (the owner read or sent a
        WhatsApp on their phone within phone_pause_s), or None."""
        if self.store.get_setting("paused"):
            return "manual"
        if self.phone_pause_left() is not None:
            return "phone"
        return None

    def set_paused(self, on: bool) -> None:
        self.store.set_setting("paused", on)
        if not on:
            self.phone_active_at = None  # "resume" means now, phone or not

    def _phone_active(self, why: str) -> None:
        if self.paused() is None and self.phone_pause_on():
            log.info("phone active (%s): holding mesh relay", why)
        self.phone_active_at = self.clock.now()

    def on_wa_own(self, chat_id: str, timestamp: float | None = None) -> None:
        """The owner sent a WhatsApp from their phone (or WA Web): they have WhatsApp there."""
        now = self.clock.now()
        if now - self._touched.get(chat_id, float("-inf")) < OWN_READ_GRACE_S:
            return  # the bridge's own send (a mesh reply or an SOS alert)
        if timestamp is not None and now - timestamp > OWN_STALE_S:
            return
        self._phone_active("sent")

    def _flushing(self) -> bool:
        return any(isinstance(p.ref, Compact) for p in self.outbox.active())

    def _send_compact(self, msg: Message) -> None:
        body = body_of(msg.text, msg.media_label)
        line = compact_line(msg.short_id, msg.sender_name, body, self.budget)
        self.store.set_message_state(msg.id, "sent")
        self.outbox.enqueue(line, Prio.MSG, Compact(msg.id))

    def digest(self, msgs: list[Message]) -> str:
        counts: dict[str, int] = {}
        for m in msgs:
            n = first_name(clean(m.sender_name)) or "?"
            counts[n] = counts.get(n, 0) + 1
        names = [n if c == 1 else f"{n}×{c}" for n, c in counts.items()]
        return fit_list(f"{len(msgs)} waiting: ", names, " — r to read", self.budget)

    def flush(self) -> None:
        """Node just became reachable: digest, then oldest-first up to the cap."""
        now = self.clock.now()
        self.last_flush_at = now
        queued = self.store.queued()
        if not queued:
            self.held = False
            return
        probe_just_acked = self.probe_acked_at is not None and now - self.probe_acked_at < 60
        if len(queued) > 1 and not probe_just_acked:
            self.outbox.enqueue(self.digest(queued), Prio.DIGEST)
        cap = self.cfg.mesh.flush_cap
        for msg in queued[:cap]:
            self._send_compact(msg)
        rest = len(queued) - cap
        self.held = rest > 0
        if self.held:
            # Same priority as the lines so it lands after them.
            self.outbox.enqueue(f"+{rest} more, r for next", Prio.MSG)

    # =====================================================================
    # mesh -> bridge
    # =====================================================================

    def on_mesh_packet(self, *, from_num: int, packet_id: int, is_dm: bool,
                       text: str | None, rssi: float | None = None,
                       snr: float | None = None) -> None:
        """Any packet the gateway hears. Only DMs from the pocket node are commands.

        SOS texts on the channel count from any node."""
        if (text is not None and not is_dm
                and text.lstrip().startswith((SOS_PREFIX, SOS_CANCEL_PREFIX))
                and packet_id not in self._seen_packets):
            self._seen_packets.append(packet_id)
            self.on_sos(from_num, text)
        if from_num != self.pocket:
            return
        self._heard()
        self.last_rx = {"rssi": rssi, "snr": snr}
        if not is_dm or text is None:
            return
        if packet_id in self._seen_packets:
            return
        self._seen_packets.append(packet_id)
        cmd = C.parse(text)
        if cmd is not None:
            log.info("mesh cmd %s", type(cmd).__name__)
            self.handle(cmd, packet_id)

    # =====================================================================
    # SOS -> WhatsApp
    # =====================================================================

    def sos_recipients(self) -> dict[str, str]:
        return dict(self.store.get_setting("sos_recipients", []))

    def set_sos_recipient(self, chat_id: str, name: str, on: bool) -> None:
        rec = self.sos_recipients()
        if on:
            rec[chat_id] = name
        else:
            rec.pop(chat_id, None)
        self.store.set_setting("sos_recipients",
                               sorted(rec.items(), key=lambda kv: kv[1].lower()))

    def on_sos(self, from_num: int, text: str) -> None:
        """An SOS (or its cancel) heard on the channel: WhatsApp the SOS recipients.

        The first alert goes at once, repeats at most every SOS_UPDATE_S with the
        new position, and a cancel only if we alerted for that node."""
        now = self.clock.now()
        body = clean_sos(text)
        cancel = body.startswith(SOS_CANCEL_PREFIX)
        if cancel:
            if self._sos_last.pop(from_num, None) is None:
                return  # a ✅ we never raised an alert for
        else:
            last = self._sos_last.get(from_num)
            if last is not None and now - last < SOS_UPDATE_S:
                log.info("sos repeat from %x, next WhatsApp update later", from_num)
                return
            self._sos_last[from_num] = now
        log.warning("sos %s from %x", "cancel" if cancel else "alert", from_num)
        recipients = self.sos_recipients()
        if not recipients:
            if not cancel:
                self.say("⚠ SOS heard, no WhatsApp SOS recipients set on the admin page")
            return
        when = datetime.fromtimestamp(now, self.tz).strftime("%H:%M")
        rest = body.removeprefix(SOS_CANCEL_PREFIX if cancel else SOS_PREFIX).strip()
        if cancel:
            wa_text = (f"✅ *Mesh SOS cancelled* ({when})\n{rest}\n\n"
                       "Automatic message from the farm radio mesh.")
        else:
            wa_text = (f"🆘 *Mesh SOS alert* ({when})\n{rest}\n\n"
                       "Automatic message from the farm radio mesh. "
                       "Location updates follow every 10 min until it's cancelled.")
        for chat_id, name in recipients.items():
            ob = self.store.add_outbound(mesh_packet_id=None, chat_id=chat_id,
                                         chat_name=name, text=wa_text, created_at=now)
            self._sos_outbound.add(ob.id)
        if self.wa_state != "CONNECTED":
            self.say("⚠ WA offline, SOS WhatsApp queued")

    def on_mesh_ack(self, packet_id: int, from_num: int, error_reason: str) -> None:
        self.outbox.on_ack(packet_id, from_num, error_reason)
        if from_num == self.pocket:
            self._heard()

    def on_mesh_state(self, connected: bool) -> None:
        self.mesh_connected = connected

    def _heard(self) -> None:
        was = self.reachable()
        self.last_heard = self.clock.now()
        if not was:
            log.info("pocket node reachable")
            if not self.held and not self._flushing() and self.paused() is None:
                self.flush()

    def _packet_done(self, p: Packet, ok: bool) -> None:
        now = self.clock.now()
        ref = p.ref
        if isinstance(ref, Compact):
            msg = self.store.message(ref.message_id)
            if msg is None or msg.state != "sent":
                return
            if ok:
                self.store.set_message_state(msg.id, "acked", delivered_at=now)
                if self.mark_seen_mode() == "ack":
                    self._mark_seen(msg)
            else:
                log.info("parking #%s", msg.short_id)
                self.store.set_message_state(msg.id, "queued")
        elif isinstance(ref, FullRead):
            ref.remaining -= 1
            ref.failed |= not ok
            if ref.remaining == 0:
                msg = self.store.message(ref.message_id)
                if msg is None:
                    return
                if ref.failed:
                    prev = "queued" if ref.prev_state == "sent" else ref.prev_state
                    self.store.set_message_state(msg.id, prev)
                else:
                    self.store.set_message_state(msg.id, "read",
                                                 delivered_at=msg.delivered_at or now)
                    self._mark_seen(msg)
        elif isinstance(ref, Probe) and ok:
            self.probe_acked_at = now

    # =====================================================================
    # commands
    # =====================================================================

    def say(self, text: str) -> None:
        self.outbox.enqueue(truncate(text, self.budget), Prio.SYS)

    def candidates(self) -> dict[str, str]:
        """Chats the owner may address: ones that have written to them, plus the allowlist.

        This is what keeps the bridge from ever opening a new chat.
        """
        c = self.allowlist()
        for chat_id, name in self.store.chats_with_messages().items():
            # WA gives one person two ids (phone @c.us and @lid). A favourite saved
            # under one and replies arriving from the other made "@thabo" ambiguous
            # and nothing was sent. Keep the chat they actually write from.
            for other in [i for i, n in c.items() if n == name and i != chat_id]:
                del c[other]
            c[chat_id] = name
        return c

    def resolve(self, name: str) -> tuple[str, str] | None:
        hits = match(name, self.candidates())
        if len(hits) == 1:
            return hits[0]
        if not hits:
            self.say(f"? {name}: no match")
        else:
            self.say(fit_list(f"? {name}: ", [n for _, n in hits], "", self.budget))
        return None

    def handle(self, cmd: C.Command, packet_id: int | None = None) -> None:
        match cmd:
            case C.Read():
                self._cmd_read(cmd)
            case C.Reply():
                self._cmd_reply(cmd, packet_id)
            case C.ListWaiting():
                q = self.store.queued()
                items = [f"#{m.short_id} {first_name(clean(m.sender_name))}" for m in q]
                self.say(fit_list(f"{len(q)} waiting: ", items, "", self.budget)
                         if q else "0 waiting")
            case C.Mute(name=name, on=on):
                hit = self.resolve(name)
                if hit:
                    self.set_muted(hit[0], on)
                    self.say(f"{'muted' if on else 'unmuted'} {hit[1]}")
            case C.AllMode(on=on):
                self.store.set_setting("all", on)
                self.say("all on" if on else "all off: allowlist only")
            case C.Status():
                self.say(self.status_line())
            case C.Ping():
                rx = self.last_rx or {}
                self.say(f"pong rssi {_num(rx.get('rssi'))} snr {_num(rx.get('snr'))}")
            case C.Pause(on=on):
                self.set_paused(on)
                self.say("⏸ paused: WhatsApp stays on your phone. resume to restart" if on
                         else f"▶ resumed, {len(self.store.queued())} waiting")
            case C.Invalid(message=m):
                self.say(m)

    def _cmd_read(self, cmd: C.Read) -> None:
        msg: Message | None
        if cmd.short_id is not None:
            msg = self.store.message_by_short(cmd.short_id)
            if msg is None:
                return self.say(f"? #{cmd.short_id} not found")
        elif cmd.name is not None:
            hit = self.resolve(cmd.name)
            if hit is None:
                return
            msg = self.store.latest_from_chat(hit[0])
        else:
            queued = self.store.queued()
            if not queued:
                self.held = False
                return self.say("0 waiting")
            msg = queued[0]
        assert msg is not None
        self.store.set_setting("last_chat", [msg.chat_id, msg.chat_name])
        body = full_text(msg.short_id, msg.sender_name, body_of(msg.text, msg.media_label))
        parts = chunk(body, self.budget)
        ref = FullRead(msg.id, prev_state=msg.state, remaining=len(parts))
        self.store.set_message_state(msg.id, "sent")
        for part in parts:
            self.outbox.enqueue(part, Prio.READ, ref)
        if self.held and len(self.store.queued()) == 0:
            self.held = False

    def _cmd_reply(self, cmd: C.Reply, packet_id: int | None) -> None:
        target: tuple[str, str] | None
        if cmd.short_id is not None:
            msg = self.store.message_by_short(cmd.short_id)
            if msg is None:
                return self.say(f"? #{cmd.short_id} not found")
            target = (msg.chat_id, msg.chat_name)
        elif cmd.name is not None:
            target = self.resolve(cmd.name)
            if target is None:
                return
        else:
            last = self.store.get_setting("last_chat")
            if not last:
                return self.say("? no chat selected — @name text")
            target = (last[0], last[1])
        chat_id, chat_name = target
        self.store.set_setting("last_chat", [chat_id, chat_name])
        text = cmd.text + (self.cfg.whatsapp.signature or "")
        self.store.add_outbound(mesh_packet_id=packet_id, chat_id=chat_id,
                                chat_name=chat_name, text=text, created_at=self.clock.now())
        if self.wa_state != "CONNECTED":
            self.say("⚠ WA offline, queued")

    def status_line(self) -> str:
        wa = {"CONNECTED": "ok", "QR": "needs QR"}.get(self.wa_state, "offline")
        q = len(self.store.queued())
        rtt = self.outbox.last_rtt
        up = _dur(self.clock.now() - self.started_at)
        heard = "never" if self.last_heard is None else _dur(self.clock.now() - self.last_heard)
        p = self.paused()
        paused = "" if p is None else f" · paused ({p})"
        return (f"WA {wa} · q {q} · rtt {'-' if rtt is None else f'{rtt:.1f}s'}"
                f" · up {up} · heard {heard}{paused}")

    # =====================================================================
    # WhatsApp side
    # =====================================================================

    def _next_req(self) -> int:
        self._req += 1
        return self._req

    def _mark_seen(self, msg: Message) -> None:
        if self.mark_seen_mode() == "never":
            return
        self.store.mark_seen_at(msg.id, self.clock.now())
        self.pending_seen.add(msg.chat_id)

    def on_wa_state(self, state: str) -> None:
        prev, self.wa_state = self.wa_state, state
        log.info("wa state %s -> %s", prev, state)
        if state == "CONNECTED":
            self.wa_down_since = None
        elif self.wa_down_since is None:
            self.wa_down_since = self.clock.now()
        self._wa_alert()
        if state == "CONNECTED" and prev != "CONNECTED":
            if self.store.get_setting("linked_at") is None:
                self.store.set_setting("linked_at", self.clock.now())
            req = self._next_req()
            self._wa_reqs[req] = ("contacts",)
            self.wa.get_contacts(req)
        if state != "CONNECTED":
            for what in self._wa_reqs.values():
                if what[0] == "send":
                    ob = self.store.outbound(what[1])
                    self.store.update_outbound(ob.id, state="unknown",
                                               last_error="WA dropped mid-send")
                    self.say(f"✗ {first_name(ob.chat_name)}: WA dropped, check phone")
                elif what[0] == "seen":
                    self.pending_seen.add(what[1])
            self._wa_reqs.clear()

    def on_wa_result(self, req: int, ok: bool, error: str | None = None,
                     data: object = None) -> None:
        what = self._wa_reqs.pop(req, None)
        if what is None:
            return
        if what[0] == "send":
            ob = self.store.outbound(what[1])
            name = first_name(ob.chat_name)
            if ob.id in self._sos_outbound:
                name = "SOS " + name
            if ok:
                self.store.update_outbound(ob.id, state="sent", sent_at=self.clock.now())
                self.say(f"✓ {name}")
            else:
                self.store.update_outbound(ob.id, state="failed", last_error=error)
                self.say(f"✗ {name}: {error or 'failed'}")
        elif what[0] == "seen" and not ok:
            log.warning("markSeen failed for %s: %s", what[1], error)
        elif what[0] == "contacts" and ok and isinstance(data, list):
            self.on_wa_contacts(data)

    def on_wa_contacts(self, contacts: list[dict]) -> None:
        self.store.upsert_contacts(contacts, self.clock.now())

    def _pump_wa(self) -> None:
        now = self.clock.now()
        if self.wa_state != "CONNECTED" or now < self.wa_next_at:
            return
        if any(w[0] == "send" for w in self._wa_reqs.values()):
            return  # one send in flight at a time
        queued = self.store.outbound_queued()
        if queued:
            ob = queued[0]
            req = self._next_req()
            self._wa_reqs[req] = ("send", ob.id)
            self.store.update_outbound(ob.id, state="sending", attempts=ob.attempts + 1)
            self._touched[ob.chat_id] = now
            self.wa.send(req, ob.chat_id, ob.text)
        elif self.pending_seen:
            chat_id = self.pending_seen.pop()
            req = self._next_req()
            self._wa_reqs[req] = ("seen", chat_id)
            self._touched[chat_id] = now
            self.wa.mark_seen(req, chat_id)
        else:
            return
        self.wa_next_at = now + self.cfg.whatsapp.min_send_interval_s

    # =====================================================================
    # timers
    # =====================================================================

    def on_wa_chat_read(self, chat_id: str) -> None:
        """The owner read this chat on their phone: drop what's still waiting for the mesh.

        Messages already on air are left alone. Unread going to 0 because the
        bridge itself replied or marked seen there is not the owner reading."""
        if self.clock.now() - self._touched.get(chat_id, float("-inf")) < OWN_READ_GRACE_S:
            return
        self._phone_active("read")
        n = self.store.drop_queued_for_chat(chat_id)
        if n:
            log.info("read on phone: %d queued from %s not relayed", n, chat_id)

    def wa_alerts(self) -> bool:
        return bool(self.store.get_setting("wa_alerts", True))

    def _wa_alert(self) -> None:
        """One mesh DM when WhatsApp is unlinked or down 10 min, one when it's back.

        Remembered in settings so a core restart doesn't repeat it."""
        alerted = self.store.get_setting("wa_alerted")
        if self.wa_state == "CONNECTED":
            if alerted:
                self.store.set_setting("wa_alerted", None)
                self.say("✓ WhatsApp back, replies flowing")
            return
        if alerted or not self.wa_alerts():
            return
        if self.wa_state in ("QR", "AUTH_FAILURE") and self.store.get_setting("linked_at"):
            self.store.set_setting("wa_alerted", "unlinked")
            self.say("⚠ WhatsApp unlinked: replies held. Relink on the NAS admin page")
        elif (self.wa_down_since is not None
              and self.clock.now() - self.wa_down_since >= WA_DOWN_ALERT_S):
            self.store.set_setting("wa_alerted", "offline")
            self.say("⚠ WhatsApp offline 10 min: replies held")

    def tick(self) -> None:
        self._wa_alert()
        paused = self.paused() is not None
        if (self._was_paused and not paused and self.reachable()
                and not self.held and not self._flushing()):
            log.info("relay pause over")
            self.flush()
        self._was_paused = paused
        self._schedule_queue()
        self.outbox.tick()
        self._pump_wa()

    def _schedule_queue(self) -> None:
        now = self.clock.now()
        if self.held or self._flushing() or not self.store.queued() or self.paused():
            return
        interval = self.cfg.mesh.probe_interval_s
        if self.reachable():
            # Reachable but things got parked (one-way link?): try again, gently.
            if self.last_flush_at is None or now - self.last_flush_at >= interval:
                self.flush()
        elif (self.last_probe_at is None or now - self.last_probe_at >= interval) \
                and not self.outbox.active(Prio.PROBE):
            self.last_probe_at = now
            self.outbox.enqueue(self.digest(self.store.queued()), Prio.PROBE,
                                Probe(), retries=False)

    def health(self) -> dict:
        now = self.clock.now()
        counts = self.store.counts()
        heard = None if self.last_heard is None else round(now - self.last_heard)
        return {
            "ok": self.wa_state == "CONNECTED" and self.mesh_connected,
            "wa": self.wa_state,
            "mesh_connected": self.mesh_connected,
            "pocket_reachable": self.reachable(),
            "pocket_last_heard_s": heard,
            "queue": counts.get("queued", 0),
            "messages": counts,
            "outbox": len(self.outbox.active()),
            "wa_outbound_queued": len(self.store.outbound_queued()),
            "last_ack_rtt_s": self.outbox.last_rtt,
            "held": self.held,
            "paused": self.paused(),
            "uptime_s": round(now - self.started_at),
        }


def clean_sos(text: str) -> str:
    """Drop the bell (and other control chars) the SOS node adds for phone alerts."""
    return "".join(ch for ch in text if ch == "\n" or ch >= " ").strip()


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:g}"


def _dur(s: float) -> str:
    s = int(s)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    return f"{m}m"
