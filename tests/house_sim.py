"""Simuliertes Hausnetz für den EVCC-Integrationstest.

grid = Hauslast + Ladeleistung der Bridge - PV  (negativ = Einspeisung)
GET  /grid   Netzleistung in W
GET  /pv     PV-Leistung in W
POST /pv?w=  PV-Leistung setzen

Aufruf: python house_sim.py <port> <bridge-port>
"""
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT, BRIDGE = int(sys.argv[1]), int(sys.argv[2])
HOUSE_W = 500.0
PV = {"w": 12000.0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/grid"):
            try:
                st = json.load(urllib.request.urlopen(f"http://127.0.0.1:{BRIDGE}/api/_/state", timeout=3))
                charge = st["power"]
            except Exception:
                charge = 0.0
            v = HOUSE_W + charge - PV["w"]
        else:
            v = PV["w"]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(str(v).encode())

    def do_POST(self):
        PV["w"] = float(self.path.split("w=")[1])
        self.send_response(200)
        self.end_headers()


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
