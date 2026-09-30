"""Config schema. YAML in, frozen dataclasses out; unknown keys are an error."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AllowEntry:
    chat_id: str
    name: str


@dataclass(frozen=True)
class MeshConfig:
    # Placeholders until you read them from `meshtastic --info` on the Pi.
    serial_path: str = "/dev/serial/by-id/REPLACE-ME"
    pocket_node: str = "!00000000"
    byte_budget: int = 200
    min_packet_interval_s: float = 10.0
    ack_timeout_s: float = 60.0
    retry_schedule_s: tuple[float, ...] = (60.0, 180.0, 600.0)
    reachable_window_s: float = 600.0
    probe_interval_s: float = 300.0
    flush_cap: int = 10
    # "No packets from the gateway for this long" is treated as a serial fault.
    silence_reconnect_s: float = 1800.0

    @property
    def pocket_node_num(self) -> int:
        return node_num(self.pocket_node)


@dataclass(frozen=True)
class RelayConfig:
    all: bool = True
    allowlist: tuple[AllowEntry, ...] = ()
    muted: tuple[str, ...] = ()
    # "ack": mark the WA chat read when the compact line is acked on the mesh.
    # "read": only when the owner pulls the full text with `r`.
    # "never": the bridge never marks anything read; the phone does it later.
    mark_seen: str = "ack"
    # WA replays old unread messages on link/reconnect. Never relay a message
    # sent before the first link, nor one older than this.
    max_age_s: float = 21600.0
    # The owner read or sent a WhatsApp on their phone: they have WhatsApp there, so hold
    # relaying to the mesh until this long after his last phone activity. 0 = off.
    phone_pause_s: float = 1800.0


@dataclass(frozen=True)
class WhatsAppConfig:
    socket_path: str = "/run/veld-bridge/wa.sock"
    # Where wa writes the link QR (VB_QR_PNG); the admin page shows it for re-linking.
    qr_png: str = "/var/lib/veld-bridge/qr.png"
    min_send_interval_s: float = 3.0
    signature: str | None = None


@dataclass(frozen=True)
class TranscriptionConfig:
    backend: str = "none"


@dataclass(frozen=True)
class HealthConfig:
    host: str = "0.0.0.0"
    port: int = 8787
    # Admin page at / on the same port. Unset = no admin page, /health only.
    admin_password: str | None = None


@dataclass(frozen=True)
class Config:
    mesh: MeshConfig = field(default_factory=MeshConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    db_path: str = "/var/lib/veld-bridge/bridge.db"
    timezone: str = "Africa/Johannesburg"
    log_level: str = "INFO"


def node_num(node_id: str | int) -> int:
    """'!a1b2c3d4' -> 0xa1b2c3d4. Ints pass through."""
    if isinstance(node_id, int):
        return node_id
    s = node_id.strip()
    if s.startswith("!"):
        s = s[1:]
    try:
        return int(s, 16)
    except ValueError as e:
        raise ConfigError(f"bad node id {node_id!r}: expected !hex") from e


def _build(cls: type, data: Any, path: str) -> Any:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path or 'config'}: expected a mapping")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(fields)
    if unknown:
        raise ConfigError(f"{path or 'config'}: unknown key(s) {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        sub = f"{path}.{name}" if path else name
        default = getattr(cls(), name)
        if dataclasses.is_dataclass(default):
            kwargs[name] = _build(type(default), value, sub)
        elif name == "allowlist":
            kwargs[name] = tuple(_allow(e, sub) for e in (value or []))
        elif isinstance(default, tuple):
            if not isinstance(value, list):
                raise ConfigError(f"{sub}: expected a list")
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def _allow(entry: Any, path: str) -> AllowEntry:
    if not isinstance(entry, dict) or "chat_id" not in entry:
        raise ConfigError(f"{path}: each entry needs chat_id (and name)")
    return AllowEntry(chat_id=str(entry["chat_id"]), name=str(entry.get("name", entry["chat_id"])))


def validate(cfg: Config) -> Config:
    m = cfg.mesh
    if not 40 <= m.byte_budget <= 233:
        raise ConfigError("mesh.byte_budget must be 40..233 (sendData rejects > 233)")
    if m.min_packet_interval_s < 10:
        raise ConfigError("mesh.min_packet_interval_s must be >= 10 (airtime rule)")
    if not m.retry_schedule_s:
        raise ConfigError("mesh.retry_schedule_s must not be empty")
    node_num(m.pocket_node)
    if cfg.whatsapp.min_send_interval_s < 3:
        raise ConfigError("whatsapp.min_send_interval_s must be >= 3")
    if cfg.relay.mark_seen not in ("ack", "read", "never"):
        raise ConfigError("relay.mark_seen must be 'ack', 'read' or 'never'")
    if cfg.relay.max_age_s <= 0:
        raise ConfigError("relay.max_age_s must be > 0")
    if cfg.relay.phone_pause_s < 0:
        raise ConfigError("relay.phone_pause_s must be >= 0")
    pw = cfg.health.admin_password
    if pw is not None and (not isinstance(pw, str) or len(pw) < 8):
        raise ConfigError("health.admin_password must be at least 8 characters")
    if cfg.transcription.backend != "none":
        raise ConfigError("transcription.backend: only 'none' is implemented in v1")
    return cfg


def load(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return validate(_build(Config, data, ""))


def from_dict(data: dict) -> Config:
    return validate(_build(Config, data, ""))
