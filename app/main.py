"""Einstiegspunkt: python -m app.main"""
from __future__ import annotations

import logging

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from .config import config
from .server import Bridge

__version__ = "1.0.0"


class ProblemAccessLogger(AbstractAccessLogger):
    """Loggt nur fehlgeschlagene Anfragen – damit abgewiesene Wallbox-Verbindungen sichtbar werden."""

    def log(self, request, response, time):
        if response.status < 400:
            return
        if response.status == 400 and request.method not in ("GET", "POST", "PUT", "DELETE", "HEAD"):
            self.logger.warning(
                "Unlesbare Anfrage von %s – vermutlich wss:// (TLS). Die Bridge spricht nur ws://, "
                "in der Wallbox ws://<host>:%d/ eintragen", request.remote, config.port)
            return
        self.logger.warning("%s %s von %s → HTTP %d", request.method, request.path, request.remote, response.status)


def main():
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-6s %(message)s",
    )
    logging.getLogger("ocpp").setLevel(logging.INFO if config.log_ocpp else logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.INFO)

    log = logging.getLogger("main")
    log.info("ocpp-bridge %s startet auf %s:%d (OCPP: ws://<host>:%d/<ChargePointID>)",
             __version__, config.host, config.port, config.port)
    log.info("Auto-Start (Plug & Charge): %s | Pause-Modus: %s | ID-Tag: %s",
             config.auto_start, config.disable_mode, config.id_tag)

    bridge = Bridge(config)
    web.run_app(bridge.make_app(), host=config.host, port=config.port, print=None,
                access_log_class=ProblemAccessLogger)


if __name__ == "__main__":
    main()
