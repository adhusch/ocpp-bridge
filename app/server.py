"""aiohttp-Server: OCPP-WebSocket für die Wallbox + HTTP-API für EVCC auf einem Port."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from .chargepoint import ChargePoint
from .config import Config
from .state import Store

log = logging.getLogger("server")

WEB_DIR = Path(__file__).parent / "web"


class ConnectionClosed(Exception):
    pass


class WSAdapter:
    """Macht aus einem aiohttp-WebSocket das recv/send-Interface, das python-ocpp erwartet."""

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws

    async def recv(self) -> str:
        msg = await self.ws.receive()
        if msg.type == WSMsgType.TEXT:
            return msg.data
        if msg.type == WSMsgType.BINARY:
            return msg.data.decode("utf-8", "replace")
        raise ConnectionClosed(str(msg.type))

    async def send(self, data: str):
        if self.ws.closed:
            raise ConnectionClosed("closed")
        await self.ws.send_str(data)


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.data_dir, cfg.default_current)
        self.active: dict[str, ChargePoint] = {}

    # --------------------------------------------------------------- OCPP
    def _check_basic_auth(self, request: web.Request) -> bool:
        if not self.cfg.ocpp_password:
            return True
        hdr = request.headers.get("Authorization", "")
        if not hdr.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8", "replace").partition(":")
        except Exception:
            return False
        return pw == self.cfg.ocpp_password

    async def ocpp_handler(self, request: web.Request) -> web.StreamResponse:
        log.info("OCPP-Verbindungsversuch von %s auf Pfad %s (Subprotokoll: %s, Auth: %s)",
                 request.remote, request.path, request.headers.get("Sec-WebSocket-Protocol", "–"),
                 "ja" if request.headers.get("Authorization") else "nein")
        cp_id = request.path.rstrip("/").rsplit("/", 1)[-1]
        if not cp_id or cp_id.lower() in ("ocpp", "ocpp16", "ocpp1.6", "ws"):
            cp_id = self.cfg.default_charger_id
            log.info("Keine Charge-Point-ID im Pfad – verwende '%s'", cp_id)
        if self.cfg.allowed_chargers and cp_id not in self.cfg.allowed_chargers:
            log.warning("Unbekannte Wallbox %s abgewiesen", cp_id)
            return web.Response(status=404, text="unknown charge point")
        if not self._check_basic_auth(request):
            log.warning("Wallbox %s: falsches OCPP-Passwort", cp_id)
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="ocpp"'})

        ws = web.WebSocketResponse(protocols=("ocpp1.6",), heartbeat=60, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        if ws.ws_protocol != "ocpp1.6":
            log.warning("[%s] kein Subprotokoll ocpp1.6 angefragt (%s) – fahre trotzdem fort",
                        cp_id, request.headers.get("Sec-WebSocket-Protocol"))

        old = self.active.get(cp_id)
        if old is not None:
            self.store.event(cp_id, "Neue Verbindung ersetzt alte")
            old.cancel_tasks()
            await old._connection.ws.close()

        cp = ChargePoint(cp_id, WSAdapter(ws), self.cfg, self.store)
        self.active[cp_id] = cp
        cp.st.connected = True
        cp.st.last_seen = time.time()
        self.store.event(cp_id, f"Verbunden von {request.remote}")
        # Bei Reconnect ohne BootNotification trotzdem einrichten
        cp.schedule_setup(5.0)
        try:
            await cp.start()
        except ConnectionClosed:
            pass
        except Exception as e:
            log.exception("[%s] Verbindungsfehler: %r", cp_id, e)
        finally:
            cp.cancel_tasks()
            if self.active.get(cp_id) is cp:
                del self.active[cp_id]
                cp.st.connected = False
                self.store.event(cp_id, "Verbindung getrennt")
            self.store.save()
        return ws

    # ---------------------------------------------------------------- API
    def _resolve(self, request: web.Request):
        cp_id = request.match_info["cp"]
        if cp_id in ("_", "default"):
            if self.active:
                cp_id = next(iter(self.active))
            elif self.store.chargers:
                cp_id = next(iter(self.store.chargers))
            else:
                raise web.HTTPNotFound(text="noch keine Wallbox verbunden")
        st = self.store.chargers.get(cp_id)
        if st is None:
            raise web.HTTPNotFound(text=f"Wallbox {cp_id} unbekannt")
        return cp_id, st, self.active.get(cp_id)

    @staticmethod
    async def _value(request: web.Request, key: str):
        if "value" in request.query:
            return request.query["value"]
        if key in request.query:
            return request.query[key]
        body = (await request.text()).strip()
        if body.startswith("{"):
            try:
                data = json.loads(body)
                return data.get(key, data.get("value"))
            except json.JSONDecodeError:
                pass
        return body

    @staticmethod
    def _as_bool(v) -> bool:
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("1", "true", "on", "yes"):
            return True
        if s in ("0", "false", "off", "no"):
            return False
        raise web.HTTPBadRequest(text=f"ungültiger Wahrheitswert: {v!r}")

    @staticmethod
    def _as_float(v) -> float:
        try:
            return float(str(v).strip())
        except ValueError:
            raise web.HTTPBadRequest(text=f"ungültige Zahl: {v!r}")

    async def api_chargers(self, request):
        return web.json_response([st.to_api() for st in self.store.chargers.values()])

    async def api_events(self, request):
        return web.json_response(list(self.store.events)[::-1])

    async def api_state(self, request):
        _, st, _ = self._resolve(request)
        return web.json_response(st.to_api())

    async def api_field(self, request):
        _, st, _ = self._resolve(request)
        field = request.match_info["field"]
        data = st.to_api()
        if field not in data:
            raise web.HTTPNotFound(text=f"Feld {field} unbekannt")
        v = data[field]
        if isinstance(v, bool):
            v = "true" if v else "false"
        return web.Response(text="" if v is None else str(v))

    async def api_enable(self, request):
        cp_id, st, cp = self._resolve(request)
        enable = self._as_bool(await self._value(request, "enable"))
        changed = st.desired_enabled != enable
        st.desired_enabled = enable
        if changed:
            self.store.event(cp_id, f"EVCC: {'Freigabe' if enable else 'Pause'}")
        self.store.save()
        status = await cp.apply() if cp else "offline"
        return web.json_response({"enable": enable, "result": status})

    async def api_maxcurrent(self, request):
        cp_id, st, cp = self._resolve(request)
        current = self._as_float(await self._value(request, "maxcurrent"))
        if current < 0 or current > 80:
            raise web.HTTPBadRequest(text="Strom außerhalb 0..80 A")
        changed = abs(st.max_current - current) >= 0.1
        st.max_current = current
        self.store.save()
        status = "stored"
        if cp and st.desired_enabled and (changed or st.last_profile_status != "Accepted"):
            status = await cp.set_current(current)
        return web.json_response({"maxcurrent": current, "result": status})

    async def api_phases(self, request):
        cp_id, st, cp = self._resolve(request)
        phases = int(self._as_float(await self._value(request, "phases")))
        if phases not in (0, 1, 3):
            raise web.HTTPBadRequest(text="phases muss 1 oder 3 sein")
        st.phases = phases
        self.store.save()
        self.store.event(cp_id, f"EVCC: Phasen {phases}")
        status = await cp.apply() if cp else "offline"
        return web.json_response({"phases": phases, "result": status})

    async def api_action(self, request):
        cp_id, st, cp = self._resolve(request)
        if cp is None:
            raise web.HTTPServiceUnavailable(text="Wallbox nicht verbunden")
        action = request.match_info["action"]
        if action == "start":
            res = await cp.remote_start("API", force=True)
        elif action == "stop":
            res = await cp.remote_stop("API")
        elif action == "unlock":
            res = await cp.unlock()
        elif action == "reset":
            res = await cp.reset(request.query.get("type", "Soft"))
        elif action == "apply":
            res = await cp.apply()
        elif action == "setup":
            cp._setup_done = False
            await cp.setup()
            res = "done"
        else:
            raise web.HTTPNotFound(text=f"Aktion {action} unbekannt")
        return web.json_response({"action": action, "result": res})

    async def api_trigger(self, request):
        _, _, cp = self._resolve(request)
        if cp is None:
            raise web.HTTPServiceUnavailable(text="Wallbox nicht verbunden")
        return web.json_response({"result": await cp.trigger(request.match_info["message"])})

    async def api_configuration(self, request):
        _, _, cp = self._resolve(request)
        if cp is None:
            raise web.HTTPServiceUnavailable(text="Wallbox nicht verbunden")
        if request.method == "POST":
            data = await request.json()
            return web.json_response({"result": await cp.change_configuration(str(data["key"]), str(data["value"]))})
        return web.json_response(await cp.get_configuration())

    async def health(self, request):
        return web.json_response({"ok": True, "connected": list(self.active)})

    async def index(self, request):
        return web.FileResponse(WEB_DIR / "index.html")

    # --------------------------------------------------------- Routing
    async def catch_all(self, request: web.Request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.ocpp_handler(request)
        if request.path in ("/", "/index.html"):
            return await self.index(request)
        log.info("Unbekannte Anfrage %s %s von %s", request.method, request.path, request.remote)
        raise web.HTTPNotFound()

    @web.middleware
    async def auth_mw(self, request: web.Request, handler):
        if self.cfg.api_token and request.path.startswith("/api/"):
            tok = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.query.get("token", "")
            if tok != self.cfg.api_token:
                raise web.HTTPUnauthorized(text="API-Token fehlt oder falsch")
        return await handler(request)

    def make_app(self) -> web.Application:
        app = web.Application(middlewares=[self.auth_mw])
        r = app.router
        r.add_get("/health", self.health)
        r.add_get("/api/chargers", self.api_chargers)
        r.add_get("/api/events", self.api_events)
        r.add_get("/api/{cp}/state", self.api_state)
        r.add_route("*", "/api/{cp}/enable", self.api_enable)
        r.add_route("*", "/api/{cp}/maxcurrent", self.api_maxcurrent)
        r.add_route("*", "/api/{cp}/phases", self.api_phases)
        r.add_post("/api/{cp}/trigger/{message}", self.api_trigger)
        r.add_route("*", "/api/{cp}/configuration", self.api_configuration)
        r.add_post("/api/{cp}/{action:start|stop|unlock|reset|apply|setup}", self.api_action)
        r.add_get("/api/{cp}/{field}", self.api_field)
        r.add_route("GET", "/{tail:.*}", self.catch_all)
        app.on_shutdown.append(self._on_shutdown)
        return app

    async def _on_shutdown(self, app):
        self.store.save()
        for cp in list(self.active.values()):
            cp.cancel_tasks()
            await cp._connection.ws.close()
