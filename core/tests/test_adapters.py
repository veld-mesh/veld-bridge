"""Real adapters, exercised without hardware or WhatsApp."""

import asyncio
import json
import os
import tempfile

from conftest import POCKET, Harness

from veldbridge.config import MeshConfig
from veldbridge.main import serve_health
from veldbridge.mesh_serial import SerialMesh
from veldbridge.wa_socket import WaSocketServer

GATEWAY = 0x11111111


class ImmediateLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.calls.append((name, a, kw))


def _mesh():
    rec = Recorder()
    m = SerialMesh(MeshConfig(pocket_node="!a1b2c3d4"), ImmediateLoop(), rec)
    m.my_num = GATEWAY
    return m, rec


def test_serial_text_dm_becomes_mesh_packet():
    m, rec = _mesh()
    m._on_receive({"from": POCKET, "to": GATEWAY, "id": 42, "rxRssi": -90, "rxSnr": 5.5,
                   "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "r"}})
    assert rec.calls == [("on_mesh_packet", (), {
        "from_num": POCKET, "packet_id": 42, "is_dm": True, "text": "r",
        "rssi": -90, "snr": 5.5})]


def test_serial_broadcast_text_is_not_dm():
    m, rec = _mesh()
    m._on_receive({"from": POCKET, "to": 0xFFFFFFFF, "id": 1,
                   "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}})
    assert rec.calls[0][2]["is_dm"] is False


def test_serial_routing_packet_is_ack_then_presence():
    m, rec = _mesh()
    m._on_receive({"from": POCKET, "to": GATEWAY, "id": 9,
                   "decoded": {"portnum": "ROUTING_APP", "requestId": 1234,
                               "routing": {}}})
    assert rec.calls[0] == ("on_mesh_ack", (1234, POCKET, "NONE"), {})
    assert rec.calls[1][0] == "on_mesh_packet"


def test_serial_nak_reason_passed_through():
    m, rec = _mesh()
    m._on_receive({"from": GATEWAY, "to": GATEWAY, "id": 9,
                   "decoded": {"portnum": "ROUTING_APP", "requestId": 7,
                               "routing": {"errorReason": "MAX_RETRANSMIT"}}})
    assert rec.calls[0] == ("on_mesh_ack", (7, GATEWAY, "MAX_RETRANSMIT"), {})


def test_wa_socket_roundtrip():
    h = Harness()
    h.hear()

    async def go():
        path = os.path.join(tempfile.mkdtemp(), "wa.sock")
        srv = WaSocketServer(path, h.bridge)
        h.bridge.wa = srv
        await srv.start()
        reader, writer = await asyncio.open_unix_connection(path)

        def send(obj):
            writer.write(json.dumps(obj).encode() + b"\n")

        send({"type": "state", "state": "CONNECTED"})
        send({"type": "message", "id": "w1", "chatId": "1@c.us", "senderName": "Sam",
              "body": "hi", "kind": "text"})
        await writer.drain()
        await asyncio.sleep(0.05)
        first = json.loads(await reader.readline())
        assert first["type"] == "getContacts"
        send({"type": "result", "req": first["req"], "ok": True, "data": [
            {"id": "1@c.us", "name": "Sam Smith"}]})
        await writer.drain()
        await asyncio.sleep(0.05)
        assert h.store.contact_name("1@c.us") == "Sam Smith"
        h.dm("@1 yo")
        h.bridge.tick()
        out = json.loads(await reader.readline())
        assert out == {"type": "send", "req": out["req"], "chatId": "1@c.us", "text": "yo"}
        writer.close()
        await asyncio.sleep(0.05)
        assert h.bridge.wa_state == "DISCONNECTED"
        await srv.close()

    asyncio.run(go())


def test_health_endpoint():
    h = Harness()

    async def go():
        srv = await serve_health("127.0.0.1", 0, h.bridge)
        port = srv.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        srv.close()
        return raw

    raw = asyncio.run(go())
    assert raw.startswith(b"HTTP/1.1 200 OK")
    body = json.loads(raw.split(b"\r\n\r\n", 1)[1])
    assert body["wa"] == "DISCONNECTED" and body["queue"] == 0
