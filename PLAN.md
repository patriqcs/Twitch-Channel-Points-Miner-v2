# Umbauplan: Web-verwalteter Multi-Account-Miner mit Proxys

> Wiederhergestellt aus der disconnecteten Session `0a91a42a` (2026-06-16, durch VM-Reboot
> 07:48 gekillt). Plan + Recherche waren final, Implementierung noch nicht begonnen.

## Ziel
Den Twitch-Channel-Points-Miner zu einem **web-verwalteten Multi-Account-System mit Proxy-Support**
umbauen. Bestehende Prozess-pro-Account-Isolation bleibt erhalten.

## Architektur
- **Backend:** FastAPI + SQLite (Credentials verschlüsselt via Fernet). Verwaltet Accounts/Proxys,
  startet/stoppt Miner als Subprozesse.
- **Proxys (neu):** HTTP/SOCKS5 pro Account, eingespeist in HTTP-Requests **und** WebSocket.
  Test-Funktion zeigt Exit-IP + Latenz. Regel: **max. 5 Accounts pro Proxy.**
- **Frontend:** React + TypeScript + Vite + Tailwind + shadcn/ui. Dashboard mit Punkte-Graphen
  pro Account (Recharts), Live-Status, Live-Logs (WebSocket), TanStack Query.
- **Accounts:** manuell anlegen/importieren, Device-Code-Login direkt in der UI, Proxy zuordnen,
  Login testen.
- **Deployment:** ein Unraid-Docker-Container (Multi-Stage-Build, Procfile/honcho).

## Scope-Grenze (bewusst gesetzt)
**Keine** vollautomatische Massen-Registrierung von Twitch-Accounts (Catch-all-Mails, Auto-Codes,
erfundene Geburtsdaten) — das umgeht Twitchs Missbrauchsschutz, verstößt gegen die ToS und wird
nicht gebaut. Accounts legt der Nutzer selbst an; die UI importiert/verwaltet sie nur.

## Phasen
1. **Proxy-Support in der Engine** — `Proxy.py` (dataclass + `requests_proxies`/`ws_kwargs`/`test_proxy`),
   Session-Injektion in `TwitchChannelPointsMiner`/`Twitch`/`TwitchLogin`, Proxy-Kwargs in
   `WebSocketsPool.run_forever`, `PySocks`/`requests[socks]` in requirements. Isoliert mit 1 Account testbar.
2. **Backend-Grundgerüst** — FastAPI-App, SQLModel-Models (Account/Proxy/Event/AppSetting),
   `crypto.py` (Fernet), `db.py` (SQLite+WAL), `config.py`, `schemas.py`, Migrationsskript aus
   `accounts.txt`/`streamers.txt`.
3. **MinerManager + miner_runner.py + /internal** — MinerManager (Popen-Subprozesse,
   start/stop/restart/all, Reaper), `miner_runner.py` (Config-Fetch, Reporting-Handler),
   interne Endpoints config + events.
4. **Login-Service + Account/Proxy-Endpoints** — Device-Code-Login-Service, Accounts-/Proxys-Router
   (CRUD, login/status, start/stop/restart, proxy-test, login-test, Zuordnung mit 5er-Limit), Settings-Router.
5. **WebSocket-Streams** — Live-Log-Tail (`logs/<username>.log`), Status-Push, globale Events/Punkte-Serie.
6. **Frontend** — Dashboard, Accounts, Proxys, Einstellungen, Logs. Recharts, TanStack Query, WS-Live-Daten.
7. **Dockerfile/Procfile + Unraid + E2E** — Multi-Stage-Dockerfile, Procfile/honcho, Unraid-Template,
   End-to-End-Test im Container.

## Library-Fakten (recherchiert)
- **Kein** Proxy-Support in der Library vorhanden → muss in Phase 1 ergänzt werden.
  Injektionspunkte: `TwitchLogin.py` (`requests.session()`), `Twitch.py` (requests),
  `TwitchWebSocket.py` (`WebSocketApp`, `run_forever`).
- Eingebauter Flask-Analytics-Server (`AnalyticsServer.py`, Port 5000) existiert — wird durch das
  neue FastAPI-Backend ersetzt/abgelöst.

## Stand
- ✅ **Phase 1 fertig** (2026-06-16): `Proxy.py` (dataclass, from_url, requests_proxies, ws_kwargs,
  test_proxy), Proxy-Injektion in `Twitch`/`TwitchLogin`/`WebSocketsPool`, `Settings.proxy`-Global,
  `proxy`-Param in der Hauptklasse + `run.py` (ENV `PROXY`), `requests[socks]`+`python-socks` in
  requirements. E2E getestet: HTTP-Requests UND WebSocket docken nachweislich am Proxy an.
- ✅ **Phase 2 fertig** (2026-06-16): `backend/`-Package — FastAPI-App (`main.py`, Health, Lifespan),
  SQLModel-Models (Account/Proxy/Event/AppSetting, verschlüsselte Creds), `crypto.py` (Fernet,
  Key in DATA_DIR/secret.key chmod 600), `db.py` (SQLite+WAL+FK), `config.py` (alles unter DATA_DIR),
  `schemas.py`, `migrate.py` (idempotent aus accounts.txt/streamers.txt). Getestet: Crypto-Roundtrip,
  WAL, Relationships, Unique-Constraint, Migration-Idempotenz, Health.
- ✅ **Phase 3 fertig** (2026-06-16): `MinerManager` (`backend/manager.py`, Popen-Subprozess pro
  Account in eigener Prozessgruppe, start/stop/restart/start_all/stop_all, Reaper-Thread, sauberer
  SIGTERM→SIGKILL-Stop), `miner_runner.py` (Config-Fetch vom Backend mit Fallback, Reporter-Thread
  für Status + Punkte-Snapshots), interner Router (`/internal/config`, `/internal/events`,
  Token-geschützt). Getestet: Token-Auth, Config mit entschlüsselter Proxy-URL, Event-Recording +
  Status-Update, Prozess-Start/Stop/Doppelstart-Schutz/Reaper.
- ✅ **Phase 4 fertig** (2026-06-16): Login-Service (`login_service.py`, Device-Code-Flow für die UI,
  proxy-aware, schreibt Cookie), Accounts-Router (CRUD, start/stop/restart, login + login/status +
  login-test, 5-pro-Proxy-Regel), Proxys-Router (CRUD + /test mit Exit-IP+Latenz), Settings-Router
  (Streamer-Liste + generisch), System-Router (start-all/stop-all/running), gemeinsames `proxy_util`.
  Getestet: alle CRUD, 5-pro-Proxy-409, Duplikat-409, Proxy-in-use-409, Passwort nie in Response,
  Login-Flow, Login-Test ohne Cookie, Start/Stop.
- Phasen 5–7: noch nicht implementiert.
- Vorhandenes Fundament (frühere Session, uncommittet): `run.py`, `docker-entrypoint.sh`,
  `Dockerfile.unraid`, `docker-compose.yml`, `start_all.sh`/`stop_all.sh`, `UNRAID.md`, `.gitignore`.
