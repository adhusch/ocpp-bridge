"""Zustand je Ladepunkt plus einfache JSON-Persistenz."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger("state")

# OCPP-Status -> EVCC-Status (A = kein Auto, B = verbunden, C = lädt, E/F = Fehler)
STATUS_MAP = {
    "Available": "A",
    "Unavailable": "A",
    "Reserved": "A",
    "Preparing": "B",
    "SuspendedEV": "B",
    "SuspendedEVSE": "B",
    "Finishing": "B",
    "Charging": "C",
    "Faulted": "F",
}


@dataclass
class ChargerState:
    cp_id: str
    connected: bool = False
    last_seen: float = 0.0

    # aus BootNotification
    vendor: str = ""
    model: str = ""
    firmware: str = ""
    serial: str = ""

    # Status
    ocpp_status: str = "Unknown"
    error_code: str = "NoError"
    status_info: str = ""

    # Transaktion
    transaction_id: Optional[int] = None
    id_tag: str = ""
    last_id_tag: str = ""
    tx_start: Optional[str] = None
    meter_start_wh: Optional[float] = None
    last_session_kwh: Optional[float] = None

    # Messwerte
    power_w: float = 0.0
    energy_kwh: Optional[float] = None
    currents: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    voltages: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    soc: Optional[float] = None
    meter_ts: float = 0.0

    # Sollwerte von EVCC
    desired_enabled: bool = False
    max_current: float = 6.0
    phases: int = 0  # 0 = nicht vorgegeben (Wallbox entscheidet)

    # Fähigkeiten / Rückmeldungen
    profile_supported: Optional[bool] = None
    last_profile_status: str = ""
    features: str = ""

    def evcc_status(self) -> str:
        if not self.connected:
            return "A"
        return STATUS_MAP.get(self.ocpp_status, "A")

    def evcc_enabled(self) -> bool:
        """Wie EVCC: tatsächlichen Zustand aus OCPP-Status ableiten, sonst Sollwert."""
        if self.ocpp_status == "SuspendedEVSE":
            return False
        if self.ocpp_status in ("Charging", "SuspendedEV"):
            return True
        return self.desired_enabled

    def session_kwh(self) -> Optional[float]:
        if self.transaction_id is None or self.meter_start_wh is None or self.energy_kwh is None:
            return None
        return max(0.0, round(self.energy_kwh - self.meter_start_wh / 1000.0, 3))

    def to_api(self) -> dict:
        d = asdict(self)
        d["status"] = self.evcc_status()
        d["enabled"] = self.evcc_enabled()
        d["power"] = round(self.power_w, 1)
        d["energy"] = self.energy_kwh  # null, solange die Wallbox noch keinen Zählerstand geschickt hat
        d["session_energy"] = self.session_kwh()
        d["idtag"] = self.id_tag or self.last_id_tag
        d["charging"] = self.ocpp_status == "Charging"
        return d


class Store:
    """Hält alle Ladepunkte und speichert die relevanten Teile als JSON."""

    PERSIST_KEYS = (
        "transaction_id", "id_tag", "last_id_tag", "tx_start", "meter_start_wh",
        "desired_enabled", "max_current", "phases", "energy_kwh", "last_session_kwh",
        "vendor", "model", "firmware", "serial",
    )

    def __init__(self, data_dir: str, default_current: float):
        self.path = os.path.join(data_dir, "state.json")
        self.default_current = default_current
        self.chargers: dict[str, ChargerState] = {}
        self.next_tx_id = int(time.time()) % 1_000_000  # eindeutig auch ohne Persistenz
        self.events: deque = deque(maxlen=200)
        self._lock = threading.Lock()
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as e:
            log.warning("Datenverzeichnis %s nicht anlegbar: %s", data_dir, e)
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:  # defekte Datei nicht fatal
            log.warning("state.json nicht lesbar: %s", e)
            return
        self.next_tx_id = max(self.next_tx_id, int(raw.get("next_tx_id", 1)))
        for cp_id, data in raw.get("chargers", {}).items():
            st = ChargerState(cp_id=cp_id)
            for k in self.PERSIST_KEYS:
                if k in data:
                    setattr(st, k, data[k])
            self.chargers[cp_id] = st
        log.info("Zustand geladen: %d Ladepunkt(e)", len(self.chargers))

    def save(self):
        with self._lock:
            data = {
                "next_tx_id": self.next_tx_id,
                "chargers": {
                    cp_id: {k: getattr(st, k) for k in self.PERSIST_KEYS}
                    for cp_id, st in self.chargers.items()
                },
            }
            d = os.path.dirname(self.path)
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=d, prefix=".state")
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, self.path)
            except Exception as e:
                log.warning("state.json nicht schreibbar (%s) – Rechte von %s prüfen", e, d)
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    def get(self, cp_id: str) -> ChargerState:
        st = self.chargers.get(cp_id)
        if st is None:
            st = ChargerState(cp_id=cp_id, max_current=self.default_current)
            self.chargers[cp_id] = st
        return st

    def new_transaction_id(self) -> int:
        self.next_tx_id += 1
        if self.next_tx_id > 2_000_000_000:
            self.next_tx_id = 1
        return self.next_tx_id

    def event(self, cp_id: str, text: str):
        self.events.append({"ts": time.time(), "cp": cp_id, "text": text})
        log.info("[%s] %s", cp_id, text)
