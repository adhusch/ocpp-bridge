"""Konfiguration über Umgebungsvariablen (passend für Docker/Portainer)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "ja")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _list(name: str) -> list[str]:
    v = os.environ.get(name, "")
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class Config:
    # Netzwerk: ein Port für OCPP (WebSocket) und die HTTP-API für EVCC
    host: str = field(default_factory=lambda: os.environ.get("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8887))

    # Persistenz (Transaktions-IDs, letzte Sollwerte)
    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR", "/data"))

    # OCPP
    connector_id: int = field(default_factory=lambda: _int("CONNECTOR_ID", 1))
    heartbeat_interval: int = field(default_factory=lambda: _int("HEARTBEAT_INTERVAL", 60))
    meter_interval: int = field(default_factory=lambda: _int("METER_INTERVAL", 10))
    meter_measurands: str = field(
        default_factory=lambda: os.environ.get(
            "METER_MEASURANDS",
            "Energy.Active.Import.Register,Power.Active.Import,Current.Import,Voltage",
        )
    )
    # Security Profile 1 (HTTP Basic Auth): leer = keine Authentifizierung
    ocpp_password: str = field(default_factory=lambda: os.environ.get("OCPP_PASSWORD", ""))
    # Erlaubte Charge-Point-IDs (leer = alle)
    allowed_chargers: list[str] = field(default_factory=lambda: _list("ALLOWED_CHARGERS"))
    # ID, falls die Wallbox ohne ID im Pfad verbindet (ws://host:8887/)
    default_charger_id: str = field(default_factory=lambda: os.environ.get("DEFAULT_CHARGER_ID", "wallbox"))

    # Autorisierung / Plug & Charge
    # ID-Tag, mit dem die Bridge per RemoteStartTransaction startet
    id_tag: str = field(default_factory=lambda: os.environ.get("ID_TAG", "evcc"))
    # Erlaubte RFID-/ID-Tags für lokale Starts (leer = jeder Tag wird akzeptiert)
    allowed_id_tags: list[str] = field(default_factory=lambda: _list("ALLOWED_ID_TAGS"))
    # Plug & Charge serverseitig: beim Einstecken (Status "Preparing") automatisch RemoteStart senden
    auto_start: bool = field(default_factory=lambda: _bool("AUTO_START", True))
    # Pausieren: "profile" = 0 A per Ladeprofil (Transaktion bleibt), "stop" = RemoteStopTransaction
    disable_mode: str = field(default_factory=lambda: os.environ.get("DISABLE_MODE", "profile").lower())
    # Wenn Ladeprofile abgelehnt werden: beim Pausieren auf RemoteStop ausweichen
    stop_fallback: bool = field(default_factory=lambda: _bool("STOP_FALLBACK", True))

    # Ladeprofil-Feinheiten (wie in EVCC)
    profile_id: int = field(default_factory=lambda: _int("PROFILE_ID", 1))
    stack_level: int = field(default_factory=lambda: _int("STACK_LEVEL", 0))
    # Standard-Ladestrom, bis EVCC einen Wert vorgibt
    default_current: float = field(default_factory=lambda: float(os.environ.get("DEFAULT_CURRENT", "6")))

    # HTTP-API
    api_token: str = field(default_factory=lambda: os.environ.get("API_TOKEN", ""))

    # Logging
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO").upper())
    log_ocpp: bool = field(default_factory=lambda: _bool("LOG_OCPP", False))


config = Config()
