"""Real WhatsApp transport: NDJSON over a Unix socket; `wa/` connects as the client.

core -> wa: {"type":"send","req":N,"chatId":...,"text":...}
            {"type":"markSeen","req":N,"chatId":...}
            {"type":"getContacts","req":N}
wa -> core: {"type":"message", ...normalised message...}
            {"type":"state","state":"CONNECTED"|"DISCONNECTED"|"QR"|...}
            {"type":"qr","qr":"..."}
            {"type":"chatRead","chatId":...}     (read on another device)
            {"type":"own","chatId":...,"timestamp":...}  (owner sent from another device)
            {"type":"result","req":N,"ok":bool,"error":str|null,"data":...}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

MAX_LINE = 1 << 20


class WaSocketServer:
    def __init__(self, path: str, bridge: Any):
        self.path = path
        self.bridge = bridge
        self._writer: asyncio.StreamWriter | None = None
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        self._server = await asyncio.start_unix_server(self._client, self.path, limit=MAX_LINE)
        os.chmod(self.path, 0o660)
        log.info("wa socket listening on %s", self.path)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # -- WhatsApp protocol ------------------------------------------------

    def send(self, req: int, chat_id: str, text: str) -> None:
        self._write({"type": "send", "req": req, "chatId": chat_id, "text": text})

    def mark_seen(self, req: int, chat_id: str) -> None:
        self._write({"type": "markSeen", "req": req, "chatId": chat_id})

    def get_contacts(self, req: int) -> None:
        self._write({"type": "getContacts", "req": req})

    def _write(self, obj: dict) -> None:
        w = self._writer
        if w is None or w.is_closing():
            # The bridge only sends while state is CONNECTED, which implies a client;
            # losing a race here reads as a dropped request, handled on disconnect.
            log.warning("wa adapter not connected; dropping %s", obj["type"])
            return
        w.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")

    # -- connection handling ----------------------------------------------

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._writer is not None and not self._writer.is_closing():
            log.warning("second wa client; replacing the old one")
            self._writer.close()
        self._writer = writer
        log.info("wa adapter connected")
        try:
            while line := await reader.readline():
                try:
                    self.dispatch(json.loads(line))
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("bad line from wa: %s", e)
        except (ConnectionError, asyncio.LimitOverrunError, ValueError) as e:
            log.warning("wa socket error: %s", e)
        finally:
            if self._writer is writer:
                self._writer = None
                self.bridge.on_wa_state("DISCONNECTED")
            writer.close()
            log.info("wa adapter disconnected")

    def dispatch(self, msg: dict) -> None:
        t = msg["type"]
        if t == "message":
            self.bridge.on_wa_message(msg)
        elif t == "state":
            self.bridge.on_wa_state(str(msg["state"]))
        elif t == "chatRead":
            self.bridge.on_wa_chat_read(str(msg["chatId"]))
        elif t == "own":
            ts = msg.get("timestamp")
            self.bridge.on_wa_own(str(msg["chatId"]), float(ts) if ts is not None else None)
        elif t == "result":
            self.bridge.on_wa_result(int(msg["req"]), bool(msg["ok"]), msg.get("error"),
                                     msg.get("data"))
        elif t == "qr":
            log.warning("WhatsApp needs linking: scan the QR printed by the wa service")
            self.bridge.on_wa_state("QR")
        else:
            log.debug("ignoring wa message type %s", t)
