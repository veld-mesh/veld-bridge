import itertools

import pytest

from veldbridge.bridge import Bridge
from veldbridge.config import from_dict
from veldbridge.db import Store
from veldbridge.transports import FakeClock, FakeMesh, FakeWhatsApp

POCKET = 0xA1B2C3D4


class Harness:
    """A bridge wired to fakes, plus helpers that read like the field."""

    def __init__(self, **cfg):
        base = {"mesh": {"pocket_node": "!a1b2c3d4"}}
        for k, v in cfg.items():
            base.setdefault(k, {}).update(v)
        self.cfg = from_dict(base)
        self.clock = FakeClock()
        self.mesh = FakeMesh()
        self.wa = FakeWhatsApp()
        self.store = Store()
        self.bridge = Bridge(self.cfg, self.store, self.mesh, self.wa, self.clock)
        self._wa_ids = itertools.count(1)
        self._pkt_ids = itertools.count(5000)

    # WhatsApp side
    def wa_msg(self, name="Sam Smith", chat="27820000001@c.us", body="hi", **kw):
        m = {"id": f"wa{next(self._wa_ids)}", "chatId": chat, "senderName": name,
             "body": body, "kind": "text", **kw}
        return self.bridge.on_wa_message(m)

    def wa_up(self):
        self.bridge.on_wa_state("CONNECTED")
        for c in self.wa.of("getContacts"):
            self.bridge.on_wa_result(c[1], True, data=[])

    # mesh side
    def hear(self):
        """Any packet from the pocket node (telemetry, position...)."""
        self.bridge.on_mesh_packet(from_num=POCKET, packet_id=next(self._pkt_ids),
                                   is_dm=False, text=None)

    def dm(self, text, rssi=-97.0, snr=6.5):
        self.bridge.on_mesh_packet(from_num=POCKET, packet_id=next(self._pkt_ids),
                                   is_dm=True, text=text, rssi=rssi, snr=snr)

    def ack_last(self, reason="NONE", from_num=POCKET):
        pid, _ = self.mesh.last()
        self.bridge.on_mesh_ack(pid, from_num, reason)

    def run(self, seconds, step=1.0):
        """Advance time, ticking like the real 1 Hz loop."""
        t = 0.0
        while t < seconds:
            self.clock.advance(step)
            t += step
            self.bridge.tick()

    def air(self):
        return self.mesh.texts

    def drain(self, max_s=600):
        """Tick and ack everything sent until the outbox is empty."""
        seen = len(self.mesh.sent)
        t = 0
        while self.bridge.outbox.active() and t < max_s:
            self.clock.advance(1)
            t += 1
            self.bridge.tick()
            while seen < len(self.mesh.sent):
                pid, _ = self.mesh.sent[seen]
                self.bridge.on_mesh_ack(pid, POCKET, "NONE")
                seen += 1


@pytest.fixture
def h():
    return Harness()
