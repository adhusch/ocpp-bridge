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

        if status in ("Available", "Unavailable"):
            self.st.power_w = 0.0
            self.st.currents = [0.0, 0.0, 0.0]

        # Plug & Charge serverseitig: eingesteckt, aber noch keine Transaktion
        if status == "Preparing" and self.st.transaction_id is None and self.cfg.auto_start:
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
        return call_result.MeterValues()

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
            sampled_ro = bool((keys.get("metervaluessampleddata") or {}).get("readonly"))
        except Exception as e:
            self._event(f"GetConfiguration fehlgeschlagen: {e!r}")

        if not sampled_ro and self.cfg.meter_measurands:
            await self._configure_measurands()
        if self.cfg.meter_interval > 0:
            await self._change_config("MeterValueSampleInterval", str(self.cfg.meter_interval))
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
        try:
            res = await self.call(call.RemoteStopTransaction(transaction_id=self.st.transaction_id))
            status = res.status if res else "Error"
        except Exception as e:
            status = f"Fehler {e!r}"
        self._event(f"RemoteStopTransaction #{self.st.transaction_id} ({reason}): {status}")
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

    def _profile(self, current: float) -> dict:
        phases = self.st.phases or None
        if self.rate_unit == "W":
            limit = float(int(self.cfg.voltage * current * self._active_phases()))
        else:
            limit = float(int(current * 10) / 10)
        period = {"start_period": 0, "limit": limit}
        if phases:
            period["number_phases"] = phases
        return {
            "charging_profile_id": self.cfg.profile_id,
            "stack_level": self.cfg.stack_level,
            "charging_profile_purpose": "TxDefaultProfile",
            "charging_profile_kind": "Absolute",
            "charging_schedule": {
                "charging_rate_unit": self.rate_unit,
                "start_schedule": now_iso(timedelta(minutes=-1)),
                "charging_schedule_period": [period],
            },
        }

    async def set_current(self, current: float) -> str:
        try:
            res = await self.call(call.SetChargingProfile(
                connector_id=self.cfg.connector_id, cs_charging_profiles=self._profile(current)))
            status = res.status if res else "Error"
        except Exception as e:
            status = f"Fehler {e!r}"
        self.st.profile_supported = status == "Accepted"
        self.st.last_profile_status = status
        unit_txt = f" (= {self._profile(current)['charging_schedule']['charging_schedule_period'][0]['limit']:g} W bei {self._active_phases()}p)" \
            if self.rate_unit == "W" else ""
        self._last_w_phases = self._active_phases()
        self._event(f"Ladeprofil {current:g} A" + (f"/{self.st.phases}p" if self.st.phases else "") + unit_txt + f": {status}")
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
        if status != "Accepted" and self.cfg.stop_fallback and self.st.transaction_id is not None:
            return await self.remote_stop(reason="Ladeprofil abgelehnt, Fallback")
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
