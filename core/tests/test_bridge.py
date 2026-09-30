from conftest import POCKET, Harness

from veldbridge.text import blen

SAM = "27820000001@c.us"
SAMANTHA = "27820000002@c.us"
JO = "27820000003@c.us"
GROUP = "120363000000000001@g.us"


# ---------------------------------------------------------------- filtering


def test_relays_1to1_but_not_groups_status_or_channels(h):
    h.hear()
    assert h.wa_msg(chat=SAM) is not None
    assert h.wa_msg(chat=GROUP, isGroup=True, chatName="Farm") is None
    assert h.wa_msg(chat="status@broadcast") is None
    assert h.wa_msg(chat="123@newsletter") is None
    assert h.wa_msg(chat=JO, fromMe=True) is None


def test_allowlisted_group_is_relayed_with_group_tag():
    h = Harness(relay={"allowlist": [{"chat_id": GROUP, "name": "Farm"}]})
    h.hear()
    h.wa_msg(name="Sam Smith", chat=GROUP, isGroup=True, chatName="Farm Crew", body="gate open")
    h.drain()
    assert h.air() == ["#1 Sam/Farm: gate open"]


def test_all_off_means_allowlist_only(h):
    h.hear()
    h.dm("all off")
    assert h.wa_msg(chat=SAM) is None
    h.dm("all on")
    assert h.wa_msg(chat=SAM) is not None


# ---------------------------------------------------------------- live delivery


def test_live_message_delivered_and_marked_seen_on_ack(h):
    h.wa_up()
    h.hear()
    h.wa_msg(body="lunch 👍 at  1?")
    assert h.wa.of("markSeen") == []           # never on arrival
    h.bridge.tick()
    assert h.air() == ["#1 Sam Smith: lunch at 1?"]
    assert h.wa.of("markSeen") == []           # not before the mesh ack
    h.ack_last()
    h.run(3)
    assert [c[2] for c in h.wa.of("markSeen")] == [SAM]
    assert h.store.message(1).state == "acked"


def test_mark_seen_read_mode_waits_for_r():
    h = Harness(relay={"mark_seen": "read"})
    h.wa_up()
    h.hear()
    h.wa_msg()
    h.drain()
    h.run(5)
    assert h.wa.of("markSeen") == []
    h.dm("r 1")
    h.drain()
    h.run(5)
    assert [c[2] for c in h.wa.of("markSeen")] == [SAM]
    assert h.store.message(1).state == "read"


def test_mark_seen_never_leaves_chats_unread():
    h = Harness(relay={"mark_seen": "never"})
    h.wa_up()
    h.hear()
    h.wa_msg()
    h.bridge.tick()
    h.ack_last()
    h.run(5)
    h.dm("r 1")
    h.drain()
    h.run(5)
    assert h.wa.of("markSeen") == []
    assert h.store.message(1).state == "read"


# ---------------------------------------------------------------- replayed history


def test_messages_sent_before_first_link_are_skipped(h):
    h.wa_up()
    h.hear()
    now = h.clock.now()
    assert h.wa_msg(timestamp=now - 86400) is None     # old unread, replayed on link
    assert h.wa_msg(timestamp=now + 2) is not None


def test_offline_gap_after_link_is_still_relayed(h):
    h.wa_up()
    h.hear()
    h.clock.advance(3600)
    h.bridge.on_wa_state("DISCONNECTED")
    sent_while_down = h.clock.now()
    h.clock.advance(600)
    h.wa_up()                                          # reconnect keeps the first link time
    assert h.wa_msg(timestamp=sent_while_down) is not None


def test_messages_older_than_max_age_are_skipped():
    h = Harness(relay={"max_age_s": 3600})
    h.wa_up()
    h.hear()
    h.clock.advance(7200)
    now = h.clock.now()
    assert h.wa_msg(timestamp=now - 3601) is None
    assert h.wa_msg(timestamp=now - 60) is not None


def test_message_without_timestamp_is_relayed(h):
    h.wa_up()
    h.hear()
    assert h.wa_msg() is not None


def test_media_labels_on_air(h):
    h.hear()
    h.wa_msg(body="", kind="voice", duration=42)
    h.wa_msg(body="the new calf", kind="photo")
    h.wa_msg(body="", kind="location", lat=-30.123, lon=25.456)
    h.drain()
    assert h.air() == [
        "#1 Sam Smith: [voice 0:42]",
        "#2 Sam Smith: [photo] the new calf",
        "#3 Sam Smith: [loc] -30.12300,25.45600",
    ]


def test_long_message_truncated_on_air_full_on_r(h):
    h.hear()
    long = "word " * 120
    h.wa_msg(body=long)
    h.drain()
    assert h.air()[0].endswith("…")
    assert blen(h.air()[0]) <= 200
    h.dm("r 1")
    h.drain()
    parts = h.air()[1:]
    assert len(parts) >= 3
    assert parts[0].startswith(f"[1/{len(parts)}] #1 Sam Smith: word")
    assert all(blen(p) <= 200 for p in parts)


def test_unacked_message_parks_back_in_queue(h):
    h.hear()
    h.wa_msg()
    h.run(60 + 60 + 60 + 180 + 60 + 600 + 60 + 5)   # every attempt times out
    assert len(h.air()) >= 4
    assert h.store.message(1).state == "queued"


# ---------------------------------------------------------------- presence, probe, digest


def test_unreachable_queues_and_probes_every_5_min(h):
    h.wa_msg(chat=SAM, body="a")
    h.wa_msg(chat=SAM, body="b")
    h.wa_msg(chat=JO, name="Jo", body="c")
    h.run(1)
    assert h.air() == ["3 waiting: Sam×2, Jo — r to read"]   # the probe is the digest
    h.run(299)
    assert len(h.air()) == 1                                # at most every 5 min
    h.run(5)
    assert len(h.air()) == 2


def test_probe_ack_triggers_flush_without_repeating_digest(h):
    for i in range(3):
        h.wa_msg(body=f"m{i}")
    h.run(1)
    h.ack_last()          # pocket node acked the probe -> reachable
    h.drain()
    assert h.air() == [
        "3 waiting: Sam×3 — r to read",
        "#1 Sam Smith: m0",
        "#2 Sam Smith: m1",
        "#3 Sam Smith: m2",
    ]


def test_becoming_reachable_sends_digest_then_capped_flush(h):
    for i in range(13):
        h.wa_msg(name="Sam" if i % 2 else "Alex", chat=SAM if i % 2 else JO, body=f"m{i}")
    h.hear()
    h.drain()
    air = h.air()
    assert air[0] == "13 waiting: Alex×7, Sam×6 — r to read"
    assert air[1:11] == [f"#{i + 1} {'Sam' if i % 2 else 'Alex'}: m{i}" for i in range(10)]
    assert air[11] == "+3 more, r for next"
    assert h.bridge.held
    # new message while held: stays queued, not pushed
    h.wa_msg(chat=SAM, name="Sam", body="late")
    h.drain()
    assert len(h.air()) == 12
    h.dm("l")
    h.drain()
    assert h.air()[-1] == "4 waiting: #11 Alex, #12 Sam, #13 Alex, #14 Sam"
    for _ in range(4):
        h.dm("r")
        h.drain()
    assert h.air()[-4:] == ["#11 Alex: m10", "#12 Sam: m11", "#13 Alex: m12", "#14 Sam: late"]
    assert not h.bridge.held
    h.dm("r")
    h.drain()
    assert h.air()[-1] == "0 waiting"


def test_airtime_never_more_than_one_packet_per_10s():
    h = Harness()
    times = []
    real = h.mesh.send_dm

    def spy(text):
        times.append(h.clock.now())
        return real(text)

    h.mesh.send_dm = spy
    for i in range(15):
        h.wa_msg(body=f"m{i}")
    h.hear()
    h.dm("s")
    h.dm("ping")
    h.dm("l")
    h.drain(max_s=2000)
    assert len(times) > 10
    assert min(b - a for a, b in zip(times, times[1:], strict=False)) >= 10


# ---------------------------------------------------------------- replies


def _two_sams(h):
    h.hear()
    h.wa_msg(name="Sam Smith", chat=SAM, body="hi")
    h.wa_msg(name="Samantha K", chat=SAMANTHA, body="hey")
    h.drain()


def test_reply_by_short_id_goes_to_exact_chat_with_confirmation(h):
    h.wa_up()
    _two_sams(h)
    h.dm("@2 on my way")
    h.run(1)
    send = h.wa.of("send")[-1]
    assert send[2:] == (SAMANTHA, "on my way")
    h.bridge.on_wa_result(send[1], True)
    h.drain()
    assert h.air()[-1] == "✓ Samantha"


def test_ambiguous_name_asks(h):
    _two_sams(h)
    h.dm("@sam hello")
    h.drain()
    assert h.air()[-1] == "? sam: Sam Smith, Samantha K"
    assert h.store.outbound_queued() == []


def test_unambiguous_name_and_bare_text_follow_up(h):
    h.wa_up()
    _two_sams(h)
    h.dm("@smith ok")
    h.dm("see you at 5")
    h.run(1)
    h.bridge.on_wa_result(h.wa.of("send")[-1][1], True)
    h.run(5)
    assert [c[2:] for c in h.wa.of("send")] == [(SAM, "ok"), (SAM, "see you at 5")]


def test_min_3s_between_wa_sends(h):
    h.wa_up()
    _two_sams(h)
    h.dm("@1 a")
    h.dm("b")
    h.run(1)
    first = h.wa.of("send")
    assert len(first) == 1
    h.bridge.on_wa_result(first[0][1], True)
    h.run(1)
    assert len(h.wa.of("send")) == 1
    h.run(2)
    assert len(h.wa.of("send")) == 2


def test_bare_text_without_chat(h):
    h.hear()
    h.dm("hello?")
    h.drain()
    assert h.air() == ["? no chat selected — @name text"]


def test_r_sets_chat_for_bare_reply(h):
    h.wa_up()
    _two_sams(h)
    h.dm("r samantha")
    h.dm("yes")
    h.run(5)
    assert h.wa.of("send")[-1][2:] == (SAMANTHA, "yes")


def test_never_opens_a_new_chat(h):
    h.store.upsert_contact("27829999999@c.us", "Kim", None, False, 0)  # in contacts only
    h.hear()
    h.dm("@kim hi")
    h.drain()
    assert h.air() == ["? kim: no match"]
    assert h.store.outbound_queued() == []


def test_wa_offline_holds_reply_then_flushes(h):
    _two_sams(h)
    h.dm("@1 later")
    h.drain()
    assert h.air()[-1] == "⚠ WA offline, queued"
    assert h.wa.of("send") == []
    h.wa_up()
    h.run(1)
    assert h.wa.of("send")[-1][2:] == (SAM, "later")


def test_failed_send_reports_reason(h):
    h.wa_up()
    _two_sams(h)
    h.dm("@1 hi")
    h.run(1)
    h.bridge.on_wa_result(h.wa.of("send")[-1][1], False, "chat not found")
    h.drain()
    assert h.air()[-1] == "✗ Sam: chat not found"


def test_disconnect_mid_send_is_not_resent(h):
    h.wa_up()
    _two_sams(h)
    h.dm("@1 hi")
    h.run(1)
    h.bridge.on_wa_state("DISCONNECTED")
    h.wa_up()
    h.run(10)
    assert len(h.wa.of("send")) == 1
    assert h.store.outbound(1).state == "unknown"
    h.drain()
    assert "✗ Sam: WA dropped, check phone" in h.air()


def test_duplicate_mesh_packet_sends_once(h):
    h.wa_up()
    _two_sams(h)
    h.bridge.on_mesh_packet(from_num=POCKET, packet_id=77, is_dm=True, text="@1 once")
    h.bridge.on_mesh_packet(from_num=POCKET, packet_id=77, is_dm=True, text="@1 once")
    h.run(10)
    assert len(h.wa.of("send")) == 1


def test_signature_appended_when_configured():
    h = Harness(whatsapp={"signature": " -L"})
    h.wa_up()
    h.hear()
    h.wa_msg()
    h.dm("@1 ok")
    h.run(1)
    assert h.wa.of("send")[-1][3] == "ok -L"


def test_dms_from_other_nodes_are_ignored(h):
    h.wa_up()
    _two_sams(h)
    h.bridge.on_mesh_packet(from_num=0x12345678, packet_id=1, is_dm=True, text="@1 pwned")
    h.run(10)
    assert h.wa.of("send") == []


def test_channel_text_from_pocket_is_not_a_command(h):
    h.wa_up()
    _two_sams(h)
    h.bridge.on_mesh_packet(from_num=POCKET, packet_id=1, is_dm=False, text="@1 hi all")
    h.run(10)
    assert h.wa.of("send") == []


# ---------------------------------------------------------------- misc commands


def test_mute_and_unmute(h):
    _two_sams(h)
    h.dm("mute samantha")
    h.drain()
    assert h.air()[-1] == "muted Samantha K"
    assert h.wa_msg(chat=SAMANTHA, name="Samantha K") is None
    h.dm("unmute samantha")
    h.drain()
    assert h.wa_msg(chat=SAMANTHA, name="Samantha K") is not None


def test_status_and_ping(h):
    h.wa_up()
    h.hear()
    h.dm("ping", rssi=-101, snr=-3.25)
    h.drain()
    assert h.air()[-1] == "pong rssi -101 snr -3.25"
    h.dm("s")
    h.drain()
    assert h.air()[-1].startswith("WA ok · q 0 · rtt ")


def test_health_payload(h):
    h.wa_up()
    h.bridge.on_mesh_state(True)
    health = h.bridge.health()
    assert health["ok"] is True
    assert health["queue"] == 0


# ---------------------------------------------------------------- WhatsApp alerts


def _alerts(h):
    return [t for t in h.air() if "WhatsApp" in t]


def _live(h, seconds):
    """Run with the pocket node acking every packet, like a node in range."""
    for _ in range(seconds):
        n = len(h.mesh.sent)
        h.run(1)
        for pid, _ in h.mesh.sent[n:]:
            h.bridge.on_mesh_ack(pid, POCKET, "NONE")


def test_unlinked_alerts_once_then_says_back(h):
    h.wa_up()
    h.hear()
    for _ in range(3):                       # WA re-emits QR every ~20 s
        h.bridge.on_wa_state("QR")
        _live(h, 30)
    h.wa_up()
    _live(h, 30)
    assert _alerts(h) == ["⚠ WhatsApp unlinked: replies held. Relink on the NAS admin page",
                          "✓ WhatsApp back, replies flowing"]


def test_first_link_and_short_restart_do_not_alert(h):
    h.hear()
    h.bridge.on_wa_state("QR")               # never linked yet: the first QR is expected
    _live(h, 60)
    h.wa_up()
    h.bridge.on_wa_state("DISCONNECTED")     # container restart
    _live(h, 120)
    h.wa_up()
    _live(h, 30)
    assert _alerts(h) == []


def test_offline_ten_minutes_alerts(h):
    h.wa_up()
    h.hear()
    h.bridge.on_wa_state("DISCONNECTED")
    _live(h, 590)
    assert _alerts(h) == []
    _live(h, 20)
    _live(h, 30)
    assert _alerts(h) == ["⚠ WhatsApp offline 10 min: replies held"]


def test_alerts_can_be_switched_off(h):
    h.store.set_setting("wa_alerts", False)
    h.wa_up()
    h.hear()
    h.bridge.on_wa_state("QR")
    _live(h, 700)
    h.wa_up()
    _live(h, 30)
    assert _alerts(h) == []


# ---------------------------------------------------------------- read on the phone


def test_read_on_phone_drops_queued_before_node_is_back():
    h = Harness(relay={"phone_pause_s": 0})   # the pause itself: test_pause.py
    h.wa_up()                                  # node away: messages queue
    h.wa_msg(chat=SAM, body="one")
    h.wa_msg(chat=SAM, body="two")
    h.wa_msg(name="Jo", chat=JO, body="three")
    h.bridge.on_wa_chat_read(SAM)              # owner read Sam's chat on wifi
    h.hear()                                   # node back in range
    h.drain()
    assert not any("Sam" in t for t in h.air())
    assert any(t.startswith("#3 Jo: three") for t in h.air())
    assert [h.store.message(i).state for i in (1, 2)] == ["read", "read"]


def test_read_on_phone_leaves_messages_already_on_air(h):
    h.wa_up()
    h.hear()
    h.wa_msg(chat=SAM)
    h.bridge.tick()                            # #1 sent, not yet acked
    h.bridge.on_wa_chat_read(SAM)
    assert h.store.message(1).state == "sent"


def test_bridges_own_mark_seen_is_not_owner_reading():
    h = Harness(relay={"mark_seen": "ack"})
    h.wa_up()
    h.wa_msg(chat=SAM, body="waiting")        # node away: queued
    h.bridge._mark_seen(h.store.message(1))   # e.g. an earlier Sam message was acked
    h.bridge.tick()                           # bridge sends markSeen to WA
    assert h.wa.of("markSeen")
    h.bridge.on_wa_chat_read(SAM)             # unread->0 caused by us, not the owner
    assert h.store.message(1).state == "queued"
    h.clock.advance(31)
    h.bridge.on_wa_chat_read(SAM)             # later: the owner really read it
    assert h.store.message(1).state == "read"


def test_favourite_and_lid_chat_for_same_person_is_not_ambiguous():
    thabo_pn, thabo_lid = "27820000011@c.us", "106348238872782@lid"
    h = Harness(relay={"allowlist": [{"chat_id": thabo_pn, "name": "Thabo Mokoena"}]})
    h.wa_up()
    h.hear()
    assert h.bridge.resolve("thabo") == (thabo_pn, "Thabo Mokoena")   # before he writes
    h.wa_msg(name="Thabo Mokoena", chat=thabo_lid, body="hi")         # replies arrive as @lid
    assert h.bridge.resolve("thabo") == (thabo_lid, "Thabo Mokoena")
    assert not any(t.startswith("? thabo") for t in h.air())


# ---------------------------------------------------------------- @c.us / @lid


THABO_PN, THABO_LID = "27830000009@c.us", "123456789012345@lid"


def test_favourite_saved_under_phone_id_still_relays_replies_from_lid(h):
    h.bridge.set_allowed(THABO_PN, "Thabo Mokoena", True)
    h.dm("all off")
    h.hear()
    assert h.wa_msg(name="Thabo Mokoena", chat=THABO_LID, body="got it") is None  # no alias
    msg = h.wa_msg(name="Thabo Mokoena", chat=THABO_LID, body="got it", aliases=[THABO_PN])
    assert msg is not None
    h.drain()
    assert h.air()[-1] == "#1 Thabo Mokoena: got it"


def test_mute_saved_under_one_id_covers_the_other(h):
    h.hear()
    h.bridge.set_muted(THABO_PN, True)
    assert h.wa_msg(name="Thabo", chat=THABO_LID, body="x", aliases=[THABO_PN]) is None
