"""Hold the mesh relay while the owner has WhatsApp on their phone, and `pause` / `resume`."""

from conftest import Harness
from test_bridge import JO, SAM

from veldbridge import commands as C


def live():
    """WhatsApp up, pocket node in range: new messages would go straight out."""
    h = Harness()
    h.wa_up()
    h.hear()
    return h


def relayed(h):
    return [t for t in h.air() if t.startswith("#")]


def test_parse_pause_and_resume():
    assert C.parse("pause") == C.Pause(on=True)
    assert C.parse("Resume") == C.Pause(on=False)
    assert isinstance(C.parse("pause now"), C.Reply)


def test_sending_from_phone_holds_new_messages():
    h = live()
    h.bridge.on_wa_own(JO, timestamp=h.clock.now())
    h.wa_msg(chat=SAM, body="hi there")
    h.drain(60)
    assert relayed(h) == []
    assert h.bridge.paused() == "phone"
    assert h.store.message(1).state == "queued"


def test_reading_on_phone_holds_new_messages():
    h = live()
    h.bridge.on_wa_chat_read(JO)
    h.wa_msg(chat=SAM, body="hi there")
    h.drain(60)
    assert relayed(h) == []


def test_phone_quiet_for_30_min_resumes_and_delivers_what_waited():
    h = live()
    h.bridge.on_wa_own(JO, timestamp=h.clock.now())
    h.wa_msg(chat=SAM, body="hi there")
    h.run(1700)
    h.hear()
    assert relayed(h) == []
    h.run(120)                           # 30 min since the phone was used
    h.hear()
    h.drain(120)
    assert any(t.startswith("#1 Sam") for t in relayed(h))
    assert h.bridge.paused() is None


def test_each_phone_use_extends_the_pause():
    h = live()
    h.bridge.on_wa_own(JO, timestamp=h.clock.now())
    h.run(1500)
    h.bridge.on_wa_chat_read(SAM)
    h.run(1500)
    assert h.bridge.paused() == "phone"


def test_read_on_phone_while_paused_still_drops_that_chat():
    h = live()
    h.bridge.on_wa_own(JO, timestamp=h.clock.now())
    h.wa_msg(chat=SAM, body="seen it")
    h.wa_msg(name="Jo", chat=JO, body="not yet")
    h.run(60)
    h.bridge.on_wa_chat_read(SAM)
    h.run(1900)
    h.hear()
    h.drain(120)
    assert not any("Sam" in t for t in relayed(h))
    assert any("Jo: not yet" in t for t in relayed(h))


def test_bridges_own_sends_do_not_count_as_phone_use():
    h = live()
    h.wa_msg(chat=SAM, body="hi")
    h.drain(60)
    h.dm("@1 on my way")                 # owner replies from the field
    h.run(10)
    h.bridge.on_wa_own(SAM, timestamp=h.clock.now())   # WA echoes our own send
    assert h.bridge.paused() is None


def test_old_own_messages_from_history_sync_are_ignored():
    h = live()
    h.bridge.on_wa_own(JO, timestamp=h.clock.now() - 3600)
    assert h.bridge.paused() is None


def test_phone_pause_can_be_switched_off():
    h = live()
    h.store.set_setting("phone_pause", False)
    h.bridge.on_wa_own(JO, timestamp=h.clock.now())
    h.wa_msg(chat=SAM, body="hi")
    h.drain(60)
    assert any(t.startswith("#1 Sam") for t in relayed(h))


def test_pause_command_holds_until_resume():
    h = live()
    h.dm("pause")
    h.drain(60)
    assert any(t.startswith("⏸ paused") for t in h.air())
    h.wa_msg(chat=SAM, body="one")
    h.wa_msg(name="Jo", chat=JO, body="two")
    h.run(7200)
    h.hear()
    h.drain(60)
    assert relayed(h) == []
    h.dm("resume")
    h.drain(300)
    assert any(t.startswith("▶ resumed, 2 waiting") for t in h.air())
    assert any(t.startswith("#1 Sam Smith: one") for t in relayed(h))
    assert any("Jo: two" in t for t in relayed(h))


def test_resume_also_ends_a_phone_pause():
    h = live()
    h.bridge.on_wa_own(JO, timestamp=h.clock.now())
    h.dm("resume")
    assert h.bridge.paused() is None


def test_commands_still_work_while_paused():
    h = live()
    h.dm("pause")
    h.wa_msg(chat=SAM, body="read me")
    h.dm("r")
    h.drain(120)
    assert any("read me" in t for t in h.air())


def test_status_and_health_show_the_pause():
    h = live()
    h.dm("pause")
    assert h.bridge.health()["paused"] == "manual"
    assert h.bridge.status_line().endswith("paused (manual)")
