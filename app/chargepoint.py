"""OCPP 1.6J Central System: ein Handler pro verbundener Wallbox."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call, call_result

from .config import Config
from .state import ChargerState, Store

log = logging.getLogger("cp")

STRICT = False  # eingehende Nachrichten tolerant behandeln (reale Wallboxen weichen gern ab)

# Profilvarianten, die nacheinander probiert werden, wenn die Wallbox ein Limit nicht einhält.
# unit: "auto" = wie von der Wallbox gemeldet, "flip" = die jeweils andere Einheit (A↔W)
VARIANTS = [
    {"purpose": "TxDefaultProfile", "unit": "auto", "connector": "cfg"},
    {"purpose": "TxProfile", "unit": "auto", "connector": "cfg"},
    {"purpose": "TxDefaultProfile", "unit": "flip", "connector": "cfg"},
    {"purpose": "TxProfile", "unit": "flip", "connector": "cfg"},
    {"purpose": "ChargePointMaxProfile", "unit": "auto", "connector": 0},
    {"purpose": "ChargePointMaxProfile", "unit": "flip", "connector": 0},
]


def now_iso(offset: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat(timespec="seconds").replace("+00:00", "Z")


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ChargePoint(OcppChargePoint):
    def __init__(self, cp_id: str, connection, cfg: Config, store: Store):
        super().__init__(cp_id, connection, response_timeout=30)
        self.cfg = cfg
        self.store = store
        self.st: ChargerState = store.get(cp_id)
        self.rate_unit = cfg.rate_unit.upper() if cfg.rate_unit.upper() in ("A", "W") else "A"
        self.supports_trigger = True
        self._setup_task: Optional[asyncio.Task] = None
        self._setup_done = False
        self._last_remote_start = 0.0
        self._last_w_phases = 0
        self.max_stack_level = 0       # aus ChargeProfileMaxStackLevel
        # Durchsetzung: zuletzt gesetztes Limit und Messungen danach
        self._target: Optional[float] = None
        self._target_ts = 0.0
        self._violations = 0
        self._enforcing = False
        self._last_enforce_stop = 0.0
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ helpers
    def _spawn(self, coro, delay: float = 0.0):
        async def runner():
            try:
                if delay:
                    await asyncio.sleep(delay)
            except asyncio.CancelledError:
                coro.close()  # nie gestartete Coroutine sauber verwerfen
                raise
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # nie den Handler-Loop abschießen
                log.warning("[%s] Hintergrundaufgabe fehlgeschlagen: %r", self.id, e)

        t = asyncio.create_task(runner())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _touch(self):
        self.st.last_seen = time.time()

    def _event(self, text: str):
        self.store.event(self.id, text)

    def schedule_setup(self, delay: float = 2.0, force: bool = False):
        if self._setup_task and not self._setup_task.done():
            if not force:
                return
            self._setup_task.cancel()
        self._setup_task = self._spawn(self.setup(), delay)

    def cancel_tasks(self):
        for t in list(self._tasks):
            t.cancel()

    # ------------------------------------------------- eingehende Nachrichten
    @on("BootNotification", skip_schema_validation=not STRICT)
    def on_boot_notification(self, charge_point_vendor=None, charge_point_model=None, **kw):
        self._touch()
        self.st.vendor = charge_point_vendor or ""
        self.st.model = charge_point_model or ""
        self.st.firmware = kw.get("firmware_version", "") or ""
        self.st.serial = kw.get("charge_point_serial_number", "") or kw.get("charge_box_serial_number", "") or ""
        self._event(f"BootNotification {self.st.vendor} {self.st.model} FW {self.st.firmware}")
        self._setup_done = False
        self.schedule_setup(2.0, force=True)
        self.store.save()
        return call_result.BootNotification(
            current_time=now_iso(), interval=self.cfg.heartbeat_interval, status="Accepted"
        )

    @on("Heartbeat", skip_schema_validation=not STRICT)
    def on_heartbeat(self, **kw):
        self._touch()
        return call_result.Heartbeat(current_time=now_iso())

    @on("StatusNotification", skip_schema_validation=not STRICT)
    def on_status_notification(self, connector_id=0, error_code="NoError", status="", **kw):
        self._touch()
        info = kw.get("info") or kw.get("vendor_error_code") or ""
        if int(connector_id) == 0:
            # Status der gesamten Station: nur Fehler übernehmen
            if status == "Faulted" or error_code != "NoError":
                self.st.error_code, self.st.status_info = error_code, info
                self._event(f"Station: {status} ({error_code}) {info}".strip())
            return call_result.StatusNotification()
        if int(connector_id) != self.cfg.connector_id:
            return call_result.StatusNotification()

        old = self.st.ocpp_status
        self.st.ocpp_status = status
        self.st.error_code, self.st.status_info = error_code, info
        if old != status:
            self._event(f"Status {old} → {status}" + (f" ({error_code})" if error_code != "NoError" else ""))

        if status == "Available" and self.st.transaction_id is not None:
            # Auto abgesteckt: Transaktion ist vorbei, auch wenn StopTransaction noch fehlt
            self._finish_transaction(None, "Fahrzeug getrennt")

        if status in ("Available", "Unavailable", "Preparing", "Finishing"):
            # in diesen Zuständen fließt kein Strom; ohne Transaktion kommen keine Messwerte mehr
            self.st.power_w = 0.0
            self.st.currents = [0.0, 0.0, 0.0]

        # Eingesteckt, aber noch keine Transaktion: starten, wenn EVCC schon freigegeben hat
        # (oder immer, wenn AUTO_START gesetzt ist)
        if status == "Preparing" and self.st.transaction_id is None:
            if self.st.desired_enabled:
                self._spawn(self.remote_start(reason="eingesteckt, EVCC hat freigegeben"), 0.5)
            elif self.cfg.auto_start:
                self._spawn(self.remote_start(reason="Plug & Charge (Auto-Start)"), 0.5)
        return call_result.StatusNotification()

    def _authorize(self, id_tag: str) -> str:
        if not self.cfg.allowed_id_tags or id_tag == self.cfg.id_tag or id_tag in self.cfg.allowed_id_tags:
            return "Accepted"
        return "Invalid"

    @on("Authorize", skip_schema_validation=not STRICT)
    def on_authorize(self, id_tag, **kw):
        self._touch()
        status = self._authorize(id_tag)
        self.st.last_id_tag = id_tag
        self._event(f"Authorize {id_tag}: {status}")
        return call_result.Authorize(id_tag_info={"status": status})

    @on("StartTransaction", skip_schema_validation=not STRICT)
    def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp=None, **kw):
        self._touch()
        status = self._authorize(id_tag)
        tx_id = self.store.new_transaction_id()
        if status == "Accepted":
            self.st.transaction_id = tx_id
            self.st.id_tag = id_tag
            self.st.last_id_tag = id_tag
            self.st.tx_start = timestamp or now_iso()
            self.st.meter_start_wh = _f(meter_start)
            if self.st.energy_kwh is None and self.st.meter_start_wh is not None:
                self.st.energy_kwh = self.st.meter_start_wh / 1000.0
            self._event(f"StartTransaction #{tx_id} (Tag {id_tag}, Zähler {meter_start} Wh)")
            # Sollwert sofort durchsetzen (z. B. 0 A, wenn EVCC gerade pausiert)
            self._spawn(self.apply(), 0.5)
        else:
            self._event(f"StartTransaction mit unbekanntem Tag {id_tag} abgelehnt")
        self.store.save()
        return call_result.StartTransaction(transaction_id=tx_id, id_tag_info={"status": status})

    def _finish_transaction(self, meter_stop_wh: Optional[float], reason: str):
        tx = self.st.transaction_id
        if meter_stop_wh is not None and self.st.meter_start_wh is not None:
            self.st.last_session_kwh = round(max(0.0, meter_stop_wh - self.st.meter_start_wh) / 1000.0, 3)
            self.st.energy_kwh = meter_stop_wh / 1000.0
        elif self.st.session_kwh() is not None:
            self.st.last_session_kwh = self.st.session_kwh()
        self.st.transaction_id = None
        self.st.id_tag = ""
        self.st.meter_start_wh = None
        self.st.tx_start = None
        self.st.power_w = 0.0
        self.st.currents = [0.0, 0.0, 0.0]
        self._event(f"Transaktion #{tx} beendet ({reason}), geladen: {self.st.last_session_kwh} kWh")
        self.store.save()

    @on("StopTransaction", skip_schema_validation=not STRICT)
    def on_stop_transaction(self, transaction_id, meter_stop, timestamp=None, **kw):
        self._touch()
        reason = kw.get("reason", "Local")
        if self.st.transaction_id in (None, transaction_id):
            if self.st.transaction_id is None:
                self._event(f"StopTransaction #{transaction_id} (bereits beendet)")
                ms = _f(meter_stop)
                if ms is not None:
                    self.st.energy_kwh = ms / 1000.0
                self.store.save()
            else:
                self._finish_transaction(_f(meter_stop), reason)
        else:
            self._event(f"StopTransaction für fremde Transaktion #{transaction_id}")
        return call_result.StopTransaction(id_tag_info={"status": "Accepted"})

    @on("MeterValues", skip_schema_validation=not STRICT)
    def on_meter_values(self, connector_id, meter_value, **kw):
        self._touch()
        cid = int(connector_id)
        if cid not in (0, self.cfg.connector_id):
            return call_result.MeterValues()
        tx = kw.get("transaction_id")
        if tx is not None and self.st.transaction_id is None and cid == self.cfg.connector_id \
                and self.st.ocpp_status in ("Charging", "SuspendedEV", "SuspendedEVSE"):
            # Transaktion nach Neustart der Bridge wiedergefunden
            self.st.transaction_id = int(tx)
            self._event(f"Laufende Transaktion #{tx} wiederhergestellt")
        for mv in meter_value or []:
            self._parse_sampled(mv.get("sampled_value") or [], connector0=(cid == 0))
        self.st.meter_ts = time.time()
        # Watt-Profile: wenn das Auto mit anderer Phasenzahl lädt als angenommen, Limit neu rechnen
        if (self.rate_unit == "W" and self.st.desired_enabled and cid == self.cfg.connector_id
                and self.st.ocpp_status == "Charging"
                and self._last_w_phases and self._active_phases() != self._last_w_phases):
            self._event(f"Auto lädt {self._active_phases()}-phasig – Watt-Limit wird angepasst")
            self._last_w_phases = self._active_phases()
            self._spawn(self.set_current(self.st.max_current))
        if cid == self.cfg.connector_id:
            self._check_limit()
        return call_result.MeterValues()

    # ------------------------------------------------- Durchsetzung des Limits
    def _check_limit(self):
        """Prüft nach jedem Messwert, ob die Wallbox das gesetzte Limit einhält."""
        if not self.cfg.enforce or self._target is None or self._enforcing:
            return
        if time.time() - self._target_ts < self.cfg.enforce_grace:
            return
        if self.st.ocpp_status not in ("Charging", "SuspendedEV", "SuspendedEVSE"):
            self._violations = 0
            return
        target = self._target
        measured = max(self.st.currents or [0.0])
        allowed = target + self.cfg.enforce_tolerance if target > 0 else 1.0

        if self.st.profiles_ignored:
            # Profile wirken nicht: Pause nur noch per RemoteStop möglich
            if (target == 0 and measured > allowed and self.st.transaction_id is not None
                    and self.cfg.stop_fallback and time.time() - self._last_enforce_stop > 60):
                self._last_enforce_stop = time.time()
                self._spawn(self.remote_stop(reason="Pause, Wallbox ignoriert Ladeprofile"))
            return

        if measured > allowed:
            self._violations += 1
            if self._violations >= 2:
                self._violations = 0
                self._enforcing = True
                self._spawn(self._escalate(target, measured))
            return

        self._violations = 0
        if (measured > 1.0 or target == 0) and self.st.profile_effective is not True:
            self.st.profile_effective = True
            self._event(f"Limit {target:g} A wird eingehalten (gemessen {measured:.1f} A, "
                        f"Variante {self.st.profile_variant + 1}: {self._variant_desc()})")
            self.store.save()

    def _variant_desc(self, n: Optional[int] = None) -> str:
        v = VARIANTS[self.st.profile_variant if n is None else n]
        conn = self.cfg.connector_id if v["connector"] == "cfg" else v["connector"]
        return f"{v['purpose']}, {self._unit_for(v)}, Connector {conn}, Stack {self._stack_level()}"

    async def _clear_profiles(self):
        try:
            res = await self.call(call.ClearChargingProfile())
            self._event(f"ClearChargingProfile: {res.status if res else 'Error'}")
        except Exception as e:
            self._event(f"ClearChargingProfile fehlgeschlagen: {e!r}")

    async def _escalate(self, target: float, measured: float):
        try:
            n = self.st.profile_variant
            if n + 1 < len(VARIANTS):
                self.st.profile_variant = n + 1
                self.st.profile_effective = None
                self._event(f"Wallbox hält Limit {target:g} A nicht ein (gemessen {measured:.1f} A) → "
                            f"probiere Variante {n + 2}/{len(VARIANTS)}: {self._variant_desc()}")
                self.store.save()
                await self._clear_profiles()
                await self.set_current(target)
                return
            # alle Varianten durch: Profile wirken nicht
            self.st.profile_variant = 0
            self.st.profiles_ignored = True
            self.st.profile_effective = False
            self.store.save()
            self._event("WARNUNG: Wallbox ignoriert alle Ladeprofil-Varianten – Strombegrenzung per OCPP "
                        "nicht möglich. Pausieren erfolgt ab jetzt per RemoteStopTransaction.")
            await self._clear_profiles()
            if target == 0 and self.st.transaction_id is not None and self.cfg.stop_fallback:
                self._last_enforce_stop = time.time()
                await self.remote_stop(reason="Pause, Wallbox ignoriert Ladeprofile")
        finally:
            self._enforcing = False

    def reset_profile_detection(self):
        self.st.profile_variant = 0
        self.st.profiles_ignored = False
        self.st.profile_effective = None
        self.store.save()

    def _parse_sampled(self, samples: list, connector0: bool):
        power_total = None
        power_phases = [None, None, None]
        currents = [None, None, None]
        voltages = [None, None, None]
        for s in samples:
            val = _f(s.get("value"))
            if val is None:
                continue
            measurand = s.get("measurand") or "Energy.Active.Import.Register"
            unit = (s.get("unit") or "").lower()
            phase = (s.get("phase") or "")
            idx = {"L1": 0, "L2": 1, "L3": 2}.get(phase[:2]) if phase else None
            if measurand == "Energy.Active.Import.Register":
                if phase:  # nur Summenzähler
                    continue
                kwh = val if unit == "kwh" else val / 1000.0
                if not connector0 or self.st.energy_kwh is None:
                    self.st.energy_kwh = round(kwh, 4)
            elif connector0:
                continue  # Stationszähler nur für Energie verwenden
            elif measurand == "Power.Active.Import":
                w = val * 1000.0 if unit == "kw" else val
                if idx is None:
                    power_total = w
                else:
                    power_phases[idx] = w
            elif measurand == "Current.Import":
                if idx is None:
                    if phase == "N":
                        continue
                    currents[0] = val if currents[0] is None else currents[0]
                else:
                    currents[idx] = val
            elif measurand == "Voltage":
                if idx is None:
                    voltages[0] = val if voltages[0] is None else voltages[0]
                else:
                    voltages[idx] = val
            elif measurand == "SoC":
                self.st.soc = val
        if connector0:
            return
        if power_total is None and any(p is not None for p in power_phases):
            power_total = sum(p or 0.0 for p in power_phases)
        if power_total is not None:
            self.st.power_w = power_total
        if any(c is not None for c in currents):
            self.st.currents = [round(c or 0.0, 2) for c in currents]
        if any(v is not None for v in voltages):
            self.st.voltages = [round(v or 0.0, 1) for v in voltages]

    @on("DataTransfer", skip_schema_validation=not STRICT)
    def on_data_transfer(self, vendor_id, **kw):
        self._touch()
        self._event(f"DataTransfer von {vendor_id}: {kw.get('message_id', '')} {str(kw.get('data', ''))[:120]}")
        return call_result.DataTransfer(status="Accepted")

    @on("FirmwareStatusNotification", skip_schema_validation=not STRICT)
    def on_fw_status(self, status, **kw):
        self._event(f"Firmware-Status: {status}")
        return call_result.FirmwareStatusNotification()

    @on("DiagnosticsStatusNotification", skip_schema_validation=not STRICT)
    def on_diag_status(self, status, **kw):
        self._event(f"Diagnose-Status: {status}")
        return call_result.DiagnosticsStatusNotification()

    @on("SecurityEventNotification", skip_schema_validation=not STRICT)
    def on_security_event(self, type=None, **kw):
        self._event(f"Security-Event: {type}")
        return call_result.SecurityEventNotification()

    # ------------------------------------------------- Einrichtung nach Verbindung
    async def setup(self):
        if self._setup_done:
            return
        self._event("Einrichtung startet")
        sampled_ro = False
        try:
            res = await self.call(call.GetConfiguration())
            keys = {k.get("key", "").lower(): k for k in (res.configuration_key or [])} if res else {}
            features = (keys.get("supportedfeatureprofiles") or {}).get("value") or ""
            self.st.features = features
            if features:
                self.supports_trigger = "remotetrigger" in features.lower()
                if "smartcharging" not in features.lower():
                    self._event("Warnung: Wallbox meldet kein SmartCharging-Profil")
            # Standard-Schlüssel ChargingScheduleAllowedChargingRateUnit; Solax schreibt
            # "ChargingSchduleAllowedChargingRate" (sic!) – daher tolerant suchen
            unit = next((v.get("value") or "" for k, v in keys.items()
                         if "allowedchargingrate" in k), "")
            u = unit.lower()
            if self.cfg.rate_unit.lower() == "auto":
                if u and "current" not in u and u != "a" and ("power" in u or u == "w"):
                    self.rate_unit = "W"
                    self._event(f"Wallbox meldet Ladeprofil-Einheit '{unit}' → Ladeprofile in Watt")
                else:
                    self.rate_unit = "A"
            else:
                self._event(f"Ladeprofil-Einheit fest auf {self.rate_unit} (RATE_UNIT)")
            msl = (keys.get("chargeprofilemaxstacklevel") or {}).get("value")
            if msl is not None and str(msl).strip().isdigit():
                self.max_stack_level = int(msl)
            self._event(f"Ladeprofile: Stack Level {self._stack_level()}"
                        + (" (höchste Stufe der Wallbox, wie EVCC)" if self.cfg.stack_level is None else " (STACK_LEVEL)")
                        + f", Variante {self.st.profile_variant + 1}"
                        + (" – Wallbox ignorierte Profile bisher, Pause per RemoteStop" if self.st.profiles_ignored else ""))
            sampled_ro = bool((keys.get("metervaluessampleddata") or {}).get("readonly"))
        except Exception as e:
            self._event(f"GetConfiguration fehlgeschlagen: {e!r}")

        if not sampled_ro and self.cfg.meter_measurands:
            await self._configure_measurands()
        if self.cfg.meter_interval > 0:
            await self._change_config("MeterValueSampleInterval", str(self.cfg.meter_interval))
        if self.cfg.meter_aligned_interval > 0:
            # liefert den Zählerstand auch ohne Ladevorgang (manche Wallboxen, z. B. Solax,
            # lehnen TriggerMessage MeterValues im Leerlauf ab)
            await self._change_config("ClockAlignedDataInterval", str(self.cfg.meter_aligned_interval))
        await self._change_config("WebSocketPingInterval", "30", quiet=True)

        if self.supports_trigger:
            await self.trigger("StatusNotification")
            await self.trigger("MeterValues")

        await self.apply()
        self._setup_done = True
        self._event("Einrichtung abgeschlossen")

    async def _change_config(self, key: str, value: str, quiet: bool = False) -> str:
        try:
            res = await self.call(call.ChangeConfiguration(key=key, value=value))
            status = res.status if res else "Error"
        except Exception as e:
            status = f"Fehler {e!r}"
        if not quiet:
            self._event(f"ChangeConfiguration {key}={value}: {status}")
        return status

    async def _configure_measurands(self):
        wanted = [m.strip() for m in self.cfg.meter_measurands.split(",") if m.strip()]
        status = await self._change_config("MeterValuesSampledData", ",".join(wanted))
        if status in ("Accepted", "RebootRequired"):
            return
        accepted: list[str] = []
        for m in wanted:  # einzeln herantasten, wie EVCC
            if await self._change_config("MeterValuesSampledData", ",".join(accepted + [m]), quiet=True) in ("Accepted", "RebootRequired"):
                accepted.append(m)
        self._event(f"Messgrößen akzeptiert: {','.join(accepted) or 'keine'}")

    # ------------------------------------------------- Steuerbefehle
    async def remote_start(self, reason: str = "manuell", force: bool = False) -> str:
        # Doppelte Starts vermeiden (Auto-Start + EVCC-Freigabe fast gleichzeitig)
        if self.st.transaction_id is not None:
            return "AlreadyRunning"
        if not force and time.time() - self._last_remote_start < 10:
            return "Debounced"
        self._last_remote_start = time.time()
        try:
            res = await self.call(call.RemoteStartTransaction(id_tag=self.cfg.id_tag, connector_id=self.cfg.connector_id))
            status = res.status if res else "Error"
        except Exception as e:
            status = f"Fehler {e!r}"
        self._event(f"RemoteStartTransaction ({reason}): {status}")
        return status

    async def remote_stop(self, reason: str = "manuell") -> str:
        if self.st.transaction_id is None:
            return "NoTransaction"
        tx = self.st.transaction_id
        try:
            res = await self.call(call.RemoteStopTransaction(transaction_id=tx))
            status = res.status if res else "Error"
        except Exception as e:
            status = f"Fehler {e!r}"
        self._event(f"RemoteStopTransaction #{tx} ({reason}): {status}")
        return status

    def _active_phases(self) -> int:
        """Phasen für die A→W-Umrechnung: Vorgabe von EVCC, sonst gemessen, sonst 3."""
        if self.st.phases:
            return self.st.phases
        if self.st.ocpp_status == "Charging":
            n = sum(1 for c in self.st.currents if (c or 0) > 1.0)
            if n in (1, 2):
                return n
        return 3

    def _stack_level(self) -> int:
        return self.cfg.stack_level if self.cfg.stack_level is not None else self.max_stack_level

    def _unit_for(self, variant: dict) -> str:
        if variant["unit"] == "flip":
            return "A" if self.rate_unit == "W" else "W"
        return self.rate_unit

    def _variant_call(self, current: float) -> tuple[int, dict, str]:
        """(connectorId, Profil, Einheit) für die aktuelle Variante."""
        v = VARIANTS[self.st.profile_variant % len(VARIANTS)]
        unit = self._unit_for(v)
        purpose = v["purpose"]
        connector = self.cfg.connector_id if v["connector"] == "cfg" else v["connector"]
        # TxProfile braucht eine laufende Transaktion; vorher als Default-Profil setzen
        if purpose == "TxProfile" and self.st.transaction_id is None:
            purpose = "TxDefaultProfile"
        phases = self.st.phases or None
        if unit == "W":
            limit = float(int(self.cfg.voltage * current * self._active_phases()))
        else:
            limit = float(int(current * 10) / 10)
        period = {"start_period": 0, "limit": limit}
        if phases:
            period["number_phases"] = phases
        profile = {
            "charging_profile_id": self.cfg.profile_id,
            "stack_level": self._stack_level(),
            "charging_profile_purpose": purpose,
            "charging_profile_kind": "Absolute",
            "charging_schedule": {
                "charging_rate_unit": unit,
                "start_schedule": now_iso(timedelta(minutes=-1)),
                "charging_schedule_period": [period],
            },
        }
        if purpose == "TxProfile":
            profile["transaction_id"] = self.st.transaction_id
        return connector, profile, unit

    def _profile(self, current: float) -> dict:
        return self._variant_call(current)[1]

    async def set_current(self, current: float) -> str:
        connector, profile, unit = self._variant_call(current)
        try:
            res = await self.call(call.SetChargingProfile(connector_id=connector, cs_charging_profiles=profile))
            status = res.status if res else "Error"
        except Exception as e:
            status = f"Fehler {e!r}"
        self.st.profile_supported = status == "Accepted"
        self.st.last_profile_status = status
        limit = profile["charging_schedule"]["charging_schedule_period"][0]["limit"]
        unit_txt = f" (= {limit:g} W bei {self._active_phases()}p)" if unit == "W" else ""
        self._last_w_phases = self._active_phases()
        if self._target is None or abs(self._target - current) >= 0.1:
            self.st.profile_effective = None if not self.st.profiles_ignored else False
        self._target = current
        self._target_ts = time.time()
        self._violations = 0
        self._event(f"Ladeprofil {current:g} A" + (f"/{self.st.phases}p" if self.st.phases else "") + unit_txt
                    + f" [{profile['charging_profile_purpose']}, Stack {profile['stack_level']}, Connector {connector}]"
                    + f": {status}")
        return status

    async def apply(self) -> str:
        """Sollzustand (enabled, Strom, Phasen) auf die Wallbox übertragen."""
        if self.st.desired_enabled:
            status = await self.set_current(self.st.max_current)
            if (self.st.transaction_id is None and self.st.ocpp_status in ("Preparing", "Finishing", "SuspendedEVSE")):
                await self.remote_start(reason="EVCC Freigabe", force=True)
            return status

        if self.cfg.disable_mode == "stop":
            await self.set_current(0)
            return await self.remote_stop(reason="EVCC Pause") if self.st.transaction_id else "Accepted"

        status = await self.set_current(0)
        if self.st.transaction_id is not None and self.cfg.stop_fallback:
            if status != "Accepted":
                return await self.remote_stop(reason="Ladeprofil abgelehnt, Fallback")
            if self.st.profiles_ignored:
                self._last_enforce_stop = time.time()
                return await self.remote_stop(reason="Pause, Wallbox ignoriert Ladeprofile")
        return status

    async def trigger(self, message: str) -> str:
        try:
            kwargs = {"requested_message": message}
            if message in ("StatusNotification", "MeterValues"):
                kwargs["connector_id"] = self.cfg.connector_id
            res = await self.call(call.TriggerMessage(**kwargs))
            return res.status if res else "Error"
        except Exception as e:
            return f"Fehler {e!r}"

    async def unlock(self) -> str:
        res = await self.call(call.UnlockConnector(connector_id=self.cfg.connector_id))
        status = res.status if res else "Error"
        self._event(f"UnlockConnector: {status}")
        return status

    async def reset(self, kind: str = "Soft") -> str:
        res = await self.call(call.Reset(type=kind))
        status = res.status if res else "Error"
        self._event(f"Reset {kind}: {status}")
        return status

    async def get_configuration(self) -> list:
        res = await self.call(call.GetConfiguration())
        return list(res.configuration_key or []) if res else []

    async def change_configuration(self, key: str, value: str) -> str:
        return await self._change_config(key, value)
