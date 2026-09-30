import pytest

from veldbridge import commands as C


@pytest.mark.parametrize(
    "raw,want",
    [
        ("r", C.Read()),
        ("R", C.Read()),
        ("r 4", C.Read(short_id=4)),
        ("r #4", C.Read(short_id=4)),
        ("r sam", C.Read(name="sam")),
        ("@4 on my way", C.Reply(text="on my way", short_id=4)),
        ("@sam  ok  thanks ", C.Reply(text="ok  thanks", name="sam")),
        ("@#12 yes", C.Reply(text="yes", short_id=12)),
        ("@thabo. Using meshtastic", C.Reply(text="Using meshtastic", name="thabo")),
        ("@4, ok", C.Reply(text="ok", short_id=4)),
        ("see you soon", C.Reply(text="see you soon")),
        ("l", C.ListWaiting()),
        ("L", C.ListWaiting()),
        ("mute sam", C.Mute(name="sam", on=True)),
        ("unmute sam", C.Mute(name="sam", on=False)),
        ("all on", C.AllMode(on=True)),
        ("All Off", C.AllMode(on=False)),
        ("s", C.Status()),
        ("ping", C.Ping()),
        ("Ping", C.Ping()),
    ],
)
def test_parse(raw, want):
    assert C.parse(raw) == want


def test_empty_is_none():
    assert C.parse("   ") is None


def test_at_without_text_is_invalid():
    assert isinstance(C.parse("@sam"), C.Invalid)


@pytest.mark.parametrize("raw", ["r u there", "s ok", "l8r", "all good", "ping me later",
                                 "mute the tv now"])
def test_things_that_look_like_commands_are_replies(raw):
    assert C.parse(raw) == C.Reply(text=raw)
