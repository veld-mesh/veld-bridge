# ADR-001: Process layout — Node WhatsApp adapter + Python core

- **Status:** Accepted
- **Date:** 2026-09-26
- **Context checked against:** whatsapp-web.js v1.34.7 + `main`, `@meshtastic/core` 2.6.7 / `transport-node-serial` 0.0.2 (npm), meshtastic/web `main`, Python `meshtastic` 2.7.11

## Decision

Two processes on the gateway host (a Raspberry Pi or a NAS), each its own systemd unit or container:

| Process | Language | Owns |
|---|---|---|
| `wa/` | Node 22 LTS, `whatsapp-web.js` | Thin adapter only: inbound message events, `send`, `markSeen`, `getContacts`, connection state, QR |
| `core/` | Python ≥ 3.11, `meshtastic` 2.7.x | Everything else: SQLite, queue, compaction, chunking, commands, presence, ack/retry state machine, `/health` |

The single-Node-process alternative (`@meshtastic/js`) is **rejected for now**; revisit when `@meshtastic/sdk` 1.x and `transport-node-serial` 1.x are published and have Node users.

## Why

### Mesh library: Python `meshtastic` wins clearly
- **Actively released** (2.7.9 → 2.7.11, Jun–Jul 2026) and it is the same code path the official CLI uses, so serial handling gets far more real-world use.
- **JS side is mid-rewrite.** Published `@meshtastic/core` 2.6.7 and `transport-node-serial` 0.0.2 are a year old (Sep 2025). `main` has an unpublished `@meshtastic/sdk` 1.0.0 that "replaces @meshtastic/core" with a different API (`Result` types). Building on either means building on something about to change or not yet released.
- **JS long-run bug:** published `Queue.push` adds two dispatcher subscriptions per packet (including heartbeats) and never removes them — an unbounded listener leak for a process meant to run for weeks unattended.
- **JS ack check is weak:** `sendText` resolves on any routing ACK with the matching `requestId` without checking which node sent it, so a local implicit ack could be mistaken for delivery.
- Neither library auto-reconnects serial, so the watchdog is ours either way — no advantage to JS there.

### WhatsApp library: `whatsapp-web.js` (per brief) — usable, with care
- Maintained (v1.34.7, 2026-04-24; commits through Sep 2026), small maintainer core, ~2-month worst-case fix latency for WA Web breakages.
- **Currently needs a git pin:** a July 2026 WA Web change (`id._serialized` → `id.$1`) is fixed on `main` (commit `58ddf15`, 2026-09-26) but not in an npm release. `wa/package.json` pins `github:pedroslopez/whatsapp-web.js#<sha>` until a release ships. Verify the sha when scaffolding.
- Media *sending* is broken upstream right now (#201921) — irrelevant, we only send text. `downloadMedia()` on uncached images is flaky (#201908) — we only need media *kind* + caption for v1 (no transcription), so not blocking.
- Runs on arm64 with apt `chromium` (`PUPPETEER_SKIP_DOWNLOAD=1`, `executablePath: '/usr/bin/chromium'`, `--no-sandbox --disable-dev-shm-usage --disable-gpu --no-zygote`). Expect ~300–600 MB RSS (unverified) → Pi with ≥ 2 GB.

## Consequences / implementation rules this ADR fixes

### Glue: NDJSON over a Unix domain socket
- `core` is the **server** at `/run/veld-bridge/wa.sock` (systemd `RuntimeDirectory=veld-bridge`, mode 0660, group `pi`). `wa` is the client and reconnects with backoff. `core` outlives `wa` restarts, so replies can be held ("⚠ WA offline, queued") and flushed on reconnect.
- One JSON object per line, both directions. `wa → core`: `{"type":"message",...}`, `{"type":"state","state":"CONNECTED"}`, `{"type":"qr",...}`, and `{"type":"result","req":N,"ok":true|false,"error":...}`. `core → wa`: `{"type":"send","req":N,"chatId":...,"text":...}`, `{"type":"markSeen","req":N,"chatId":...}`, `{"type":"getContacts","req":N}`.
- No TCP port for the glue → nothing extra exposed on the LAN. `/health` is a separate HTTP listener in `core` bound to the LAN interface.
- The protocol is the seam for tests: `FakeWhatsApp` (Python) implements the same interface `core` uses for the socket client; the Node adapter gets a small fake-core test harness.

### Mesh adapter rules (from reading `meshtastic` source)
- Open with `SerialInterface(devPath="/dev/serial/by-id/...")`.
- **Do not use `sendText` for acked DMs.** Its `onResponse` silently drops successful ACKs (only NAKs get through) unless the handler is registered ack-permitted. Use:
  `iface.sendData(text.encode(), destinationId=node, portNum=TEXT_MESSAGE_APP, wantAck=True, onResponse=cb, onResponseAckPermitted=True)`
- In `cb`: `reason = packet["decoded"]["routing"].get("errorReason", "NONE")`; treat as delivered **only if** `reason == "NONE"` **and** `packet["from"] == pocket_node_num`. Everything else is a NAK/implicit-ack → retry schedule.
- The library has no response timeout and removes the handler after one call → `core` runs its own 60 s ack timer.
- Presence: subscribe to `meshtastic.receive` (all packets), filter on `from`, track last-heard ourselves. `interface.nodes[..].lastHeard` only updates for some port types.
- Reconnect: on `meshtastic.connection.lost` or "no packets for N min", `close()` and build a new `SerialInterface` in a supervisor loop; systemd `Restart=always` is the backstop.
- `sendData` rejects payloads > 233 bytes (`DATA_PAYLOAD_LEN`); our 200-byte budget sits safely under it.

### WA adapter rules (from reading `whatsapp-web.js` source)
- `LocalAuth({ dataPath })` → session under a 0700 directory outside the repo; never logged or committed.
- Listen on `message` (it excludes `fromMe`); drop `status@broadcast`, `@newsletter`, and `@g.us` unless allowlisted. People may now appear as `@lid` as well as `@c.us`.
- `client.sendMessage(chatId, text, { sendSeen: false })`. The default `sendSeen: true` would mark the chat read on every reply, which breaks the read-receipt rule. Mark seen only via `client.sendSeen(chatId)` when `core` asks.
- On `disconnected` the library `destroy()`s itself → adapter builds a **new** `Client` and `initialize()`s again; also a "not ready within N min" watchdog (known upstream hang, #201919). Kill leftover Chromium children before re-init.
- Pin `webVersionCache` so a WA Web push doesn't silently change behaviour mid-season.

### Tooling
- `core/`: `pyproject.toml`, pytest, ruff. `wa/`: `node:test` (no framework). One GitHub Actions workflow runs both. No hardware is needed in CI: everything goes through `FakeMesh` / `FakeWhatsApp`.

## Risks accepted
- **WhatsApp ban risk** on an unofficial client. Mitigated by the brief's rules: event-driven only, reply-only (never open new chats), ≥ 3 s between sends, no bulk anything.
- **Two runtimes on the Pi** (Node + Python) make install heavier. `install.sh` owns this.
- **Upstream breakage windows** of up to ~2 months for whatsapp-web.js. Pinning plus `deploy.sh` makes bumping the pin a one-line change.

## Amendments during implementation (2026-09-26)

- **Acks come from `meshtastic.receive`, not `onResponse`.** The library pops a response
  handler on the first routing packet for a request id. With `onResponseAckPermitted=True`
  that first packet can be our own gateway's implicit ack, and then the real ack from the
  pocket node never reaches us. `mesh_serial.py` sends with `sendData(..., wantAck=True)` and
  no handler. It reads every `ROUTING_APP` packet and passes `(requestId, from, errorReason)`
  to the outbox. The rule itself hasn't changed: a packet counts as delivered only with
  `NONE` from the pocket node. An implicit ack is ignored (it is not a NAK), and a late
  real ack for an earlier attempt still counts.
- **Group filtering lives in `core`**, because the allowlist lives there. `wa/` still drops
  `status@broadcast`, other `@broadcast` ids, `@newsletter` and `fromMe`.
- **Pin format:** `wa/package.json` uses
  `git+https://github.com/pedroslopez/whatsapp-web.js.git#58ddf1561cd7…`. That sha was
  still `main` HEAD when checked on 2026-09-26. Switch back to an npm version once a
  release after 1.34.7 ships.
- **WA Web pin** is `WA_WEB_VERSION` in `wa.env`. `LocalWebCache` only serves a version
  that was asked for by name, so the README step reads it from the cache after the
  first link.
