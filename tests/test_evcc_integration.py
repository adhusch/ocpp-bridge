"""Integrationstest mit echtem EVCC.

Läuft nur, wenn EVCC_BIN auf ein evcc-Binary zeigt (die GitHub-Action lädt
automatisch die neueste Version). Getestet wird genau der Charger-Block aus
evcc-charger.yaml: Sofortladen, PV-Überschussregelung, 3p→1p-Umschaltung,
Pause und Abstecken.

    EVCC_BIN=/pfad/zu/evcc pytest -v tests/test_evcc_integration.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from test_e2e import ROOT, Env, free_port, http, wait_for

EVCC_BIN = os.environ.get("EVCC_BIN", "")
pytestmark = pytest.mark.skipif(not EVCC_BIN or not Path(EVCC_BIN).exists(),
                                reason="EVCC_BIN nicht gesetzt")


def charger_block(bridge_port: int) -> str:
    """Charger-Block aus evcc-charger.yaml, auf den Testport umgebogen, Phasenumschaltung aktiv."""
    src = (ROOT / "evcc-charger.yaml").read_text()
    block = src[src.index("chargers:"):src.index("loadpoints:")]
    block = block.replace("http://192.168.1.10:8887", f"http://127.0.0.1:{bridge_port}")
    block = (block.replace("    # tos: true", "    tos: true")
                  .replace("    # phases1p3p:", "    phases1p3p:")
                  .replace("    #   ", "      "))
    return block


def write_config(tmp: Path, evcc_port: int, bridge_port: int, house_port: int) -> Path:
    cfg = f"""
network:
  port: {evcc_port}
interval: 5s
database:
  type: sqlite
  dsn: {tmp / 'evcc.db'}
log: info
levels:
  lp-1: debug
meters:
  - name: grid
    type: custom
    power:
      source: http
      uri: http://127.0.0.1:{house_port}/grid
  - name: pv
    type: custom
    power:
      source: http
      uri: http://127.0.0.1:{house_port}/pv
site:
  title: Test
  meters:
    grid: grid
    pv: [pv]
{charger_block(bridge_port)}
loadpoints:
  - title: Garage
    charger: solax
    mode: off
    phases: 0
    enable:
      delay: 10s
      threshold: 0
    disable:
      delay: 10s
      threshold: 0
"""
    p = tmp / "evcc.yaml"
    p.write_text(cfg)
    return p


def test_with_real_evcc(tmp_path):
    env = Env(tmp_path, {}, {"METER_EVERY": "2"})
    evcc_port, house_port = free_port(), free_port()
    procs = []
    try:
        env.start_bridge()
        env.start_sim()
        wait_for(lambda: env.state()["connected"], msg="Wallbox verbunden")
        procs.append(subprocess.Popen([sys.executable, str(ROOT / "tests/house_sim.py"), str(house_port), str(env.port)]))
        cfg = write_config(tmp_path, evcc_port, env.port, house_port)
        assert "phases1p3p:" in cfg.read_text() and "\n    tos: true" in cfg.read_text()
        log = open(tmp_path / "evcc.log", "w")
        procs.append(subprocess.Popen([EVCC_BIN, "--config", str(cfg)], cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT))

        api = f"http://127.0.0.1:{evcc_port}/api"

        def lp():
            st = http("GET", f"{api}/state")
            st = st.get("result", st)  # ältere Versionen packen in "result"
            return st["loadpoints"][0]

        def set_mode(m):
            http("POST", f"{api}/loadpoints/1/mode/{m}", "")

        def pv(w):
            http("POST", f"http://127.0.0.1:{house_port}/pv?w={w}", "")

        wait_for(lambda: lp() is not None, timeout=60, msg="EVCC gestartet")
        version = (tmp_path / "evcc.log").read_text().split("evcc ", 1)[-1].split()[0]
        print(f"EVCC-Version: {version}")
        text = (tmp_path / "evcc.log").read_text()
        assert "power ✓ energy ✓ currents ✓ phases ✓" in text, "Charger-Fähigkeiten nicht erkannt"

        # 1) einstecken, Modus off → verbunden, aber gesperrt
        env.simctl("plug")
        wait_for(lambda: (l := lp())["connected"] and not l["enabled"], timeout=40, msg="EVCC sieht Auto")
        assert lp()["vehicleIdentity"] == "evcc"

        # 2) Sofortladen → 16 A, 3-phasig
        set_mode("now")
        wait_for(lambda: (l := lp())["charging"] and l["chargePower"] > 10000, timeout=60, msg="Sofortladen 16 A")
        assert env.simstate()["limit_a"] == 16.0

        # 3) PV-Überschuss ~6 kW → Strom wird heruntergeregelt
        pv(6000)
        set_mode("pv")
        wait_for(lambda: 6.0 <= env.simstate()["limit_a"] <= 8.0 and env.simstate()["phases"] in (0, 3),
                 timeout=90, msg="PV-Regelung 3p")

        # 4) wenig PV → Umschaltung auf 1 Phase
        pv(2500)
        wait_for(lambda: env.simstate()["phases"] == 1 and lp()["phasesActive"] == 1 and lp()["charging"],
                 timeout=150, msg="Umschaltung 1p")

        # 5) zu wenig PV → Pause (0 A, Transaktion bleibt)
        pv(500)
        wait_for(lambda: env.simstate()["status"] == "SuspendedEVSE" and not lp()["enabled"],
                 timeout=150, msg="Pause bei zu wenig PV")
        assert env.state()["transaction_id"] is not None

        # 6) abstecken
        env.simctl("unplug")
        wait_for(lambda: not lp()["connected"], timeout=40, msg="EVCC sieht Abstecken")
        assert lp()["chargedEnergy"] > 0

        errors = [l for l in (tmp_path / "evcc.log").read_text().splitlines()
                  if ("ERROR" in l or "FATAL" in l) and "solax" in l.lower()]
        assert not errors, "\n".join(errors)
    finally:
        for p in procs:
            p.terminate()
        env.close()
        log_path = tmp_path / "evcc.log"
        if log_path.exists():
            print("\n===== evcc.log =====\n" + log_path.read_text()[-5000:])
