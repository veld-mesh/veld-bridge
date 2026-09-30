"""Entry point: `veldbridge --config /etc/veld-bridge/config.yaml`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import signal
import sys
import time
from typing import Any

from . import config as config_mod
from .admin import Admin
from .bridge import Bridge
from .db import Store
from .transports import MeshError, SystemClock
from .wa_socket import WaSocketServer

log = logging.getLogger("veldbridge")


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        out = {"ts": round(r.created, 3), "lvl": r.levelname, "log": r.name, "msg": r.getMessage()}
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging(level: str, file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if file:
        handlers.append(logging.handlers.RotatingFileHandler(file, maxBytes=5 << 20,
                                                             backupCount=5))
    for h in handlers:
        h.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=handlers, force=True)


END_OF_HEADERS = (b"\r\n", b"\n", b"")


async def serve_health(host: str, port: int, bridge: Bridge,
                       admin: Admin | None = None) -> asyncio.AbstractServer:
    """GET /health (open, for the farm dashboard) and the admin page (password). LAN only."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.readline(), 5)
            method, target, _ = first.decode().split(" ", 2)
            headers: dict[str, str] = {}
            while (line := await asyncio.wait_for(reader.readline(), 5)) not in END_OF_HEADERS:
                k, _, v = line.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            length = min(int(headers.get("content-length") or 0), 64_000)
            body = await asyncio.wait_for(reader.readexactly(length), 5) if length else b""
            extra = ""
            if target.split("?")[0] == "/health":
                out = json.dumps(bridge.health()).encode()
                status, ctype = "200 OK", "application/json"
            elif admin is not None:
                status, ctype, out = admin.handle(method, target, headers, body)
                if status.startswith("401"):
                    extra = 'WWW-Authenticate: Basic realm="Mesh WhatsApp", charset="UTF-8"\r\n'
            else:
                status, ctype, out = "404 Not Found", "application/json", b'{"error":"not found"}'
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n{extra}"
                         f"Content-Length: {len(out)}\r\nCache-Control: no-store\r\n"
                         "Connection: close\r\n\r\n".encode() + out)
            await writer.drain()
        except (TimeoutError, ConnectionError, ValueError, UnicodeDecodeError,
                asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("http handler failed")
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)


class LateMesh:
    """Lets the Bridge be built before the serial adapter (which needs the bridge)."""

    target: Any = None

    def send_dm(self, text: str) -> int:
        if self.target is None:
            raise MeshError("mesh not started")
        return self.target.send_dm(text)


async def run(cfg: config_mod.Config, fake_mesh: bool) -> None:
    loop = asyncio.get_running_loop()
    store = Store(cfg.db_path)
    late_mesh = LateMesh()
    wa = WaSocketServer(cfg.whatsapp.socket_path, None)
    bridge = Bridge(cfg, store, late_mesh, wa, SystemClock())
    wa.bridge = bridge

    if fake_mesh:
        from .transports import FakeMesh

        late_mesh.target = FakeMesh()
        bridge.on_mesh_state(True)
        mesh = None
    else:
        from .mesh_serial import SerialMesh

        mesh = SerialMesh(cfg.mesh, loop, bridge)
        late_mesh.target = mesh
        mesh.start()

    await wa.start()
    admin = Admin(bridge, cfg.health.admin_password) if cfg.health.admin_password else None
    health = await serve_health(cfg.health.host, cfg.health.port, bridge, admin)
    log.info("health on http://%s:%d/health%s", cfg.health.host, cfg.health.port,
             ", admin page on /" if admin else "")

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    next_tick = time.monotonic()
    while not stop.is_set():
        try:
            bridge.tick()
        except Exception:
            log.exception("tick failed")
        next_tick += 1.0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), max(0.0, next_tick - time.monotonic()))

    log.info("shutting down")
    health.close()
    await wa.close()
    if mesh:
        mesh.stop()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="veldbridge")
    p.add_argument("--config", default="/etc/veld-bridge/config.yaml")
    p.add_argument("--log-file", default=None, help="also log to a rotating file")
    p.add_argument("--fake-mesh", action="store_true",
                   help="no radio: record mesh packets in memory (smoke tests)")
    p.add_argument("--check", action="store_true", help="validate config and exit")
    args = p.parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
    except (OSError, config_mod.ConfigError) as e:
        sys.exit(f"config: {e}")
    if args.check:
        print("config ok")
        return
    setup_logging(cfg.log_level, args.log_file)
    asyncio.run(run(cfg, args.fake_mesh))


if __name__ == "__main__":
    main()
