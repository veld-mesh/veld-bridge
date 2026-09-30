from pathlib import Path

import pytest

from veldbridge.config import ConfigError, from_dict, load, node_num
from veldbridge.db import Store

ROOT = Path(__file__).resolve().parents[2]


def test_example_config_loads_with_default_answers():
    cfg = load(ROOT / "config.example.yaml")
    assert cfg.relay.all is True
    assert cfg.relay.allowlist == ()
    assert cfg.whatsapp.signature is None
    assert cfg.transcription.backend == "none"
    assert cfg.mesh.byte_budget == 200
    assert cfg.mesh.retry_schedule_s == (60, 180, 600)


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="unknown"):
        from_dict({"mesh": {"bytes": 200}})


def test_airtime_and_budget_guards():
    with pytest.raises(ConfigError):
        from_dict({"mesh": {"byte_budget": 240}})
    with pytest.raises(ConfigError):
        from_dict({"mesh": {"min_packet_interval_s": 2}})
    with pytest.raises(ConfigError):
        from_dict({"whatsapp": {"min_send_interval_s": 1}})


def test_allowlist_entries():
    cfg = from_dict({"relay": {"allowlist": [{"chat_id": "123@g.us", "name": "Farm"}]}})
    assert cfg.relay.allowlist[0].name == "Farm"


def test_node_num():
    assert node_num("!a1b2c3d4") == 0xA1B2C3D4
    with pytest.raises(ConfigError):
        node_num("sam")


def _add(store, wa_id, day="2026-09-26", chat="1@c.us"):
    return store.add_message(wa_msg_id=wa_id, day=day, chat_id=chat, chat_name="Sam",
                             sender_name="Sam", text="hi", media_kind=None, media_label=None,
                             received_at=0.0)


def test_short_ids_roll_1_to_99_and_reset_daily():
    s = Store()
    ids = [_add(s, f"m{i}").short_id for i in range(100)]
    assert ids[:3] == [1, 2, 3]
    assert ids[98] == 99
    assert ids[99] == 1  # wrapped
    assert _add(s, "next-day", day="2026-09-27").short_id == 1
    # after a wrap, #1 means the newest one
    assert s.message_by_short(1).wa_msg_id == "next-day"


def test_duplicate_wa_message_is_ignored():
    s = Store()
    assert _add(s, "dup") is not None
    assert _add(s, "dup") is None


def test_recover_resets_in_flight_state():
    s = Store()
    m = _add(s, "a")
    s.set_message_state(m.id, "sent")
    ob = s.add_outbound(mesh_packet_id=1, chat_id="1@c.us", chat_name="Sam", text="x",
                        created_at=0)
    s.update_outbound(ob.id, state="sending")
    s.recover()
    assert s.message(m.id).state == "queued"
    assert s.outbound(ob.id).state == "unknown"  # never resend blindly


def test_settings_roundtrip():
    s = Store()
    assert s.get_setting("all", True) is True
    s.set_setting("muted", ["a", "b"])
    assert s.get_setting("muted") == ["a", "b"]


def test_upsert_contacts_is_one_transaction():
    s = Store()
    s.upsert_contacts([{"id": "1@c.us", "name": "Jan Botha"},
                       {"name": "no id, skipped"},
                       {"id": "2@c.us", "pushname": "Tiaan"}], when=1.0)
    assert s.contact_name("1@c.us") == "Jan Botha"
    assert s.contact_name("2@c.us") == "Tiaan"
    with pytest.raises(AttributeError):
        s.upsert_contacts([{"id": "3@c.us", "name": "Lisa"}, None], when=2.0)
    assert s.contact_name("3@c.us") is None          # rolled back as a whole


def test_sqlite_is_wal_with_normal_sync(tmp_path):
    s = Store(str(tmp_path / "b.db"))
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert s.conn.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL
