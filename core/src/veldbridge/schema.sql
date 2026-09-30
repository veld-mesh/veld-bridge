-- Bridge state. Bodies live here in full; only compact/chunked forms go on air.
PRAGMA journal_mode = WAL;
-- FULL fsyncs every autocommit write; on the NAS's btrfs that blocked the
-- event loop for minutes. NORMAL in WAL mode is crash-safe and never corrupts.
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    wa_msg_id    TEXT NOT NULL UNIQUE,          -- dedupe on WA redelivery
    short_id     INTEGER NOT NULL,              -- #n, 1..99, resets daily
    day          TEXT NOT NULL,                 -- local date short_id belongs to
    chat_id      TEXT NOT NULL,
    chat_name    TEXT NOT NULL,                 -- person, or group name
    sender_name  TEXT NOT NULL,
    text         TEXT NOT NULL DEFAULT '',      -- full body / caption
    media_kind   TEXT,                          -- photo, video, voice, pdf, doc, loc, contact, ...
    media_label  TEXT,                          -- rendered "[pdf: x.pdf]" etc.
    received_at  REAL NOT NULL,
    state        TEXT NOT NULL DEFAULT 'queued'
                 CHECK (state IN ('queued', 'sent', 'acked', 'read')),
    delivered_at REAL,
    seen_at      REAL
);
CREATE INDEX IF NOT EXISTS messages_state ON messages (state, received_at);
CREATE INDEX IF NOT EXISTS messages_short ON messages (day, short_id);

CREATE TABLE IF NOT EXISTS outbound (
    id             INTEGER PRIMARY KEY,
    mesh_packet_id INTEGER,                      -- source packet, for dedupe
    chat_id        TEXT NOT NULL,
    chat_name      TEXT NOT NULL,
    text           TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'queued'
                   CHECK (state IN ('queued', 'sending', 'sent', 'failed', 'unknown')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     REAL NOT NULL,
    sent_at        REAL
);
CREATE INDEX IF NOT EXISTS outbound_state ON outbound (state, created_at);

CREATE TABLE IF NOT EXISTS contacts (
    chat_id    TEXT PRIMARY KEY,
    name       TEXT,
    pushname   TEXT,
    is_group   INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                          -- JSON
);
