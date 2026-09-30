"""Real mesh adapter: T-Beam on USB serial via the `meshtastic` library.

meshtastic runs its own reader threads; everything it tells us is posted onto
the asyncio loop so the Bridge stays single-threaded.

Acks: we do NOT use sendData's onResponse. The library pops the handler on the
first routing packet for that request id, and with ack-permitted handlers that
can be our own gateway's implicit ack, which would swallow the real ack from
the pocket node. Instead we watch every routing packet on `meshtastic.receive`
and let the Outbox decide (NONE + from pocket node = delivered).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from .config import MeshConfig
from .transports import MeshError

log = logging.getLogger(__name__)


class SerialMesh:
    def __init__(self, cfg: MeshConfig, loop: asyncio.AbstractEventLoop, bridge: Any):
        self.cfg = cfg
        self.loop = loop
        self.bridge = bridge
        self.iface: Any = None
        self.my_num: int | None = None
        self.last_rx = time.monotonic()
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._supervise, name="mesh", daemon=True)

    def start(self) -> None:
        from pubsub import pub  # installed with meshtastic

        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._lost.set()
        self._close()

    # -- Mesh protocol (called on the loop thread) --------------------------

    def send_dm(self, text: str) -> int:
        iface = self.iface
        if iface is None:
            raise MeshError("serial not connected")
        from meshtastic import portnums_pb2

        try:
            pkt = iface.sendData(
                text.encode("utf-8"),
                destinationId=self.cfg.pocket_node_num,
                portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                wantAck=True,
            )
        except Exception as e:  # serial write errors surface as assorted types
            self._lost.set()
            raise MeshError(str(e)) from e
        return int(pkt.id)

    # -- supervisor thread ------------------------------------------------

    def _supervise(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._connect()
                backoff = 5.0
            except Exception as e:
                log.warning("serial connect failed: %s (retry in %.0fs)", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120.0)
                continue
            while not self._stop.is_set():
                if self._lost.wait(30):
                    log.warning("serial connection lost")
                    break
                silent = time.monotonic() - self.last_rx
                if silent > self.cfg.silence_reconnect_s:
                    log.warning("no packets from gateway for %.0fs, reinitialising", silent)
                    break
            self._close()

    def _connect(self) -> None:
        from meshtastic.serial_interface import SerialInterface

        log.info("opening %s", self.cfg.serial_path)
        self._lost.clear()
        iface = SerialInterface(devPath=self.cfg.serial_path)
        self.my_num = int(iface.myInfo.my_node_num)
        self.last_rx = time.monotonic()
        self.iface = iface
        log.info("gateway node !%08x up", self.my_num)
        self._post(self.bridge.on_mesh_state, True)

    def _close(self) -> None:
        iface, self.iface = self.iface, None
        if iface is not None:
            self._post(self.bridge.on_mesh_state, False)
            try:
                iface.close()
            except Exception as e:
                log.debug("close: %s", e)

    # -- pubsub callbacks (meshtastic threads) ----------------------------

    def _on_lost(self, interface: Any = None, **_: Any) -> None:
        if interface is None or interface is self.iface:
            self._lost.set()

    def _on_receive(self, packet: dict, interface: Any = None, **_: Any) -> None:
        if interface is not None and interface is not self.iface:
            return
        self.last_rx = time.monotonic()
        try:
            from_num = int(packet.get("from", 0))
            decoded = packet.get("decoded") or {}
            port = decoded.get("portnum")
            if port == "ROUTING_APP" and decoded.get("requestId"):
                reason = (decoded.get("routing") or {}).get("errorReason", "NONE")
                self._post(self.bridge.on_mesh_ack, int(decoded["requestId"]), from_num, reason)
            text = decoded.get("text") if port == "TEXT_MESSAGE_APP" else None
            self._post_kw(
                self.bridge.on_mesh_packet,
                from_num=from_num,
                packet_id=int(packet.get("id", 0)),
                is_dm=self.my_num is not None and packet.get("to") == self.my_num,
                text=text,
                rssi=packet.get("rxRssi"),
                snr=packet.get("rxSnr"),
            )
        except Exception:
            log.exception("bad packet from radio")

    def _post(self, fn: Any, *args: Any) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def _post_kw(self, fn: Any, **kw: Any) -> None:
        self.loop.call_soon_threadsafe(lambda: fn(**kw))
