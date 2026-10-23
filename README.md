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

- **Remote Start**: Sobald EVCC freigibt, startet die Bridge die Transaktion per `RemoteStartTransaction`. Ist das Auto beim Freigeben noch nicht eingesteckt, startet sie beim Einstecken. Mit `AUTO_START=true` startet sie schon beim Einstecken (mit 0 A, bis EVCC freigibt). Das ist nur sinnvoll bei Wallboxen, die ein 0-A-Profil zuverlässig einhalten.
- **Kontrolle des Limits**: Nach jeder Änderung prüft die Bridge anhand der gemessenen Ströme, ob die Wallbox das Limit einhält. Wenn nicht, probiert sie der Reihe nach andere Profilvarianten (`TxProfile`, die andere Einheit, `ChargePointMaxProfile`) und merkt sich die erste, die wirkt. Wirkt keine, pausiert sie per `RemoteStopTransaction`.
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

**Erster Ladevorgang mit Auto (14.10.2026, Bridge-Version mit Stack Level 0 und Auto-Start)**
- Remote Start, `StartTransaction`, Messwerte (Strom, Leistung, Zählerstand) und die Fahrzeugkennung `evcc` kommen korrekt in EVCC an. Geladen wurde mit ~12,2 A pro Phase (≈ 8,3 kW).
- **Die Box hat die Ladeprofile angenommen (`Accepted`), aber nicht eingehalten.** Das 0-W-Profil nach dem automatischen Start ignorierte sie, deshalb lud sie nachts im PV-Modus, ohne dass EVCC freigegeben hatte. Auch EVCCs 6-A-Vorgabe hielt sie nicht ein (`current mismatch (got 12.4A measured, expected 6A)`).
- Konsequenzen in der Bridge:
  - Kein Remote Start mehr ohne EVCC-Freigabe (`AUTO_START=false` ist jetzt Standard).
  - Stack Level wie bei EVCC: höchste gemeldete Stufe (bei der Solax 2) statt 0.
  - Die Bridge kontrolliert das Limit (siehe „Funktionen“), schaltet bei Bedarf auf andere Profilvarianten um und pausiert notfalls per Remote Stop.
**Zweiter Test mit Auto (14.10.2026 abends, Stack Level 2)**
- Ohne PV-Überschuss im Modus „Smart“ lädt das Auto nicht, wie es sein soll.
- **Das 0-W-Profil wird eingehalten:** Die Box geht von `Charging` auf `SuspendedEVSE`, die Bridge meldet „Limit 0 A wird eingehalten“.
- **Das 6-A-Profil (4140 W) wird eingehalten:** Mit „immer laden, mindestens 6 A“ lädt das Auto mit ~6 A pro Phase. Die Strombegrenzung per OCPP funktioniert also mit `TxDefaultProfile`, Watt, Stack Level 2.
- **Die Box lehnt Watt-Limits außerhalb eines Bereichs ab (`Rejected`):** 11 040 W und 11 000 W abgelehnt, 7 400 W angenommen; 2 760 W abgelehnt, 4 140 W angenommen. Ungebremst lud das Auto mit ~12,2 A ≈ 8,3 kW. Vermutlich lehnt die Box alles über ihrem eingestellten Maximalstrom (Solax-App) und alles unter 6 A × 3 Phasen ab.
  - Bei einer Ablehnung sucht die Bridge den Höchstwert auf 100 W genau und speichert ihn (`max_power_w`, fest einstellbar mit `MAX_POWER`). Vorher blieb bei 16 A das 0-W-Profil aktiv und „Sofort“ lud gar nicht.
  - Abgelehnte Limits ändern nie das aktive Profil. Löschen würde ungebremstes Laden bedeuten.
  - **1-phasiges Laden ist per OCPP mit der Solax nicht möglich**, weil 1 Phase × 6–16 A unter 4 140 W liegt. Den EVCC-Block `phases1p3p` deshalb nicht aktivieren.
- **Phasenerkennung:** Beim Hochfahren stand L1 kurz bei 0,66 A, während L2/L3 schon ~6 A zogen. Die Bridge hielt das für 2-phasiges Laden und rechnete Watt-Limits falsch um. Sie rechnet jetzt wie EVCC fest mit 3 Phasen; die Erkennung ist optional (`PHASE_DETECT=true`) und braucht 3 übereinstimmende Messwerte.
- Weiterhin ungetestet: Phasenumschaltung per OCPP. Laut EVCC-Forum unterstützte die Solax-Firmware das bis Anfang 2026 nicht.
- Der Zählerstand kam im Leerlauf trotz `ClockAlignedDataInterval` nicht an, erst ab Ladebeginn. Für EVCC ist das unkritisch, die Sitzungsenergie stimmt.

## EVCC einrichten

Den Inhalt von `evcc-charger.yaml` in die `evcc.yaml` übernehmen und `192.168.1.10` durch die IP des Docker-Hosts ersetzen. Läuft EVCC im selben Docker-Netz, geht auch `http://ocpp-bridge:8887`. Danach EVCC neu starten.

Phasenumschaltung: Den auskommentierten Block `tos: true` / `phases1p3p` aktivieren. Das klappt nur, wenn die Wallbox `numberPhases` in Ladeprofilen umsetzt – auf der Statusseite prüfbar.

**Test mit zweitem Auto (23.10.2026)**
- Eingesteckt im Modus „Smart“ ohne Überschuss: lädt nicht, wie es sein soll.
- „Sofort“: lädt mit ~13,5 A pro Phase ≈ 9,3 kW. „Aus“: Laden stoppt sofort.
- EVCC meldete beim Freigeben `charger enable: … Client.Timeout exceeded`: EVCC wartet höchstens 10 s auf eine HTTP-Antwort, die Bridge antwortete erst nach der kompletten Abstimmung mit der langsamen Solax (bis ~2,5 s pro Befehl, dazu die Höchstleistungssuche). Die HTTP-API antwortet jetzt sofort, die Befehle an die Wallbox laufen im Hintergrund nacheinander, und bei schnellen Wechseln gewinnt der letzte Stand.

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PORT` | `8887` | ein Port für OCPP, HTTP-API und Statusseite |
| `AUTO_START` | `false` | `true` = schon beim Einstecken `RemoteStartTransaction` senden, nicht erst bei EVCC-Freigabe |
| `DISABLE_MODE` | `profile` | Pause: `profile` = 0 A-Ladeprofil, `stop` = Transaktion beenden |
| `STOP_FALLBACK` | `true` | lehnt die Wallbox Ladeprofile ab oder ignoriert sie, beim Pausieren `RemoteStop` senden |
| `ENFORCE` | `true` | gemessene Ströme mit dem Limit vergleichen und bei Abweichung andere Profilvarianten probieren |
| `ENFORCE_GRACE` | `30` | Sekunden nach einer Änderung, bevor geprüft wird (das Auto braucht Zeit zum Nachregeln) |
| `ENFORCE_TOLERANCE` | `1.5` | erlaubte Überschreitung in A pro Phase |
| `ID_TAG` | `evcc` | ID-Tag für Remote Start |
| `ALLOWED_ID_TAGS` | leer | erlaubte RFID-/Plug&Charge-Tags, kommagetrennt; leer = alle |
| `ALLOWED_CHARGERS` | leer | erlaubte Charge-Point-IDs; leer = alle |
| `DEFAULT_CHARGER_ID` | `wallbox` | ID, wenn die Wallbox ohne ID im Pfad verbindet (`ws://host:8887/`) |
| `RATE_UNIT` | `auto` | Einheit der Ladeprofile: `auto` (wie von der Wallbox gemeldet), `A` oder `W` |
| `VOLTAGE` | `230` | Spannung für die Umrechnung von A in W |
| `MAX_POWER` | leer | Höchstleistung der Wallbox in W für Watt-Profile; leer = automatisch beim ersten `Rejected` ermitteln |
| `PHASE_DETECT` | `false` | Watt-Limits an die gemessene Phasenzahl anpassen (erst nach 3 stabilen Messwerten); sonst wie EVCC 3 Phasen |
| `OCPP_PASSWORD` | leer | Basic-Auth-Passwort für die Wallbox |
| `API_TOKEN` | leer | Bearer-Token für `/api/*` (Statusseite dann mit `?token=…` öffnen) |
| `CONNECTOR_ID` | `1` | Ladepunkt-Nummer an der Wallbox |
| `METER_INTERVAL` | `10` | Sekunden zwischen Messwerten während des Ladens |
| `METER_ALIGNED_INTERVAL` | `60` | Sekunden zwischen uhr-synchronen Messwerten, auch ohne Ladevorgang; `0` = nicht ändern |
| `METER_MEASURANDS` | Energie, Leistung, Strom, Spannung | angeforderte Messgrößen |
| `STACK_LEVEL` | `auto` | Priorität der Ladeprofile; `auto` = höchste Stufe aus `ChargeProfileMaxStackLevel` (wie EVCC) |
| `PROFILE_ID` | `1` | ID der Ladeprofile |
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

`tests/sim_wallbox.py` simuliert eine Solax-ähnliche Wallbox mit Steuer-API (einstecken, abstecken, RFID). Mit `SOLAX_QUIRKS=1` verhält sie sich wie die echte X3-HAC mit Firmware 012.04 (siehe oben). Mit `HONOR_PROFILES` und `MIN_STACK` lässt sich eine Wallbox nachbilden, die Profile annimmt, aber ignoriert. Die Tests decken ab: Einrichtung, kein Laden vor EVCC-Freigabe, Stack Level aus der Wallbox, Wechsel auf eine wirksame Profilvariante, Suche der Höchstleistung der Wallbox, schnelle Antworten an EVCC trotz langsamer Wallbox, abgelehnte Limits lassen das aktive Profil unverändert, Hochfahren wird nicht als 2-phasig gedeutet, Pause per Remote Stop bei komplett ignorierten Profilen, Remote Start beim Einstecken, Freigabe/Pause/Stromänderung, Phasenumschaltung, wallbox-seitiges Plug & Charge, RFID-Allowlist, Fallback ohne Ladeprofile, Neustart der Bridge mitten im Laden, Watt-Profile der Solax inklusive einphasig ladendem Auto, Verbindung ohne ID im Pfad, noch nie verbundene Wallbox, OCPP-Passwort und API-Token.

`tests/test_evcc_integration.py` startet zusätzlich ein **echtes EVCC** mit dem Charger-Block aus `evcc-charger.yaml` und einem simulierten Hausnetz. Drei Szenarien: Ampere-Profile, Watt-Profile der Solax und eine Solax, die das Standardprofil ignoriert. Geprüft werden jeweils, dass ohne Freigabe nicht geladen wird, Sofortladen, PV-Überschussregelung, Umschaltung von 3 auf 1 Phase bei wenig PV (unter 3 × 6 A ≈ 4,1 kW), Pause und Abstecken. Lokal mit `EVCC_BIN=/pfad/zu/evcc pytest tests/test_evcc_integration.py`. Die GitHub-Action lädt dafür bei jedem Lauf die neueste EVCC-Version und läuft zusätzlich jeden Montag, damit neue EVCC-Versionen automatisch geprüft werden.

## Bekannte Grenzen

- **ISO-15118-Plug-&-Charge mit Zertifikaten** (OCPP 1.6 Security/PnC-Erweiterung) ist nicht implementiert. „Plug & Charge“ heißt hier: automatischer Start beim Einstecken bzw. Fahrzeug-/Wallbox-Tag akzeptieren.
- Eine Wallbox mit **einem Ladepunkt** pro Charge-Point-ID (`CONNECTOR_ID`).
- Gegen echte Solax-Hardware bisher Verbindung, Einrichtung und ein Ladevorgang geprüft; die Strombegrenzung ist dort noch nicht bestätigt (siehe „Erfahrungen mit echter Hardware“), die Ladeabläufe gegen die Simulation und echtes EVCC (automatisch gegen die jeweils neueste Version). Bei Problemen `LOG_OCPP=true` setzen und das Log ansehen.
