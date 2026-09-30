import asyncio
import base64
import json

import pytest
from conftest import Harness

from veldbridge.admin import Admin
from veldbridge.main import serve_health

PW = "correct horse"
JAN = "27820000010@c.us"
THABO = "27820000011@c.us"
FAMILY = "120363000000000009@g.us"
STRANGER = "27829999999@c.us"


def auth(pw=PW):
    return {"authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


@pytest.fixture
def a():
    h = Harness(relay={"allowlist": [{"chat_id": JAN, "name": "Jan Botha"}]})
    h.store.upsert_contacts([
        {"id": JAN, "name": "Jan Botha"},
        {"id": THABO, "name": "Thabo Mokoena"},
        {"id": FAMILY, "name": "Botha Family", "isGroup": True},
    ], when=1.0)
    adm = Admin(h.bridge, PW)
    adm.h = h
    return adm


def get(a, path, headers=None):
    status, _, body = a.handle("GET", path, auth() if headers is None else headers, b"")
    return status, body


def post(a, path, data, csrf=True):
    headers = {**auth(), **({"x-vb-admin": "1"} if csrf else {})}
    status, _, body = a.handle("POST", path, headers, json.dumps(data).encode())
    return status, json.loads(body)


def test_password_required(a):
    assert get(a, "/", headers={})[0].startswith("401")
    assert get(a, "/", headers=auth("wrong"))[0].startswith("401")
    status, body = get(a, "/")
    assert status == "200 OK" and b"<title>Mesh WhatsApp</title>" in body


def test_state_starts_from_config_and_hides_bodies(a):
    a.h.wa_msg(body="secret words")
    s = json.loads(get(a, "/api/state")[1])
    assert [f["name"] for f in s["favourites"]] == ["Jan Botha"]
    assert s["groups"] == [] and s["relay_all"] is True
    assert "secret words" not in json.dumps(s)
    assert s["recent"][0]["sender_name"] == "Sam Smith"


def test_search_marks_favourites(a):
    by = {h["name"]: h for q in ("jan", "family")
          for h in json.loads(get(a, f"/api/search?q={q}")[1])}
    assert by["Jan Botha"]["allowed"] and not by["Botha Family"]["allowed"]
    assert by["Botha Family"]["is_group"]
    assert json.loads(get(a, "/api/search?q=e")[1]) == []    # too short


def test_posts_need_the_admin_header(a):
    status, _ = post(a, "/api/favourite", {"chat_id": THABO, "on": True}, csrf=False)
    assert status.startswith("403")
    assert THABO not in a.h.bridge.allowlist()


def test_favourite_lets_at_name_reach_someone_who_never_wrote(a):
    assert a.h.bridge.resolve("thabo") is None
    status, s = post(a, "/api/favourite", {"chat_id": THABO, "on": True})
    assert status == "200 OK"
    assert a.h.bridge.resolve("thabo") == (THABO, "Thabo Mokoena")
    post(a, "/api/favourite", {"chat_id": THABO, "on": False})
    assert THABO not in a.h.bridge.allowlist()


def test_only_known_contacts_can_be_favourited(a):
    status, body = post(a, "/api/favourite", {"chat_id": STRANGER, "on": True})
    assert status.startswith("400") and "contacts" in body["error"]


def test_relayed_group_reaches_the_mesh(a):
    h = a.h
    h.hear()
    assert h.wa_msg(chat=FAMILY, isGroup=True, chatName="Botha Family") is None
    _, s = post(a, "/api/favourite", {"chat_id": FAMILY, "on": True})
    assert [g["name"] for g in s["groups"]] == ["Botha Family"]
    assert h.wa_msg(chat=FAMILY, isGroup=True, chatName="Botha Family") is not None


def test_mute_and_settings(a):
    h = a.h
    post(a, "/api/mute", {"chat_id": JAN, "on": True})
    assert JAN in h.bridge.muted()
    assert h.wa_msg(chat=JAN) is None
    _, s = post(a, "/api/settings", {"relay_all": False, "mark_seen": "never"})
    assert s["relay_all"] is False and h.bridge.mark_seen_mode() == "never"
    status, _ = post(a, "/api/settings", {"mark_seen": "sometimes"})
    assert status.startswith("400")


def test_http_health_stays_open_admin_needs_password():
    h = Harness()

    async def go(req):
        srv = await serve_health("127.0.0.1", 0, h.bridge, Admin(h.bridge, PW))
        port = srv.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(req)
        await writer.drain()
        raw = await reader.read()
        srv.close()
        return raw

    assert asyncio.run(go(b"GET /health HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 200 OK")
    raw = asyncio.run(go(b"GET / HTTP/1.1\r\n\r\n"))
    assert raw.startswith(b"HTTP/1.1 401") and b"WWW-Authenticate: Basic" in raw
    body = json.dumps({"chat_id": "x", "on": True}).encode()
    creds = base64.b64encode(f"u:{PW}".encode())
    raw = asyncio.run(go(b"POST /api/mute HTTP/1.1\r\nAuthorization: Basic " + creds
                         + b"\r\nX-VB-Admin: 1\r\nContent-Length: " + str(len(body)).encode()
                         + b"\r\n\r\n" + body))
    assert raw.startswith(b"HTTP/1.1 200 OK")
    assert "x" in h.bridge.muted()


def test_link_qr_only_while_unlinked_and_only_with_password(a, tmp_path):
    qr = tmp_path / "qr.png"
    a.qr_png = str(qr)
    assert get(a, "/api/qr.png")[0].startswith("404")
    assert json.loads(get(a, "/api/state")[1])["qr"] is False
    qr.write_bytes(b"\x89PNG fake")
    assert get(a, "/api/qr.png") == ("200 OK", b"\x89PNG fake")
    assert json.loads(get(a, "/api/state")[1])["qr"] is True
    assert get(a, "/api/qr.png", headers={})[0].startswith("401")
