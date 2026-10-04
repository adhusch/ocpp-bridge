# ocpp-bridge

Schlanker OCPP-1.6J-Server, der eine Wallbox (Verbindung und Einrichtung mit einer echten Solax X3-HAC geprüft, Ladeabläufe gegen eine Solax-artige Simulation getestet) für **EVCC** steuerbar macht – ohne EVCCs lizenzpflichtigen `ocpp`-Charger und mit einfachem Plug&Charge.

```
Wallbox ──OCPP 1.6J (WebSocket)──▶ ocpp-bridge ◀──HTTP── EVCC (charger type: custom)
```

EVCC spricht die Bridge über seinen freien `custom`-Charger mit HTTP-Plugins an. Die Bridge übersetzt das in OCPP – nach demselben Muster wie EVCCs eingebauter OCPP-Server:

| EVCC                    | Bridge → Wallbox (OCPP)                                                |
|-------------------------|------------------------------------------------------------------------|
| `enable: true`          | `SetChargingProfile` (TxDefaultProfile, z. B. 16 A); falls noch keine Transaktion: `RemoteStartTransaction` |
| `enable: false`         | `SetChargingProfile` mit 0 A (Transaktion bleibt) – oder `RemoteStopTransaction` |
| `maxcurrent`            | `SetChargingProfile` mit neuem Limit                                   |
| `phases1p3p`            | `SetChargingProfile` mit `numberPhases` 1 oder 3                        |
| `status`                | aus `StatusNotification`: Available→A, Preparing/Suspended*/Finishing→B, Charging→C, Faulted→F |
| `power/energy/currents` | aus `MeterValues` (Leistung W, Zählerstand kWh, Ströme A)               |
| `identify`              | ID-Tag der Transaktion (RFID bzw. Plug&Charge-Kennung)                   |

## Funktionen

- **Remote Start**: beim Einstecken (Status `Preparing`) startet die Bridge die Transaktion per `RemoteStartTransaction`, mit 0 A, bis EVCC freigibt. Alternativ startet sie erst bei der EVCC-Freigabe (`AUTO_START=false`).
- **Plug & Charge**: Startet die Wallbox selbst (Solax „Plug & Charge“/Free-Mode, RFID), akzeptiert die Bridge `Authorize`/`StartTransaction` und drosselt sofort auf den EVCC-Sollwert. Der ID-Tag geht als Fahrzeugkennung an EVCC.
- **RFID-Allowlist** (`ALLOWED_ID_TAGS`), **OCPP-Passwort** (Security Profile 1), **API-Token**.
- **Automatische Einrichtung** nach jedem (Re)Connect: Messgrößen und Messintervall konfigurieren, Status und Messwerte anfordern, Sollwert setzen.
- **Fallback** für Wallboxen ohne Ladeprofile: Pause per `RemoteStopTransaction`, Freigabe per `RemoteStartTransaction`.
- **Persistenz** in `/data/state.json`: Transaktionen überleben einen Neustart der Bridge.
- **Statusseite** unter `http://<host>:8887/` mit Live-Werten, Ereignisprotokoll und Knöpfen (Start/Stop/Entriegeln/Neu einrichten).

## Installation mit Portainer

Bei jedem Push auf `main` testet die GitHub-Action den Code. Danach baut sie das Image für `amd64` und `arm64` und legt es unter `ghcr.io/<github-nutzer>/ocpp-bridge` ab. Tags: `latest`, Commit-Hash und bei Git-Tags `v1.2.3` auch `1.2.3` und `1.2`.

1. *Stacks → Add stack →* Web-Editor, Inhalt von `docker-compose.yml` einfügen und `<GITHUB-NUTZER>` ersetzen. Dann *Deploy*.
2. Kann Portainer das Image nicht ziehen, ist das Paket noch privat. Entweder auf GitHub unter *Packages → ocpp-bridge → Package settings* die Sichtbarkeit auf *Public* stellen. Oder in Portainer unter *Registries* `ghcr.io` mit GitHub-Benutzer und einem Token mit `read:packages` eintragen.
3. Updates: Im Stack *Pull and redeploy* klicken (oder Watchtower nutzen).

**Alternativ lokal bauen**
```bash
git clone https://github.com/<github-nutzer>/ocpp-bridge.git && cd ocpp-bridge
# in docker-compose.yml "image:" durch "build: ." ersetzen
docker compose up -d --build
```

Danach zeigt `http://<host>:8887/` die Statusseite.

## Wallbox einrichten (Solax X1/X3-HAC)

In der SolaX-App: Wallbox → Feineinstellung → Erweiterte Einstellungen → OCPP (Menüpunkte können je nach App-Version abweichen):
- **Server-URL**: `ws://<IP-des-Docker-Hosts>:8887/`
- **Charge Point ID**: frei wählbar, z. B. die Seriennummer. Die ID wird an die URL angehängt (`ws://…:8887/<ID>`).
- Arbeitsmodus **„Fast/Schnell“**, nicht an den Solax-Wechselrichter gekoppelt (sonst regeln zwei Systeme gleichzeitig). Für 1/3-Phasen-Umschaltung braucht es laut EVCC-Doku Firmware ≥ V9.05.

### Erfahrungen mit echter Hardware

Stand 04.10.2026, **Solax X3-HAC** (Modell `SPACS000001`, Firmware `012.04`), Bridge im Docker-Stack neben EVCC 0.316.2.

**Geprüft und funktionsfähig**
- Die Wallbox verbindet sich mit `ws://<host>:8887/<Seriennummer>`, Subprotokoll `ocpp1.6`, ohne Passwort.
- `BootNotification`, `StatusNotification` und `Heartbeat` (alle 60 s) laufen stabil.
- Die Einrichtung nach dem Verbinden läuft durch: `GetConfiguration`, `MeterValueSampleInterval=10` wird akzeptiert, `SetChargingProfile` (TxDefaultProfile) wird akzeptiert.
- EVCC erreicht die Bridge im Stack über `http://ocpp-bridge:8887`.

**Eigenheiten der Solax, die die Bridge berücksichtigt**
- Die Box meldet ihre Ladeprofil-Einheit unter dem falsch geschriebenen Schlüssel `ChargingSchduleAllowedChargingRate` mit dem Wert `Power`. Die Bridge erkennt das und schickt Ladeprofile in **Watt** (Strom × 230 V × Phasen). Lädt das Auto mit weniger Phasen als angenommen, rechnet sie das Limit anhand der gemessenen Ströme nach. Mit `RATE_UNIT=A` lässt sich das übersteuern.
- `MeterValuesSampledData` ist schreibgeschützt. Die Box liefert fest Strom, Spannung, Energie, Frequenz, Leistung und Leistungsfaktor, was für EVCC reicht.
- `TriggerMessage MeterValues` wird abgelehnt, im Leerlauf kommen also keine Messwerte auf Anfrage. Die Bridge stellt deshalb `ClockAlignedDataInterval` von 900 auf 60 s (`METER_ALIGNED_INTERVAL`), damit die Box auch ohne Ladevorgang regelmäßig den Zählerstand schickt. Bis zum allerersten Zählerstand meldet sie EVCC 0 (`energy_known: false` im JSON), weil EVCCs Prüfung mit einem leeren Wert scheitert.
- Kein Schlüssel `SupportedFeatureProfiles`; `ChangeConfiguration WebSocketPingInterval` beantwortet die Box mit leerer Antwort. Beides ist unkritisch.
- `AuthorizeRemoteTxRequests=false`: Ein Remote Start braucht kein vorheriges `Authorize`.

**Noch nicht mit Auto geprüft:** Remote Start beim Einstecken, ob die Box das Watt-Limit tatsächlich einhält, Pause per 0 W und Phasenumschaltung. Diese Abläufe sind bisher nur in der Simulation mit nachgebildeten Solax-Eigenheiten getestet (`SOLAX_QUIRKS=1`).

## EVCC einrichten

Den Inhalt von `evcc-charger.yaml` in die `evcc.yaml` übernehmen und `192.168.1.10` durch die IP des Docker-Hosts ersetzen. Läuft EVCC im selben Docker-Netz, geht auch `http://ocpp-bridge:8887`. Danach EVCC neu starten.

Phasenumschaltung: Den auskommentierten Block `tos: true` / `phases1p3p` aktivieren. Das klappt nur, wenn die Wallbox `numberPhases` in Ladeprofilen umsetzt – auf der Statusseite prüfbar.

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PORT` | `8887` | ein Port für OCPP, HTTP-API und Statusseite |
| `AUTO_START` | `true` | beim Einstecken automatisch `RemoteStartTransaction` |
| `DISABLE_MODE` | `profile` | Pause: `profile` = 0 A-Ladeprofil, `stop` = Transaktion beenden |
| `STOP_FALLBACK` | `true` | lehnt die Wallbox Ladeprofile ab, beim Pausieren `RemoteStop` senden |
| `ID_TAG` | `evcc` | ID-Tag für Remote Start |
| `ALLOWED_ID_TAGS` | leer | erlaubte RFID-/Plug&Charge-Tags, kommagetrennt; leer = alle |
| `ALLOWED_CHARGERS` | leer | erlaubte Charge-Point-IDs; leer = alle |
| `DEFAULT_CHARGER_ID` | `wallbox` | ID, wenn die Wallbox ohne ID im Pfad verbindet (`ws://host:8887/`) |
| `RATE_UNIT` | `auto` | Einheit der Ladeprofile: `auto` (wie von der Wallbox gemeldet), `A` oder `W` |
| `VOLTAGE` | `230` | Spannung für die Umrechnung von A in W |
| `OCPP_PASSWORD` | leer | Basic-Auth-Passwort für die Wallbox |
| `API_TOKEN` | leer | Bearer-Token für `/api/*` (Statusseite dann mit `?token=…` öffnen) |
| `CONNECTOR_ID` | `1` | Ladepunkt-Nummer an der Wallbox |
| `METER_INTERVAL` | `10` | Sekunden zwischen Messwerten während des Ladens |
| `METER_ALIGNED_INTERVAL` | `60` | Sekunden zwischen uhr-synchronen Messwerten, auch ohne Ladevorgang; `0` = nicht ändern |
| `METER_MEASURANDS` | Energie, Leistung, Strom, Spannung | angeforderte Messgrößen |
| `STACK_LEVEL` / `PROFILE_ID` | `0` / `1` | Ladeprofil-Details, bei Bedarf anpassen |
| `LOG_LEVEL` | `INFO` | `DEBUG` für mehr Details |
| `LOG_OCPP` | `false` | jede OCPP-Nachricht protokollieren (Fehlersuche) |

## HTTP-API

`<cp>` = Charge-Point-ID oder `_` für die erste/einzige Wallbox.

| Methode | Pfad | |
|---|---|---|
| GET | `/api/<cp>/state` | kompletter Zustand als JSON (`status`, `enabled`, `power`, `energy`, `currents`, `idtag`, …) |
| GET | `/api/<cp>/<feld>` | einzelnes Feld als Text, z. B. `/api/_/status` |
| POST | `/api/<cp>/enable` | Body `true`/`false` |
| POST | `/api/<cp>/maxcurrent` | Body z. B. `16` |
| POST | `/api/<cp>/phases` | Body `1` oder `3` |
| POST | `/api/<cp>/start` · `stop` · `unlock` · `reset` · `apply` · `setup` | manuelle Befehle |
| POST | `/api/<cp>/trigger/<Nachricht>` | `TriggerMessage`, z. B. `StatusNotification` |
| GET/POST | `/api/<cp>/configuration` | `GetConfiguration` bzw. `ChangeConfiguration` (`{"key":…, "value":…}`) |
| GET | `/api/chargers`, `/api/events`, `/health` | Übersicht, Ereignisprotokoll, Healthcheck |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -v tests/
```

`tests/sim_wallbox.py` simuliert eine Solax-ähnliche Wallbox mit Steuer-API (einstecken, abstecken, RFID). Mit `SOLAX_QUIRKS=1` verhält sie sich wie die echte X3-HAC mit Firmware 012.04 (siehe oben). Die Tests decken ab: Einrichtung, Remote Start beim Einstecken, Freigabe/Pause/Stromänderung, Phasenumschaltung, wallbox-seitiges Plug & Charge, RFID-Allowlist, Fallback ohne Ladeprofile, Neustart der Bridge mitten im Laden, Watt-Profile der Solax inklusive einphasig ladendem Auto, Verbindung ohne ID im Pfad, noch nie verbundene Wallbox, OCPP-Passwort und API-Token.

`tests/test_evcc_integration.py` startet zusätzlich ein **echtes EVCC** mit dem Charger-Block aus `evcc-charger.yaml` und einem simulierten Hausnetz. Geprüft werden Sofortladen, PV-Überschussregelung, Umschaltung von 3 auf 1 Phase bei wenig PV (unter 3 × 6 A ≈ 4,1 kW), Pause und Abstecken – einmal mit Ladeprofilen in Ampere und einmal mit den Watt-Profilen der Solax. Lokal mit `EVCC_BIN=/pfad/zu/evcc pytest tests/test_evcc_integration.py`. Die GitHub-Action lädt dafür bei jedem Lauf die neueste EVCC-Version und läuft zusätzlich jeden Montag, damit neue EVCC-Versionen automatisch geprüft werden.

## Bekannte Grenzen

- **ISO-15118-Plug-&-Charge mit Zertifikaten** (OCPP 1.6 Security/PnC-Erweiterung) ist nicht implementiert. „Plug & Charge“ heißt hier: automatischer Start beim Einstecken bzw. Fahrzeug-/Wallbox-Tag akzeptieren.
- Eine Wallbox mit **einem Ladepunkt** pro Charge-Point-ID (`CONNECTOR_ID`).
- Gegen echte Solax-Hardware bisher nur Verbindung und Einrichtung geprüft (siehe „Erfahrungen mit echter Hardware“), die Ladeabläufe gegen die Simulation und echtes EVCC (automatisch gegen die jeweils neueste Version). Bei Problemen `LOG_OCPP=true` setzen und das Log ansehen.
