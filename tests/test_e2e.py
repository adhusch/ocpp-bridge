"""Ende-zu-Ende-Tests: echte Bridge-Prozesse + simulierte Wallbox.

Ausführen:  pip install -r requirements.txt pytest && pytest -v tests/
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: str | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, method=method, data=body.encode() if body is not None else None,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=40) as r:
        raw = r.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def wait_for(fn, timeout=20.0, msg="Bedingung"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noch nicht bereit
            last = e
        time.sleep(0.3)
    raise AssertionError(f"Timeout: {msg} (zuletzt: {last!r})")


class Env:
    def __init__(self, tmp: Path, bridge_env: dict, sim_env: dict):
        self.port, self.ctrl = free_port(), free_port()
        self.tmp = tmp
        self.bridge_env = {**os.environ, "PORT": str(self.port), "DATA_DIR": str(tmp / "data"),
                           "HOST": "127.0.0.1", "LOG_OCPP": "true", **bridge_env}
        self.sim_env = {**os.environ, "BRIDGE_URL": f"ws://127.0.0.1:{self.port}", "CTRL_PORT": str(self.ctrl),
                        "METER_EVERY": "1", **sim_env}
        self.bridge = self.sim = None

    def start_bridge(self):
        self.bridge = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=ROOT, env=self.bridge_env,
                                       stdout=open(self.tmp / "bridge.log", "a"), stderr=subprocess.STDOUT)
        wait_for(lambda: http("GET", self.api("/health"))["ok"], msg="Bridge startet")

    def stop_bridge(self):
        if self.bridge:
            self.bridge.terminate()
            self.bridge.wait(10)
            self.bridge = None

    def start_sim(self):
        self.sim = subprocess.Popen([sys.executable, str(ROOT / "tests/sim_wallbox.py")], env=self.sim_env,
                                    stdout=open(self.tmp / "sim.log", "a"), stderr=subprocess.STDOUT)

    def api(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def state(self, cp="_"):
        return http("GET", self.api(f"/api/{cp}/state"))

    def simstate(self):
        return http("GET", f"http://127.0.0.1:{self.ctrl}/state")

    def simctl(self, action: str, query: str = ""):
        return http("POST", f"http://127.0.0.1:{self.ctrl}/{action}{query}", "")

    def events(self):
        return [e["text"] for e in http("GET", self.api("/api/events"))]

    def close(self):
        for p in (self.sim, self.bridge):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()


@contextmanager
def running(tmp_path, bridge_env=None, sim_env=None):
    env = Env(tmp_path, bridge_env or {}, sim_env or {})
    try:
        env.start_bridge()
        env.start_sim()
        wait_for(lambda: env.state()["connected"] and any("Einrichtung abgeschlossen" in t for t in env.events()),
                 msg="Wallbox verbunden und eingerichtet")
        yield env
    finally:
        env.close()
        for name in ("bridge.log", "sim.log"):
            p = tmp_path / name
            if p.exists():
                print(f"\n===== {name} =====\n" + p.read_text()[-4000:])


# ---------------------------------------------------------------------------
def test_boot_setup_and_measurands(tmp_path):
    with running(tmp_path) as e:
        st = e.state()
        assert st["vendor"] == "SolaX" and st["status"] == "A" and st["enabled"] is False
        recv = e.simstate()["received"]
        assert any(r.startswith("ChangeConfiguration MeterValuesSampledData=Energy.Active.Import.Register,Power") for r in recv)
        assert "ChangeConfiguration MeterValueSampleInterval=10" in recv
        assert any(r.startswith("SetChargingProfile TxDefaultProfile 0.0") for r in recv)
        wait_for(lambda: e.state()["energy"] == pytest.approx(123.456), msg="Zählerstand per TriggerMessage")


def test_plug_and_charge_remote_start_and_evcc_control(tmp_path):
    with running(tmp_path) as e:
        e.simctl("plug")
        # Auto-Start: RemoteStart, Transaktion, aber 0 A solange EVCC nicht freigibt
        st = wait_for(lambda: (s := e.state())["transaction_id"] and s["ocpp_status"] == "SuspendedEVSE" and s,
                      msg="Transaktion pausiert")
        assert st["status"] == "B" and st["enabled"] is False and st["idtag"] == "evcc"
        assert "RemoteStartTransaction evcc" in e.simstate()["received"]

        # EVCC: Strom setzen und freigeben
        assert http("POST", e.api("/api/_/maxcurrent"), "10")["result"] == "stored"
        assert http("POST", e.api("/api/_/enable"), "true")["result"] == "Accepted"
        st = wait_for(lambda: (s := e.state())["status"] == "C" and s["power"] > 6000 and s, msg="lädt mit 10 A")
        assert st["enabled"] is True and st["currents"] == [10.0, 10.0, 10.0]
        assert http("GET", e.api("/api/_/status")) == "C"
        assert http("GET", e.api("/api/_/enabled")) is True  # Antworttext "true"

        # Strom ändern (EVCC-Format: ${maxcurrent:%d} als Body)
        assert http("POST", e.api("/api/_/maxcurrent"), "16")["result"] == "Accepted"
        wait_for(lambda: e.state()["power"] > 11000, msg="lädt mit 16 A")

        # Phasenumschaltung 1p
        http("POST", e.api("/api/_/phases"), "1")
        wait_for(lambda: e.simstate()["phases"] == 1 and e.state()["currents"][1] == 0, msg="1-phasig")
        assert any(r.endswith(" 16.0 1") for r in e.simstate()["received"])

        # Pause: 0 A, Transaktion bleibt bestehen
        http("POST", e.api("/api/_/enable"), "false")
        st = wait_for(lambda: (s := e.state())["ocpp_status"] == "SuspendedEVSE" and s, msg="pausiert")
        assert st["enabled"] is False and st["transaction_id"] is not None and st["status"] == "B"

        # Wieder freigeben, Energie zählt weiter
        http("POST", e.api("/api/_/enable"), "true")
        wait_for(lambda: e.state()["status"] == "C", msg="lädt wieder")
        time.sleep(2)
        assert e.state()["session_energy"] > 0

        # Abstecken
        e.simctl("unplug")
        st = wait_for(lambda: (s := e.state())["status"] == "A" and s["transaction_id"] is None and s,
                      msg="abgesteckt")
        assert st["last_session_kwh"] > 0 and st["power"] == 0


def test_enable_before_plug_starts_immediately(tmp_path):
    with running(tmp_path) as e:
        http("POST", e.api("/api/_/maxcurrent"), "8")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: (s := e.state())["status"] == "C" and s["currents"][0] == 8.0, msg="lädt sofort mit 8 A")


def test_no_autostart_remote_start_on_enable(tmp_path):
    with running(tmp_path, bridge_env={"AUTO_START": "false"}) as e:
        e.simctl("plug")
        time.sleep(2)
        st = e.state()
        assert st["transaction_id"] is None and st["status"] == "B"
        http("POST", e.api("/api/_/enable"), "true")
        wait_for(lambda: e.state()["status"] == "C", msg="RemoteStart durch EVCC-Freigabe")


def test_wallbox_side_plug_and_charge(tmp_path):
    # Wallbox startet selbst (Solax "Plug & Charge"/Free-Mode) mit Fahrzeug-Tag
    with running(tmp_path, sim_env={"LOCAL_PNC": "1", "PNC_TAG": "VID:ABC123"}) as e:
        e.simctl("plug")
        st = wait_for(lambda: (s := e.state())["transaction_id"] and s, msg="lokale Transaktion")
        assert st["idtag"] == "VID:ABC123"
        assert http("GET", e.api("/api/_/idtag")) == "VID:ABC123"
        # EVCC hat noch nicht freigegeben -> Bridge drosselt auf 0 A
        wait_for(lambda: e.state()["ocpp_status"] == "SuspendedEVSE", msg="auf 0 A gedrosselt")


def test_rfid_allowlist(tmp_path):
    with running(tmp_path, bridge_env={"ALLOWED_ID_TAGS": "GOOD1,GOOD2", "AUTO_START": "false"}) as e:
        e.simctl("plug")
        assert e.simctl("rfid", "?tag=EVIL")["ok"] is False
        assert e.simctl("rfid", "?tag=GOOD2")["ok"] is True
        wait_for(lambda: e.state()["idtag"] == "GOOD2", msg="RFID-Start")


def test_profile_rejected_falls_back_to_stop(tmp_path):
    with running(tmp_path, sim_env={"REJECT_PROFILES": "1"}) as e:
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        # Ohne Ladeprofile: Pause muss per RemoteStop gehen
        wait_for(lambda: e.state()["status"] == "C", msg="lädt")
        http("POST", e.api("/api/_/enable"), "false")
        wait_for(lambda: e.state()["transaction_id"] is None, msg="RemoteStop als Fallback")
        assert any(r.startswith("RemoteStopTransaction") for r in e.simstate()["received"])
        # Freigabe startet neue Transaktion
        http("POST", e.api("/api/_/enable"), "true")
        wait_for(lambda: e.state()["status"] == "C", msg="neu gestartet")


def test_bridge_restart_recovers_transaction(tmp_path):
    with running(tmp_path) as e:
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        tx = wait_for(lambda: e.state()["status"] == "C" and e.state()["transaction_id"], msg="lädt")
        e.stop_bridge()
        time.sleep(1)
        e.start_bridge()
        st = wait_for(lambda: (s := e.state())["connected"] and s["status"] == "C" and s, timeout=30,
                      msg="nach Neustart wieder verbunden")
        assert st["transaction_id"] == tx and st["desired_enabled"] is True
        e.simctl("unplug")
        wait_for(lambda: e.state()["last_session_kwh"] and e.state()["transaction_id"] is None, msg="sauber beendet")


def test_ocpp_basic_auth(tmp_path):
    with running(tmp_path, bridge_env={"OCPP_PASSWORD": "geheim"}, sim_env={"OCPP_PASSWORD": "geheim"}) as e:
        assert e.state()["connected"]
    env = Env(tmp_path, {"OCPP_PASSWORD": "geheim"}, {"OCPP_PASSWORD": "falsch"})
    try:
        env.start_bridge()
        env.start_sim()
        time.sleep(4)
        assert http("GET", env.api("/api/chargers")) == [] or not env.state()["connected"]
    finally:
        env.close()


def test_api_token(tmp_path):
    env = Env(tmp_path, {"API_TOKEN": "t0k"}, {})
    try:
        env.start_bridge()
        with pytest.raises(urllib.error.HTTPError) as ex:
            http("GET", env.api("/api/chargers"))
        assert ex.value.code == 401
        assert http("GET", env.api("/api/chargers"), headers={"Authorization": "Bearer t0k"}) == []
    finally:
        env.close()
