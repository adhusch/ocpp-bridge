"""Simulierte OCPP-1.6J-Wallbox (angelehnt an Solax X3-HAC) zum Testen der Bridge.

Steuerung über eine kleine HTTP-API (Standard: Port 9999):
  POST /plug             Auto einstecken
  POST /unplug           Auto abstecken
  POST /rfid?tag=XYZ     RFID-Karte vorhalten (lokaler Start)
  GET  /state            interner Zustand

Umgebungsvariablen:
  BRIDGE_URL        ws://127.0.0.1:8887        CP_ID  SIMSOLAX01
  OCPP_PASSWORD     (optional, Basic Auth)
  LOCAL_PNC=1       Wallbox startet beim Einstecken selbst (Plug & Charge / Free-Mode)
  PNC_TAG           ID-Tag für lokalen Plug & Charge (Standard: PNC-VEHICLE)
  REJECT_PROFILES=1 SetChargingProfile ablehnen (Wallbox ohne SmartCharging)
  CAR_MAX_A=16      maximaler Ladestrom des Autos
  METER_EVERY=2     Sekunden zwischen MeterValues
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import datetime, timezone

import websockets
from aiohttp import web
from ocpp.routing import on
from ocpp.v16 import ChargePoint as CP
from ocpp.v16 import call, call_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s SIM %(message)s")
log = logging.getLogger("sim")

URL = os.environ.get("BRIDGE_URL", "ws://127.0.0.1:8887")
CP_ID = os.environ.get("CP_ID", "SIMSOLAX01")
PASSWORD = os.environ.get("OCPP_PASSWORD", "")
LOCAL_PNC = os.environ.get("LOCAL_PNC", "0") == "1"
PNC_TAG = os.environ.get("PNC_TAG", "PNC-VEHICLE")
REJECT_PROFILES = os.environ.get("REJECT_PROFILES", "0") == "1"
CAR_MAX_A = float(os.environ.get("CAR_MAX_A", "16"))
CAR_PHASES = int(os.environ.get("CAR_PHASES", "3"))
# Verhalten der echten Solax X3-HAC (FW 012.04) nachbilden: Ladeprofile in Watt,
# Schlüssel mit Tippfehler, MeterValuesSampledData readonly, TriggerMessage MeterValues abgelehnt
SOLAX_QUIRKS = os.environ.get("SOLAX_QUIRKS", "0") == "1"
METER_EVERY = float(os.environ.get("METER_EVERY", "2"))
CTRL_PORT = int(os.environ.get("CTRL_PORT", "9999"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Sim(CP):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.plugged = False
        self.status = "Available"
        self.tx_id = None
        self.limit_a = 32.0            # ohne Profil: volle Leistung
        self.phases = CAR_PHASES
        self.energy_wh = 123_456.0
        self.power_w = 0.0
        self.config = {
            "SupportedFeatureProfiles": "Core,FirmwareManagement,LocalAuthListManagement,Reservation,SmartCharging,RemoteTrigger",
            "MeterValuesSampledData": "Energy.Active.Import.Register",
            "MeterValueSampleInterval": "60",
            "ChargingScheduleAllowedChargingRateUnit": "Current",
            "NumberOfConnectors": "1",
        }
        if SOLAX_QUIRKS:
            self.config = {
                "AuthorizeRemoteTxRequests": "false",
                "ClockAlignedDataInterval": "900",
                "MeterValuesSampledData": "Current.Import,Voltage,Energy.Active.Import.Register,Frequency,Power.Active.Import,Power.Factor",
                "MeterValueSampleInterval": "60",
                "NumberOfConnectors": "1",
                "ChargeProfileMaxStackLevel": "2",
                "ChargingSchduleAllowedChargingRate": "Power",
                "MaxChargingProfilesInstalled": "1",
                "StopTransactionOnEVSideDisconnect": "true",
            }
        self.limit_w = None
        self.received = []  # Protokoll für Tests

    # ---------------------------------------------------- Zustand
    async def set_status(self, s):
        if s != self.status:
            self.status = s
            await self.call(call.StatusNotification(connector_id=1, error_code="NoError", status=s, timestamp=now()))

    def eff_current(self):
        limit = self.limit_a
        if self.limit_w is not None:  # Watt-Profil: Strom ergibt sich aus tatsächlicher Phasenzahl
            limit = self.limit_w / (230.0 * self.phases)
        return max(0.0, min(limit, CAR_MAX_A))

    async def update_charging(self):
        if self.tx_id is None:
            return
        await self.set_status("Charging" if self.eff_current() >= 6 else "SuspendedEVSE")

    async def start_local(self, tag):
        res = await self.call(call.Authorize(id_tag=tag))
        if res.id_tag_info["status"] != "Accepted":
            log.info("Tag %s abgelehnt", tag)
            return False
        return await self.start_tx(tag)

    async def start_tx(self, tag):
        res = await self.call(call.StartTransaction(connector_id=1, id_tag=tag, meter_start=int(self.energy_wh), timestamp=now()))
        if res.id_tag_info["status"] != "Accepted":
            log.info("StartTransaction abgelehnt")
            return False
        self.tx_id = res.transaction_id
        await self.update_charging()
        return True

    async def stop_tx(self, reason):
        if self.tx_id is None:
            return
        tx, self.tx_id = self.tx_id, None
        self.power_w = 0
        await self.call(call.StopTransaction(transaction_id=tx, meter_stop=int(self.energy_wh), timestamp=now(), reason=reason))
        await self.set_status("Finishing" if self.plugged else "Available")

    # ---------------------------------------------------- Befehle vom Server
    @on("GetConfiguration")
    def get_config(self, **kw):
        self.received.append("GetConfiguration")
        return call_result.GetConfiguration(configuration_key=[
            {"key": k, "readonly": SOLAX_QUIRKS and k in ("MeterValuesSampledData", "NumberOfConnectors"), "value": v}
            for k, v in self.config.items()])

    @on("ChangeConfiguration")
    def change_config(self, key, value):
        self.received.append(f"ChangeConfiguration {key}={value}")
        if SOLAX_QUIRKS and key == "MeterValuesSampledData":
            return call_result.ChangeConfiguration(status="Rejected")
        self.config[key] = value
        return call_result.ChangeConfiguration(status="Accepted")

    @on("TriggerMessage")
    def trigger(self, requested_message, **kw):
        self.received.append(f"TriggerMessage {requested_message}")
        if SOLAX_QUIRKS and requested_message == "MeterValues":
            return call_result.TriggerMessage(status="Rejected")
        async def later():
            await asyncio.sleep(0.2)
            if requested_message == "StatusNotification":
                await self.call(call.StatusNotification(connector_id=1, error_code="NoError", status=self.status, timestamp=now()))
            elif requested_message == "MeterValues":
                await self.send_meter()
        asyncio.create_task(later())
        return call_result.TriggerMessage(status="Accepted")

    @on("SetChargingProfile")
    def set_profile(self, connector_id, cs_charging_profiles):
        p = cs_charging_profiles
        sched = p["charging_schedule"]
        period = sched["charging_schedule_period"][0]
        self.received.append(f"SetChargingProfile {p['charging_profile_purpose']} {period['limit']} {period.get('number_phases')} {sched['charging_rate_unit']}")
        if REJECT_PROFILES:
            return call_result.SetChargingProfile(status="Rejected")
        if sched["charging_rate_unit"] == "W":
            self.limit_w = float(period["limit"])
        else:
            self.limit_w = None
            self.limit_a = float(period["limit"])
        if period.get("number_phases"):
            self.phases = int(period["number_phases"])
        asyncio.create_task(self.update_charging())
        return call_result.SetChargingProfile(status="Accepted")

    @on("RemoteStartTransaction")
    def remote_start(self, id_tag, **kw):
        self.received.append(f"RemoteStartTransaction {id_tag}")
        if not self.plugged or self.tx_id is not None:
            return call_result.RemoteStartTransaction(status="Rejected")
        asyncio.create_task(self.start_tx(id_tag))
        return call_result.RemoteStartTransaction(status="Accepted")

    @on("RemoteStopTransaction")
    def remote_stop(self, transaction_id):
        self.received.append(f"RemoteStopTransaction {transaction_id}")
        if transaction_id != self.tx_id:
            return call_result.RemoteStopTransaction(status="Rejected")
        asyncio.create_task(self.stop_tx("Remote"))
        return call_result.RemoteStopTransaction(status="Accepted")

    @on("UnlockConnector")
    def unlock(self, connector_id):
        self.received.append("UnlockConnector")
        return call_result.UnlockConnector(status="Unlocked")

    @on("Reset")
    def reset(self, type):
        self.received.append(f"Reset {type}")
        return call_result.Reset(status="Accepted")

    # ---------------------------------------------------- Messwerte
    async def send_meter(self):
        a = self.eff_current() if self.status == "Charging" else 0.0
        ph = self.phases
        currents = [a if i < ph else 0.0 for i in range(3)]
        sv = [
            {"measurand": "Energy.Active.Import.Register", "unit": "Wh", "value": f"{self.energy_wh:.0f}"},
            {"measurand": "Power.Active.Import", "unit": "W", "value": f"{self.power_w:.0f}"},
        ] + [
            {"measurand": "Current.Import", "unit": "A", "phase": f"L{i+1}", "value": f"{c:.1f}"} for i, c in enumerate(currents)
        ] + [
            {"measurand": "Voltage", "unit": "V", "phase": f"L{i+1}-N", "value": "230.0"} for i in range(3)
        ]
        kw = {"connector_id": 1, "meter_value": [{"timestamp": now(), "sampled_value": sv}]}
        if self.tx_id is not None:
            kw["transaction_id"] = self.tx_id
        await self.call(call.MeterValues(**kw))

    async def meter_loop(self):
        import time
        last_aligned = time.time()
        while True:
            await asyncio.sleep(METER_EVERY)
            aligned = int(self.config.get("ClockAlignedDataInterval", "0") or 0)
            if self.tx_id is None and aligned > 0 and time.time() - last_aligned >= aligned:
                last_aligned = time.time()
                await self.send_meter()  # uhr-synchrone Messwerte auch im Leerlauf
            if self.status == "Charging":
                a = self.eff_current()
                self.power_w = 230.0 * a * self.phases
                self.energy_wh += self.power_w * METER_EVERY / 3600.0
            else:
                self.power_w = 0
            if self.tx_id is not None:
                await self.send_meter()

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(30)
            await self.call(call.Heartbeat())


SIM: Sim | None = None


async def ctrl_app():
    async def plug(r):
        SIM.plugged = True
        await SIM.set_status("Preparing")
        if LOCAL_PNC:
            asyncio.create_task(SIM.start_tx(PNC_TAG))
        return web.json_response({"ok": True})

    async def unplug(r):
        SIM.plugged = False
        if SIM.tx_id is not None:
            await SIM.stop_tx("EVDisconnected")
        await SIM.set_status("Available")
        return web.json_response({"ok": True})

    async def rfid(r):
        ok = await SIM.start_local(r.query.get("tag", "RFID-1"))
        return web.json_response({"ok": ok})

    async def state(r):
        return web.json_response({k: getattr(SIM, k) for k in
                                  ("plugged", "status", "tx_id", "limit_a", "limit_w", "phases", "energy_wh", "power_w", "received")}
                                 | {"current_a": SIM.eff_current()})

    app = web.Application()
    app.router.add_post("/plug", plug)
    app.router.add_post("/unplug", unplug)
    app.router.add_post("/rfid", rfid)
    app.router.add_get("/state", state)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CTRL_PORT).start()


async def main():
    global SIM
    headers = {}
    if PASSWORD:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{CP_ID}:{PASSWORD}".encode()).decode()
    ctrl_started = False
    state = None
    while True:
        try:
            async with websockets.connect(f"{URL.rstrip('/')}/{CP_ID}", subprotocols=["ocpp1.6"],
                                          additional_headers=headers) as ws:
                sim = Sim(CP_ID, ws)
                if state:  # Zustand über Reconnects behalten
                    sim.__dict__.update({k: v for k, v in state.items() if k not in ("_connection", "id")})
                    sim._connection = ws
                SIM = sim
                if not ctrl_started:
                    await ctrl_app()
                    ctrl_started = True
                task = asyncio.create_task(sim.start())
                if state is None:
                    await sim.call(call.BootNotification(charge_point_vendor="SolaX", charge_point_model="X3-HAC-11P-SIM",
                                                         firmware_version="V9.99"))
                    await sim.call(call.StatusNotification(connector_id=1, error_code="NoError", status="Available", timestamp=now()))
                loops = [asyncio.create_task(sim.meter_loop()), asyncio.create_task(sim.heartbeat_loop())]
                state = {}
                try:
                    await task
                finally:
                    for t in loops:
                        t.cancel()
                    state = {k: v for k, v in sim.__dict__.items()
                             if k in ("plugged", "status", "tx_id", "limit_a", "limit_w", "phases", "energy_wh", "power_w", "config", "received")}
        except (OSError, websockets.exceptions.WebSocketException) as e:
            log.info("Verbindung weg (%s), neuer Versuch in 2 s", e)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
