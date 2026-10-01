"""SQLite store. Single-threaded: only the core event loop touches it."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from importlib import resources
from typing import Any


@dataclass
class Message:
    id: int
    wa_msg_id: str
    short_id: int
    day: str
    chat_id: str
    chat_name: str
    sender_name: str
    text: str
    media_kind: str | None
    media_label: str | None
    received_at: float
    state: str
    delivered_at: float | None
    seen_at: float | None


@dataclass
class Outbound:
    id: int
    mesh_packet_id: int | None
    chat_id: str
    chat_name: str
    text: str
    state: str
    attempts: int
    last_error: str | None
    created_at: float
    sent_at: float | None


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        schema = resources.files("veldbridge").joinpath("schema.sql").read_text()
        self.conn.executescript(schema)

    # -- startup recovery -------------------------------------------------

    def recover(self) -> None:
        """Anything in flight when we died goes back to a safe state."""
        self.conn.execute("UPDATE messages SET state='queued' WHERE state='sent'")
        # We can't know whether WhatsApp accepted it; never resend blindly.
        self.conn.execute(
            "UPDATE outbound SET state='unknown', last_error='restart while sending' "
            "WHERE state='sending'"
        )

    # -- messages ---------------------------------------------------------

    def next_short_id(self, day: str) -> int:
        row = self.conn.execute(
            "SELECT short_id FROM messages WHERE day=? ORDER BY id DESC LIMIT 1", (day,)
        ).fetchone()
        return 1 if row is None else row["short_id"] % 99 + 1

    def add_message(
        self,
        *,
        wa_msg_id: str,
        day: str,
        chat_id: str,
        chat_name: str,
        sender_name: str,
        text: str,
        media_kind: str | None,
        media_label: str | None,
        received_at: float,
    ) -> Message | None:
        """Returns None if this WA message id was already stored."""
        short_id = self.next_short_id(day)
        try:
            cur = self.conn.execute(
                "INSERT INTO messages (wa_msg_id, short_id, day, chat_id, chat_name, sender_name,"
                " text, media_kind, media_label, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (wa_msg_id, short_id, day, chat_id, chat_name, sender_name, text, media_kind,
                 media_label, received_at),
            )
        except sqlite3.IntegrityError:
            return None
        return self.message(cur.lastrowid)

    def message(self, id: int) -> Message | None:
        row = self.conn.execute("SELECT * FROM messages WHERE id=?", (id,)).fetchone()
        return Message(**row) if row else None

    def message_by_short(self, short_id: int) -> Message | None:
        """Latest message carrying #short_id (ids wrap, so the newest wins)."""
        row = self.conn.execute(
            "SELECT * FROM messages WHERE short_id=? ORDER BY id DESC LIMIT 1", (short_id,)
        ).fetchone()
        return Message(**row) if row else None

    def queued(self) -> list[Message]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE state='queued' ORDER BY id"
        ).fetchall()
        return [Message(**r) for r in rows]

    def latest_from_chat(self, chat_id: str) -> Message | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)
        ).fetchone()
        return Message(**row) if row else None

    def set_message_state(self, id: int, state: str, **ts: float) -> None:
        cols = ", ".join(f"{k}=?" for k in ts)
        sql = f"UPDATE messages SET state=?{', ' + cols if cols else ''} WHERE id=?"
        self.conn.execute(sql, (state, *ts.values(), id))

    def set_message_body(self, id: int, text: str, media_label: str | None) -> None:
        self.conn.execute("UPDATE messages SET text=?, media_label=? WHERE id=?",
                          (text, media_label, id))

    def drop_queued_for_chat(self, chat_id: str) -> int:
        """Read elsewhere: nothing still waiting from this chat goes on air."""
        cur = self.conn.execute(
            "UPDATE messages SET state='read' WHERE chat_id=? AND state='queued'", (chat_id,)
        )
        return cur.rowcount

    def mark_seen_at(self, id: int, when: float) -> None:
        self.conn.execute("UPDATE messages SET seen_at=? WHERE id=? AND seen_at IS NULL",
                          (when, id))

    def chats_with_messages(self) -> dict[str, str]:
        """chat_id -> latest chat name, for chats that have written to the owner."""
        rows = self.conn.execute(
            "SELECT chat_id, chat_name FROM messages m WHERE id = "
            "(SELECT MAX(id) FROM messages WHERE chat_id = m.chat_id)"
        ).fetchall()
        return {r["chat_id"]: r["chat_name"] for r in rows}

    # -- outbound ---------------------------------------------------------

    def add_outbound(self, *, mesh_packet_id: int | None, chat_id: str, chat_name: str,
                     text: str, created_at: float) -> Outbound:
        cur = self.conn.execute(
            "INSERT INTO outbound (mesh_packet_id, chat_id, chat_name, text, created_at)"
            " VALUES (?,?,?,?,?)",
            (mesh_packet_id, chat_id, chat_name, text, created_at),
        )
        return self.outbound(cur.lastrowid)

    def outbound(self, id: int) -> Outbound | None:
        row = self.conn.execute("SELECT * FROM outbound WHERE id=?", (id,)).fetchone()
        return Outbound(**row) if row else None

    def outbound_queued(self) -> list[Outbound]:
        rows = self.conn.execute(
            "SELECT * FROM outbound WHERE state='queued' ORDER BY id"
        ).fetchall()
        return [Outbound(**r) for r in rows]

    def outbound_in_state(self, state: str) -> list[Outbound]:
        rows = self.conn.execute(
            "SELECT * FROM outbound WHERE state=? ORDER BY id", (state,)
        ).fetchall()
        return [Outbound(**r) for r in rows]

    def update_outbound(self, id: int, **cols: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE outbound SET {sets} WHERE id=?", (*cols.values(), id))

    # -- contacts ---------------------------------------------------------

    def upsert_contact(self, chat_id: str, name: str | None, pushname: str | None,
                       is_group: bool, when: float) -> None:
        self.conn.execute(
            "INSERT INTO contacts (chat_id, name, pushname, is_group, updated_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET"
            " name=COALESCE(excluded.name, name), pushname=COALESCE(excluded.pushname, pushname),"
            " is_group=excluded.is_group, updated_at=excluded.updated_at",
            (chat_id, name, pushname, int(is_group), when),
        )

    def upsert_contacts(self, contacts: list[dict], when: float) -> None:
        """The whole address book (~2k rows) in one transaction, not one write each."""
        self.conn.execute("BEGIN")
        try:
            for c in contacts:
                if c.get("id"):
                    self.upsert_contact(c["id"], c.get("name"), c.get("pushname"),
                                        bool(c.get("isGroup")), when)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def contact(self, chat_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT chat_id, COALESCE(name, pushname) AS name, is_group FROM contacts"
            " WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return dict(row) if row and row["name"] else None

    def search_contacts(self, q: str, limit: int = 30) -> list[dict]:
        """Saved contacts and groups by name. Saved names first, then WA profile names."""
        like = f"%{q.strip().lower()}%"
        rows = self.conn.execute(
            "SELECT chat_id, COALESCE(name, pushname) AS name, is_group,"
            " name IS NOT NULL AS saved FROM contacts"
            " WHERE lower(name) LIKE ? OR lower(pushname) LIKE ?"
            " ORDER BY saved DESC, lower(COALESCE(name, pushname)) LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_messages(self, limit: int = 20) -> list[dict]:
        """For the admin page: who and when, never the text."""
        rows = self.conn.execute(
            "SELECT short_id, chat_name, sender_name, media_kind, state, received_at"
            " FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_outbound(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT chat_name, state, last_error, created_at FROM outbound"
            " ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def contact_name(self, chat_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT COALESCE(name, pushname) AS n FROM contacts WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return row["n"] if row else None

    # -- settings ---------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM messages GROUP BY state"
        ).fetchall()
        return {r["state"]: r["n"] for r in rows}
