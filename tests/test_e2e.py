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
    with running(tmp_path, bridge_env={"AUTO_START": "true"}) as e:
        e.simctl("plug")
        # Auto-Start: RemoteStart, Transaktion, aber 0 A solange EVCC nicht freigibt
        st = wait_for(lambda: (s := e.state())["transaction_id"] and s["ocpp_status"] == "SuspendedEVSE" and s,
                      msg="Transaktion pausiert")
        assert st["status"] == "B" and st["enabled"] is False and st["idtag"] == "evcc"
        assert "RemoteStartTransaction evcc" in e.simstate()["received"]

        # EVCC: Strom setzen und freigeben
        assert http("POST", e.api("/api/_/maxcurrent"), "10")["result"] == "stored"
        assert http("POST", e.api("/api/_/enable"), "true")["result"].startswith("queued")
        st = wait_for(lambda: (s := e.state())["status"] == "C" and s["power"] > 6000 and s, msg="lädt mit 10 A")
        assert st["enabled"] is True and st["currents"] == [10.0, 10.0, 10.0]
        assert http("GET", e.api("/api/_/status")) == "C"
        assert http("GET", e.api("/api/_/enabled")) is True  # Antworttext "true"

        # Strom ändern (EVCC-Format: ${maxcurrent:%d} als Body)
        assert http("POST", e.api("/api/_/maxcurrent"), "16")["result"].startswith("queued")
        wait_for(lambda: e.state()["power"] > 11000, msg="lädt mit 16 A")

        # Phasenumschaltung 1p
        http("POST", e.api("/api/_/phases"), "1")
        wait_for(lambda: e.simstate()["phases"] == 1 and e.state()["currents"][1] == 0, msg="1-phasig")
        assert any(r.endswith(" 16.0 1 A") for r in e.simstate()["received"])

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


def test_connect_without_id_in_path(tmp_path):
    # manche Wallboxen verbinden auf ws://host:8887/ ohne eigene ID
    with running(tmp_path, sim_env={"CP_ID": ""}) as e:
        assert e.state("wallbox")["connected"]
        assert "OCPP-Verbindungsversuch" in (tmp_path / "bridge.log").read_text()


def test_solax_quirks_watt_profiles(tmp_path):
    # Verhalten der echten Solax X3-HAC FW 012.04 (aus dem Log des Nutzers)
    with running(tmp_path, bridge_env={"METER_ALIGNED_INTERVAL": "2"}, sim_env={"SOLAX_QUIRKS": "1"}) as e:
        assert any("Ladeprofile in Watt" in t for t in e.events())
        recv = e.simstate()["received"]
        assert not any(r.startswith("ChangeConfiguration MeterValuesSampledData") for r in recv)  # readonly
        assert "ChangeConfiguration ClockAlignedDataInterval=2" in recv
        # Zählerstand kommt über uhr-synchrone Messwerte auch ohne Ladevorgang
        wait_for(lambda: (s := e.state())["energy_known"] and s["energy"] == pytest.approx(123.456),
                 msg="Zählerstand im Leerlauf")
        http("POST", e.api("/api/_/maxcurrent"), "10")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: (s := e.state())["status"] == "C" and s["currents"][0] == 10.0, msg="lädt mit 10 A")
        sim = e.simstate()
        assert sim["limit_w"] == 6900.0 and sim["current_a"] == 10.0
        http("POST", e.api("/api/_/enable"), "false")
        wait_for(lambda: e.state()["ocpp_status"] == "SuspendedEVSE", msg="0 W pausiert")


def test_solax_watt_profile_single_phase_car(tmp_path):
    # Auto lädt nur 1-phasig: 10 A bei angenommenen 3 Phasen wären 30 A auf L1 → Bridge muss nachregeln
    with running(tmp_path, bridge_env={"RATE_UNIT": "W", "PHASE_DETECT": "true"}, sim_env={"CAR_PHASES": "1"}) as e:
        http("POST", e.api("/api/_/maxcurrent"), "10")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: e.simstate()["limit_w"] == 2300.0 and e.state()["currents"][0] == 10.0,
                 msg="Watt-Limit auf 1 Phase umgerechnet")
        assert any("1-phasig" in t for t in e.events())


def test_no_charging_before_evcc_enables(tmp_path):
    # Realer Fall 14.10.: Auto eingesteckt, EVCC im PV-Modus ohne Überschuss → darf NICHT laden
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1", "CAR_MAX_A": "12"}) as e:
        e.simctl("plug")
        time.sleep(3)
        st, sim = e.state(), e.simstate()
        assert st["status"] == "B" and st["transaction_id"] is None
        assert sim["power_w"] == 0 and not any(r.startswith("RemoteStartTransaction") for r in sim["received"])
        http("POST", e.api("/api/_/maxcurrent"), "6")
        http("POST", e.api("/api/_/enable"), "true")
        wait_for(lambda: (s := e.state())["status"] == "C" and s["currents"][0] == 6.0, msg="lädt erst nach Freigabe mit 6 A")


def test_stack_level_from_wallbox(tmp_path):
    # Solax meldet ChargeProfileMaxStackLevel=2; ein internes Profil auf Stufe 1 überstimmt Stufe 0
    with running(tmp_path, bridge_env={"ENFORCE_GRACE": "3"},
                 sim_env={"SOLAX_QUIRKS": "1", "MIN_STACK": "1", "CAR_MAX_A": "12"}) as e:
        assert any("Stack Level 2" in t for t in e.events())
        http("POST", e.api("/api/_/maxcurrent"), "6")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: (s := e.state())["status"] == "C" and s["currents"][0] == 6.0, msg="6 A mit Stack 2")
        wait_for(lambda: e.state()["profile_effective"] is True, msg="als wirksam erkannt")
        assert e.state()["profile_variant"] == 0


def test_escalates_to_working_profile_variant(tmp_path):
    # Wallbox nimmt TxDefaultProfile an, hält es aber nicht ein; nur TxProfile in W wirkt
    with running(tmp_path, bridge_env={"ENFORCE_GRACE": "3"},
                 sim_env={"SOLAX_QUIRKS": "1", "HONOR_PROFILES": "TxProfile/W", "CAR_MAX_A": "12"}) as e:
        http("POST", e.api("/api/_/maxcurrent"), "6")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: e.state()["currents"][0] == 12.0, msg="lädt zunächst ungebremst")
        st = wait_for(lambda: (s := e.state())["currents"][0] == 6.0 and s["profile_effective"] and s,
                      timeout=40, msg="Variante TxProfile greift")
        assert st["profile_variant"] == 1
        assert any("probiere Variante 2" in t for t in e.events())
        # Pause per Profil funktioniert jetzt auch
        http("POST", e.api("/api/_/enable"), "false")
        wait_for(lambda: e.state()["ocpp_status"] == "SuspendedEVSE", msg="pausiert per Profil")
        assert e.state()["transaction_id"] is not None


def test_all_profiles_ignored_pause_stops_transaction(tmp_path):
    with running(tmp_path, bridge_env={"ENFORCE_GRACE": "2"},
                 sim_env={"SOLAX_QUIRKS": "1", "HONOR_PROFILES": "none", "CAR_MAX_A": "12"}) as e:
        http("POST", e.api("/api/_/maxcurrent"), "6")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: e.state()["profiles_ignored"], timeout=90, msg="alle Varianten durchprobiert")
        assert e.state()["status"] == "C"  # laden geht weiter, nur eben ungebremst
        http("POST", e.api("/api/_/enable"), "false")
        wait_for(lambda: (s := e.state())["transaction_id"] is None and s["power"] == 0,
                 msg="Pause per RemoteStop")
        assert any(r.startswith("RemoteStopTransaction") for r in e.simstate()["received"])
        # erneute Freigabe startet wieder
        http("POST", e.api("/api/_/enable"), "true")
        wait_for(lambda: e.state()["status"] == "C", msg="neu gestartet")


def test_solax_rejects_above_rated_power(tmp_path):
    # Realer Fall 14.10.: 16 A = 11040 W wird von der 11-kW-Solax abgelehnt → Auto lud gar nicht
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1"}) as e:
        http("POST", e.api("/api/_/maxcurrent"), "16")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        st = wait_for(lambda: (s := e.state())["status"] == "C" and s["currents"][0] > 15 and s,
                      msg="lädt mit gekapptem Limit")
        assert st["max_power_w"] == 11000 and e.simstate()["limit_w"] == 11000.0
        assert any("11000 W angenommen" in t for t in e.events())
        # gemerkt: nächste Änderung geht direkt mit Kappung durch
        http("POST", e.api("/api/_/maxcurrent"), "15")
        http("POST", e.api("/api/_/maxcurrent"), "16")
        assert sum("abgelehnt: 11040" in r for r in e.simstate()["received"]) == 1


def test_finds_exact_max_power(tmp_path):
    # Reale Solax 14.10.: lehnt 11040 und 11000 W ab, ungebremst ~12 A ≈ 8,3 kW → Höchstwert genau finden
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1", "MAX_POWER_W": "8280"}) as e:
        http("POST", e.api("/api/_/maxcurrent"), "16")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        st = wait_for(lambda: (s := e.state())["status"] == "C" and s["max_power_w"] and s, timeout=40,
                      msg="Höchstwert ermittelt")
        assert 8180 <= st["max_power_w"] <= 8280 and st["max_power_rejected"] > 8280
        assert 8180 <= e.simstate()["limit_w"] <= 8280


def test_refines_previously_coarse_max_power(tmp_path):
    # Stand des Nutzers: gespeicherter Höchstwert 7400 W aus der groben Stufensuche
    import json
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text(json.dumps(
        {"next_tx_id": 1, "chargers": {"SIMSOLAX01": {"max_power_w": 7400.0}}}))
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1", "MAX_POWER_W": "8280"}) as e:
        http("POST", e.api("/api/_/maxcurrent"), "16")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        st = wait_for(lambda: (s := e.state())["status"] == "C" and (s["max_power_w"] or 0) > 8000 and s,
                      timeout=40, msg="7400 W nachgeschärft")
        assert st["max_power_w"] <= 8280


def test_ramp_up_is_not_mistaken_for_two_phases(tmp_path):
    # Realer Fall 14.10.: L1 kurz bei 0,66 A → Bridge rechnete "2-phasig" und setzte 2760 W bzw. 7360 W
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1", "RAMP_L1_SECONDS": "4"}) as e:
        http("POST", e.api("/api/_/maxcurrent"), "10")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: e.state()["currents"][0] == 0.66, msg="Hochfahren mit L1 bei 0,66 A")
        wait_for(lambda: e.state()["currents"] == [10.0, 10.0, 10.0], msg="alle 3 Phasen bei 10 A")
        time.sleep(2)
        assert not any("phasig" in t for t in e.events())
        assert e.simstate()["limit_w"] == 6900.0


def test_rejected_small_limit_keeps_previous(tmp_path):
    # 1 Phase × 10 A = 2300 W liegt unter dem Solax-Minimum → nie Profile löschen (sonst ungebremst)
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1"}) as e:
        http("POST", e.api("/api/_/maxcurrent"), "10")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: e.state()["currents"][0] == 10.0, msg="lädt mit 10 A")
        http("POST", e.api("/api/_/phases"), "1")
        wait_for(lambda: any("bisheriges Limit 10 A bleibt aktiv" in t for t in e.events()), msg="abgelehnt, altes Limit bleibt")
        sim = e.simstate()
        assert sim["limit_w"] == 6900.0 and not any(r.startswith("ClearChargingProfile") for r in sim["received"])


def test_evcc_requests_answer_fast_with_slow_wallbox(tmp_path):
    # Realer Fall 23.10.: Solax antwortet langsam, Höchstleistungssuche + Remote Start > 10 s
    # → EVCC meldete "charger enable: ... Client.Timeout exceeded"
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1", "MAX_POWER_W": "9380", "REPLY_DELAY": "1.5"}) as e:
        e.simctl("plug")
        time.sleep(2)
        http("POST", e.api("/api/_/maxcurrent"), "16")
        t0 = time.time()
        http("POST", e.api("/api/_/enable"), "true")
        assert time.time() - t0 < 1.0, "Antwort an EVCC zu langsam"
        st = wait_for(lambda: (s := e.state())["status"] == "C" and s["max_power_w"] and s, timeout=90,
                      msg="lädt nach Höchstleistungssuche")
        assert 9280 <= st["max_power_w"] <= 9380
        # schnelle Wechsel werden der Reihe nach abgearbeitet, der letzte Stand gewinnt
        for v in ("false", "true", "false"):
            http("POST", e.api("/api/_/enable"), v)
        wait_for(lambda: e.state()["ocpp_status"] == "SuspendedEVSE", timeout=60, msg="letzter Stand: Pause")


def _run_phase_test(e):
    http("POST", e.api("/api/_/maxcurrent"), "16")
    http("POST", e.api("/api/_/enable"), "true")
    e.simctl("plug")
    wait_for(lambda: e.state()["status"] == "C", msg="lädt")
    assert http("POST", e.api("/api/_/test/phases?wait=4"), "")["result"] == "gestartet"
    rep = wait_for(lambda: (r := http("GET", e.api("/api/_/test/phases"))).get("summary") and r,
                   timeout=60, msg="Phasentest fertig")
    return rep


def test_phase_test_wallbox_that_switches(tmp_path):
    with running(tmp_path, bridge_env={"RATE_UNIT": "W"}) as e:
        rep = _run_phase_test(e)
        assert [s["result"].split(":")[0] for s in rep["steps"]] == ["wirkt"] * 4, rep
        assert rep["summary"].startswith("1-phasiges Laden per OCPP funktioniert")
        # danach gilt wieder die EVCC-Vorgabe: 16 A, 3 Phasen
        wait_for(lambda: e.state()["currents"] == [16.0, 16.0, 16.0], msg="EVCC-Vorgabe wiederhergestellt")


def test_phase_test_solax_rejects_single_phase(tmp_path):
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1"}) as e:
        rep = _run_phase_test(e)
        assert rep["steps"][0]["result"].startswith("wirkt")
        assert all(s["result"].startswith("abgelehnt") for s in rep["steps"][1:3]), rep
        assert "nicht" in rep["summary"]


def test_phase_test_needs_active_charging(tmp_path):
    with running(tmp_path) as e:
        http("POST", e.api("/api/_/test/phases?wait=1"), "")
        rep = wait_for(lambda: (r := http("GET", e.api("/api/_/test/phases"))).get("error") and r, msg="Abbruch")
        assert "Auto einstecken" in rep["error"]


def test_max_power_redetected_after_boot(tmp_path):
    # Nutzer hat den Maximalstrom in der Solax von 14 auf 16 A erhöht → gespeicherte Grenze verwerfen
    import json
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "state.json").write_text(json.dumps(
        {"next_tx_id": 1, "chargers": {"SIMSOLAX01": {"max_power_w": 9380.0, "max_power_rejected": 9420.0}}}))
    with running(tmp_path, sim_env={"SOLAX_QUIRKS": "1"}) as e:
        assert any("9380 W verworfen" in t for t in e.events())
        http("POST", e.api("/api/_/maxcurrent"), "16")
        http("POST", e.api("/api/_/enable"), "true")
        e.simctl("plug")
        wait_for(lambda: (s := e.state())["status"] == "C" and (s["max_power_w"] or 0) >= 10900, timeout=40,
                 msg="neue Grenze 11000 W")


def test_unknown_charger_reports_offline(tmp_path):
    # EVCC fragt eine Wallbox ab, die sich noch nie verbunden hat → offline statt 404
    env = Env(tmp_path, {}, {})
    try:
        env.start_bridge()
        st = env.state("5030B002112D0P")
        assert st["status"] == "A" and st["connected"] is False and st["enabled"] is False
        assert st["power"] == 0 and st["energy"] == 0 and st["energy_known"] is False
        assert http("POST", env.api("/api/5030B002112D0P/maxcurrent"), "10")["result"] == "stored"
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
