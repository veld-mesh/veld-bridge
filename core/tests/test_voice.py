"""Voice notes: transcribed (and translated to English) locally before they go on air."""

import os

from conftest import Harness
from test_bridge import JO, SAM

from veldbridge.transcribe import NullTranscriber, _remove

VOICE_DIR = "/var/lib/veld-bridge/voice"


def live(**transcription):
    h = Harness(transcription={"backend": "whisper", **transcription})
    h.wa_up()
    h.hear()
    return h


def voice(h, chat=SAM, name="Sam Smith", duration=42, path=None, **kw):
    return h.wa_msg(name=name, chat=chat, body="", kind="voice", duration=duration,
                    audioPath=path or f"{VOICE_DIR}/v{len(h.transcriber.submitted)}.ogg", **kw)


def relayed(h):
    return [t for t in h.air() if t.startswith("#")]


def test_voice_note_waits_for_its_text_then_goes_out_translated():
    h = live()
    msg = voice(h)
    h.drain(30)
    assert relayed(h) == []                       # held back while transcribing
    assert h.transcriber.submitted == [(msg.id, f"{VOICE_DIR}/v0.ogg")]
    h.bridge.on_transcript(msg.id, "The cows are out at the bottom gate", "af")
    h.drain(30)
    assert relayed(h) == ["#1 Sam Smith: [voice 0:42 af] The cows are out at the bottom gate"]


def test_english_voice_note_has_no_language_tag():
    h = live()
    msg = voice(h)
    h.bridge.on_transcript(msg.id, "Lunch is ready", "en")
    h.drain(30)
    assert relayed(h) == ["#1 Sam Smith: [voice 0:42] Lunch is ready"]


def test_dutch_detection_is_labelled_afrikaans():
    h = live()
    msg = voice(h)
    h.bridge.on_transcript(msg.id, "Come and eat", "nl")
    h.drain(30)
    assert relayed(h)[0].startswith("#1 Sam Smith: [voice 0:42 af]")


def test_failed_transcription_still_relays_the_voice_note():
    h = live()
    msg = voice(h)
    h.bridge.on_transcript(msg.id, None, None, "decode error")
    h.drain(30)
    assert relayed(h) == ["#1 Sam Smith: [voice 0:42]"]


def test_slow_transcription_times_out_and_late_text_is_ignored():
    h = live(timeout_s=120)
    msg = voice(h)
    h.run(119)
    assert relayed(h) == []
    h.run(2)                                      # timeout passes
    h.drain(30)
    assert relayed(h) == ["#1 Sam Smith: [voice 0:42]"]
    h.bridge.on_transcript(msg.id, "too late", "en")
    h.drain(30)
    assert relayed(h) == ["#1 Sam Smith: [voice 0:42]"]


def test_other_messages_are_not_held_up_by_a_voice_note():
    h = live()
    voice(h)
    h.wa_msg(name="Jo", chat=JO, body="text goes straight out")
    h.drain(30)
    assert relayed(h) == ["#2 Jo: text goes straight out"]


def test_voice_note_queued_while_away_is_counted_once_text_is_back():
    h = Harness(transcription={"backend": "whisper"})
    h.wa_up()                                     # node away
    msg = voice(h)
    assert h.bridge._queued() == []               # not offered while transcribing
    h.bridge.on_transcript(msg.id, "Gate is locked", "af")
    assert [m.id for m in h.bridge._queued()] == [msg.id]
    h.hear()
    h.drain(60)
    assert any("[voice 0:42 af] Gate is locked" in t for t in relayed(h))


def test_read_on_phone_while_transcribing_drops_it():
    h = live()
    msg = voice(h)
    h.run(40)                                     # outside the bridge's own-read grace
    h.bridge.on_wa_chat_read(SAM)
    h.bridge.on_transcript(msg.id, "already heard it", "en")
    h.drain(30)
    assert relayed(h) == []


def test_r_reads_the_full_transcript():
    h = live()
    msg = voice(h)
    long_text = "We need diesel for the tractor. " * 12
    h.bridge.on_transcript(msg.id, long_text.strip(), "af")
    h.drain(60)
    h.dm("r 1")
    h.drain(300)
    parts = [t for t in h.air() if t.startswith("[")]
    assert len(parts) >= 2 and "diesel" in parts[-1]


def test_transcription_off_relays_label_and_discards_audio():
    h = Harness()                                 # backend: none
    h.wa_up()
    h.hear()
    voice(h, path=f"{VOICE_DIR}/x.ogg")
    h.drain(30)
    assert relayed(h) == ["#1 Sam Smith: [voice 0:42]"]
    assert h.transcriber.submitted == [] and h.transcriber.discarded == [f"{VOICE_DIR}/x.ogg"]


def test_too_long_voice_note_is_not_transcribed():
    h = live(max_duration_s=300)
    voice(h, duration=301, path=f"{VOICE_DIR}/long.ogg")
    h.drain(30)
    assert relayed(h) == ["#1 Sam Smith: [voice 5:01]"]
    assert h.transcriber.discarded == [f"{VOICE_DIR}/long.ogg"]


def test_unwanted_voice_note_audio_is_discarded():
    h = live()
    h.bridge.set_muted(SAM, True)
    voice(h, path=f"{VOICE_DIR}/muted.ogg")
    assert h.transcriber.submitted == [] and h.transcriber.discarded == [f"{VOICE_DIR}/muted.ogg"]


def test_files_are_only_ever_deleted_inside_the_audio_dir(tmp_path):
    inside = tmp_path / "voice"
    inside.mkdir()
    keep = tmp_path / "keep.ogg"
    keep.write_bytes(b"x")
    victim = inside / "a.ogg"
    victim.write_bytes(b"x")
    t = NullTranscriber(str(inside))
    t.discard(str(keep))
    t.discard(str(inside / ".." / "keep.ogg"))
    assert keep.exists()
    t.submit(1, str(victim))
    assert not victim.exists()
    _remove(None, str(inside))
    assert os.path.isdir(inside)


def test_config_accepts_whisper_and_rejects_others():
    import pytest

    from veldbridge.config import ConfigError, from_dict

    assert from_dict({"mesh": {"pocket_node": "!a1b2c3d4"},
                      "transcription": {"backend": "whisper"}}).transcription.model == "small"
    with pytest.raises(ConfigError):
        from_dict({"mesh": {"pocket_node": "!a1b2c3d4"}, "transcription": {"backend": "cloud"}})
