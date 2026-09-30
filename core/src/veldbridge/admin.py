"""Admin page for the LAN: favourites, relayed groups, mutes, relay mode, blue ticks,
and who gets a WhatsApp when a mesh node sends 🆘.

Pure request handling (no sockets) so it tests like the rest of core. Served by
main.serve_health on the same port as /health. Never shows message bodies.
"""

from __future__ import annotations

import base64
import hmac
import json
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .bridge import Bridge

MARK_SEEN = ("ack", "read", "never")
# Browsers only send this header from our own page's fetch(); a form on another
# site can't, so it doubles as CSRF protection for the POST routes.
CSRF_HEADER = "x-vb-admin"

Response = tuple[str, str, bytes]


def _json(obj: object, status: str = "200 OK") -> Response:
    return status, "application/json", json.dumps(obj).encode()


def _err(msg: str, status: str = "400 Bad Request") -> Response:
    return _json({"error": msg}, status)


class Admin:
    def __init__(self, bridge: Bridge, password: str, qr_png: str | None = None):
        self.bridge = bridge
        self.password = password.encode()
        self.qr_png = qr_png or bridge.cfg.whatsapp.qr_png
        self.page = resources.files("veldbridge").joinpath("admin.html").read_bytes()

    def authorised(self, headers: dict[str, str]) -> bool:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return False
        try:
            _, _, pw = base64.b64decode(auth[6:]).partition(b":")
        except ValueError:
            return False
        return hmac.compare_digest(pw, self.password)

    def handle(self, method: str, target: str, headers: dict[str, str], body: bytes) -> Response:
        if not self.authorised(headers):
            return "401 Unauthorized", "text/plain", b"password required"
        url = urlsplit(target)
        route = (method, url.path)
        if route == ("GET", "/"):
            return "200 OK", "text/html; charset=utf-8", self.page
        if route == ("GET", "/api/state"):
            return _json(self.state())
        if route == ("GET", "/api/qr.png"):
            # Only exists while WA needs linking. It IS a key to the account: auth only.
            try:
                return "200 OK", "image/png", Path(self.qr_png).read_bytes()
            except OSError:
                return _err("no QR: WhatsApp is linked or still starting", "404 Not Found")
        if route == ("GET", "/api/search"):
            q = parse_qs(url.query).get("q", [""])[0]
            return _json(self.search(q)) if len(q.strip()) >= 2 else _json([])
        if method == "POST" and url.path.startswith("/api/"):
            if headers.get(CSRF_HEADER) != "1":
                return _err("missing admin header", "403 Forbidden")
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return _err("bad json")
            if not isinstance(data, dict):
                return _err("bad json")
            action = {"/api/favourite": self.favourite, "/api/mute": self.mute,
                      "/api/sos": self.sos, "/api/settings": self.settings}.get(url.path)
            if action:
                return action(data)
        return _err("not found", "404 Not Found")

    # -- reads ------------------------------------------------------------

    def state(self) -> dict:
        b, store = self.bridge, self.bridge.store
        allow, muted = b.allowlist(), b.muted()

        def entry(chat_id: str, name: str | None = None) -> dict:
            c = store.contact(chat_id)
            return {"chat_id": chat_id, "name": name or (c or {}).get("name") or chat_id,
                    "is_group": chat_id.endswith("@g.us")}

        return {
            "health": b.health(),
            "qr": Path(self.qr_png).is_file(),
            "relay_all": b.relay_all(),
            "mark_seen": b.mark_seen_mode(),
            "wa_alerts": b.wa_alerts(),
            "paused": b.paused(),
            "phone_pause": b.phone_pause_on(),
            "phone_pause_left_s": (None if (left := b.phone_pause_left()) is None
                                   else round(left)),
            "favourites": [entry(i, n) for i, n in allow.items() if not i.endswith("@g.us")],
            "groups": [entry(i, n) for i, n in allow.items() if i.endswith("@g.us")],
            "muted": sorted((entry(i) for i in muted), key=lambda e: e["name"].lower()),
            "sos": [entry(i, n) for i, n in b.sos_recipients().items()],
            "recent": store.recent_messages(),
            "outbound": store.recent_outbound(),
        }

    def search(self, q: str) -> list[dict]:
        allow, muted = self.bridge.allowlist(), self.bridge.muted()
        sos = self.bridge.sos_recipients()
        return [{"chat_id": c["chat_id"], "name": c["name"], "is_group": bool(c["is_group"]),
                 "saved": bool(c["saved"]), "allowed": c["chat_id"] in allow,
                 "muted": c["chat_id"] in muted, "sos": c["chat_id"] in sos}
                for c in self.bridge.store.search_contacts(q)]

    # -- writes -----------------------------------------------------------

    def _known(self, data: dict) -> dict | None:
        chat_id = data.get("chat_id")
        return self.bridge.store.contact(chat_id) if isinstance(chat_id, str) else None

    def favourite(self, data: dict) -> Response:
        """Favourite a person (@name before they write) or relay a group."""
        on = bool(data.get("on"))
        if on:
            c = self._known(data)
            if c is None:  # never open a chat with a number that isn't a contact
                return _err("not in your WhatsApp contacts")
            self.bridge.set_allowed(c["chat_id"], c["name"], True)
        elif isinstance(data.get("chat_id"), str):
            self.bridge.set_allowed(data["chat_id"], "", False)
        else:
            return _err("chat_id required")
        return _json(self.state())

    def sos(self, data: dict) -> Response:
        """Add or remove a WhatsApp SOS recipient (person or group)."""
        on = bool(data.get("on"))
        if on:
            c = self._known(data)
            if c is None:  # never open a chat with a number that isn't a contact
                return _err("not in your WhatsApp contacts")
            self.bridge.set_sos_recipient(c["chat_id"], c["name"], True)
        elif isinstance(data.get("chat_id"), str):
            self.bridge.set_sos_recipient(data["chat_id"], "", False)
        else:
            return _err("chat_id required")
        return _json(self.state())

    def mute(self, data: dict) -> Response:
        chat_id = data.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            return _err("chat_id required")
        self.bridge.set_muted(chat_id, bool(data.get("on")))
        return _json(self.state())

    def settings(self, data: dict) -> Response:
        if "relay_all" in data:
            self.bridge.store.set_setting("all", bool(data["relay_all"]))
        if "wa_alerts" in data:
            self.bridge.store.set_setting("wa_alerts", bool(data["wa_alerts"]))
        if "phone_pause" in data:
            self.bridge.store.set_setting("phone_pause", bool(data["phone_pause"]))
        if "paused" in data:
            self.bridge.set_paused(bool(data["paused"]))
        if "mark_seen" in data:
            if data["mark_seen"] not in MARK_SEEN:
                return _err("mark_seen must be ack, read or never")
            self.bridge.store.set_setting("mark_seen", data["mark_seen"])
        return _json(self.state())
