"""Mesh SOS button -> WhatsApp alerts."""

import json

import pytest
from conftest import POCKET, Harness
from test_admin import FAMILY, STRANGER, THABO, auth, post

from veldbridge.admin import Admin

BERT = 0x1A2B3C4D
GATE = 0x5E6F7A8B
SOS = ("🆘 SOS Bert! -30.00000,25.00000 "
       "https://maps.google.com/?q=-30.00000,25.00000\x07")
CANCEL = "✅ SOS Bert cancelled - OK now"


@pytest.fixture
def s():
    h = Harness()
    h.store.upsert_contacts([
        {"id": THABO, "name": "Thabo Mokoena"},
        {"id": FAMILY, "name": "Botha Family", "isGroup": True},
    ], when=1.0)
    h.bridge.set_sos_recipient(THABO, "Thabo Mokoena", True)
    h.bridge.set_sos_recipient(FAMILY, "Botha Family", True)
    h.wa_up()
    h._pkt = 9000
    return h


def channel(h, text, from_num=BERT, packet_id=None):
    h._pkt += 1
    h.bridge.on_mesh_packet(from_num=from_num, packet_id=packet_id or h._pkt,
                            is_dm=False, text=text)


def sent(h):
    """WhatsApp sends so far, as (chat_id, text), acking each so the next goes."""
    acked: set[int] = set()
    while True:
        h.run(10)
        new = [c for c in h.wa.of("send") if c[1] not in acked]
        if not new:
            return [(c[2], c[3]) for c in h.wa.of("send")]
        for c in new:
            acked.add(c[1])
            h.bridge.on_wa_result(c[1], True)


def test_sos_goes_to_every_recipient_with_the_map_link(s):
    channel(s, SOS)
    out = sent(s)
    assert {chat for chat, _ in out} == {THABO, FAMILY}
    text = out[0][1]
    assert text.startswith("🆘 *Mesh SOS alert*")
    assert "SOS Bert! -30.00000,25.00000" in text
    assert "https://maps.google.com/?q=-30.00000,25.00000" in text
    assert "\x07" not in text and "Automatic message" in text


def test_node_repeats_are_throttled_to_one_update_per_10_min(s):
    channel(s, SOS)
    assert len(sent(s)) == 2
    for _ in range(4):               # the node repeats every 2 min
        s.run(120)
        channel(s, SOS.replace("!", "! (repeat 2)"))
    assert len(sent(s)) == 2
    s.run(200)                       # now > 10 min since the first
    channel(s, SOS.replace("!", "! (repeat 7)"))
    assert len(sent(s)) == 4


def test_same_packet_heard_twice_alerts_once(s):
    channel(s, SOS, packet_id=4242)
    s.run(700)
    channel(s, SOS, packet_id=4242)
    assert len(sent(s)) == 2


def test_cancel_follows_an_alert_and_resets(s):
    channel(s, SOS)
    channel(s, CANCEL)
    out = sent(s)
    assert len(out) == 4
    assert out[-1][1].startswith("✅ *Mesh SOS cancelled*") and "cancelled - OK now" in out[-1][1]
    channel(s, SOS)                  # a new SOS right after a cancel alerts again
    assert len(sent(s)) == 6


def test_cancel_without_an_alert_is_ignored(s):
    channel(s, "✅ all good here")
    assert sent(s) == []


def test_each_node_is_tracked_separately(s):
    channel(s, SOS)
    channel(s, "🆘 SOS Gate! No GPS position yet\x07", from_num=GATE)
    assert len(sent(s)) == 4


def test_dm_starting_with_sos_is_not_an_alert(s):
    s.bridge.on_mesh_packet(from_num=BERT, packet_id=1, is_dm=True, text=SOS)
    assert sent(s) == []


def test_no_recipients_warns_owner_on_the_mesh(s):
    s.bridge.set_sos_recipient(THABO, "", False)
    s.bridge.set_sos_recipient(FAMILY, "", False)
    channel(s, SOS)
    s.run(30)
    assert any("no WhatsApp SOS recipients" in t for t in s.air())
    assert s.wa.of("send") == []


def test_pocket_node_sos_still_counts_and_confirmations_say_sos(s):
    channel(s, SOS, from_num=POCKET)
    sent(s)
    s.drain()
    assert any(t.startswith("✓ SOS Thabo") for t in s.air())


def test_wa_offline_queues_the_alert_and_says_so(s):
    s.bridge.on_wa_state("DISCONNECTED")
    channel(s, SOS)
    s.run(30)
    assert s.wa.of("send") == []
    assert any("SOS WhatsApp queued" in t for t in s.air())
    s.wa_up()
    assert len(sent(s)) == 2


def test_admin_sets_sos_recipients_from_contacts_only():
    h = Harness()
    h.store.upsert_contacts([{"id": THABO, "name": "Thabo Mokoena"}], when=1.0)
    a = Admin(h.bridge, "correct horse")
    status, st = post(a, "/api/sos", {"chat_id": THABO, "on": True})
    assert status == "200 OK" and [e["name"] for e in st["sos"]] == ["Thabo Mokoena"]
    status, err = post(a, "/api/sos", {"chat_id": STRANGER, "on": True})
    assert status.startswith("400") and "contacts" in err["error"]
    _, _, body = a.handle("GET", "/api/search?q=thabo", auth(), b"")
    assert json.loads(body)[0]["sos"] is True
    status, st = post(a, "/api/sos", {"chat_id": THABO, "on": False})
    assert st["sos"] == []
