# STAND — Web-Redeem (Stand: 2026-07-04)

## Neu (2026-07-09, „Wetten"-Tab: All-in auf Kanalwetten — implementiert + lokal getestet)

Neuer Manager-Tab **Wetten** (`frontend/src/pages/Predictions.tsx`, Route
`/wetten`): Der Operator wählt EIN Ergebnis der aktiven Kanalwette (Prediction)
eines Kanals; dann setzen **alle aktivierten Accounts außer einer
Ausschlussliste (Default: `patriqcs`)** ALLE ihre Kanalpunkte auf genau dieses
Ergebnis — direkt aus dem Backend per OAuth-Token + Proxy je Account (Muster
redeem/heist), die Miner-Prozesse bleiben unberührt (`make_predictions` bleibt
aus).

- **Backend:** `backend/prediction.py` (GQL: Volltext-Query
  `activePredictionEvents` — live gegen Twitch verifiziert; Mutation
  `MakePrediction` über den bewährten Persisted-Hash des Miners mit
  Volltext-Fallback; All-in-Runde als Hintergrund-Thread mit
  Zufalls-Reihenfolge + Jitter 1–4 s zwischen Accounts, Limits 10 / 250 000
  Punkte, Stopp bei Sperre/Abbruch) + Router `backend/routers/predictions.py`
  (`/api/predictions/{config,active,balances,bet,run,cancel}`).
- **UI:** Wette laden (pollt alle 7 s), Outcome-Karten (Pool/Anteil/Quote),
  Punktestände aller wettberechtigten Accounts auf Knopfdruck (parallel),
  Bestätigungsdialog, Live-Fortschritt pro Account, Abbrechen-Button,
  Einstellungen (Ausschlussliste, Jitter). Jede Wette landet als
  Event-Typ `prediction` in den Account-Logs.
- Es läuft max. EINE Wett-Runde gleichzeitig; vor dem Start wird live
  verifiziert, dass die Wette noch offen ist und das Ergebnis dazugehört.
- **Wett-AGB (`MUST_ACCEPT_TOS`) — automatisch gelöst:** Frische Accounts geben
  beim ersten Einsatz `MUST_ACCEPT_TOS` (Wett-AGB nie akzeptiert). Die echte
  Twitch-Web-Mutation dafür wurde per Playwright aus dem Prediction-Chunk
  extrahiert und live verifiziert: `updateUserPredictionSettings(input:
  {hasAcceptedTOS:true, isTemporaryChatBadgeEnabled:true})` (Operation
  `AcceptPredictionTermsMutation`), account-global und dauerhaft. `make_prediction`
  akzeptiert bei `MUST_ACCEPT_TOS` jetzt automatisch die AGB und wiederholt den
  Einsatz einmal — **kein manueller Website-Login mehr nötig**. Live bestätigt
  (Account fieserknut): nach Auto-Accept ging der 10-Punkte-Testeinsatz durch
  (`error=None`). Der Rest-Fehlerfall (Token abgelaufen) wird im UI als „🔒 …"
  markiert. Achtung: `MakePredictionInput` hat KEIN Zustimmungs-Feld — die
  Zustimmung ist zwingend die separate Mutation.
- **Anti-Detection der Wett-Requests:** (1) **TV-Fingerprint** — die Requests
  tragen die TV-Miner-Signatur des Accounts (TV-Client-Id + `ua_app` als
  User-Agent + `X-Device-Id` + Client-Version + Client-Session-Id) statt
  Web-Client-Id ohne UA; der Token ist für den TV-Client ausgestellt, damit sind
  die Wetten von normalem Miner-GQL nicht unterscheidbar (live verifiziert).
  (2) **Einsatz-Streuung** pro Account (Default 70–100 % statt 100 %).
  (3) **Breiteres Zeitfenster** (Default 3–15 s, lock-sicher gestaucht).
- **Offener Audit-Befund:** Einlösen/Chat-Einlösen/Web-Redeem/Heist-Live-Check
  senden den TV-Token weiterhin hinter Web-Client-Id + `python-requests`-UA ohne
  `X-Device-Id` — dieselbe Unstimmigkeit, die für die Wetten behoben wurde. Fix
  analog offen (siehe Memory `project-backend-gql-fingerprint-audit`).

## Neu (2026-07-08, „Account anlegen"-Tab — implementiert + lokal getestet)

Neuer Manager-Tab **Account anlegen** (`frontend/src/pages/CreateAccount.tsx`,
Route `/account-anlegen`) zum sauberen Erfassen neuer Twitch-Accounts:
Username / E-Mail (optional) / Passwort + feste **DE-Relay-Zuweisung** (belegt/5)
+ Rollen-Checkboxen + Erstellungs-Checkliste (Anti-Detect-Profil, IPv6 aus,
E-Mail-Verify statt Telefon). Nutzt den bestehenden `POST /api/accounts` — neu
ist nur das optionale Feld **`signup_email`** (Account-Model + `_ensure_columns`
+ `AccountCreate.email`/`AccountRead.signup_email`), rein zur Übersicht.
Verworfen: die Signup-Bridge (Mullvad-SOCKS5 an den Browser) — Accounts werden
über den heimischen DS-Lite-Anschluss (Residential-IP, bessere Signup-Reputation)
erstellt, nicht über Datacenter-Mullvad. Details siehe Memory.

## Neu (2026-07-08, Anti-Detection Teil 2 — deployed + live getestet)

Nach der ersten Anti-Detection-Runde (unten) zwei weitere Maßnahmen:

- **Geo-Matching (DE-Relays):** Der geminte Streamer ist deutsch → Zuschauer-Exit-IPs
  sollen zur Stream-Region passen. Die 30 vorhandenen Mullvad-Relays sind bereits
  ALLE DE (Frankfurt/Berlin/Düsseldorf); der Import-Default (`MullvadImport.country_code`)
  ist jetzt zusätzlich fest auf `"de"` (Frontend-Default war schon "de"), damit
  versehentlich keine Nicht-DE-Relays dazukommen. Hinweis: Mullvad bleibt Datacenter-IP
  — Geo passt, aber Residential-IPs wären der nächste (teure) Schritt.
- **Variable Session-Anwesenheit** (`backend/stream_gate.py`, Churn-Loop): Während ein
  Streamer live ist, wird gelegentlich EIN Account für eine zufällige Weile pausiert
  und wieder gestartet, damit nicht alle exakt dieselbe Start-bis-Ende-Watchdauer
  haben (ein Bot-Tell). Konservativ: nie unter `SESSION_CHURN_MIN_PRESENT` (1) laufend,
  max. `SESSION_CHURN_MAX_CONCURRENT` (1) gleichzeitig pausiert. **Raid-Bonus bleibt
  sicher:** JoinRaid feuert bei stream-down sofort (1–5s), das Gate stoppt aber erst
  nach ~3 Min Offline-Hysterese — der Raid wird also immer geclaimt. ENV:
  `SESSION_CHURN_ENABLED/INTERVAL(300s)/PROB(0.2)/MIN_PRESENT(1)/MAX_CONCURRENT(1)`,
  `SESSION_PAUSE_MIN/MAX` (300/1500s).
- **Beobachtbarkeit:** Backend-/Gate-Logs gehen jetzt auf stdout (`docker logs`),
  inkl. „Stream gate started", „detected LIVE → ramping up", „session pause/resume".

## Neu (2026-07-08, Anti-Detection Teil 1 — deployed + live getestet)

Drei Ebenen gegen Bot-Erkennung (Kontext: Ban-Welle 08.07., alle Alts gesperrt).
Live verifiziert: voller online↔offline-Zyklus (shlorox-Test), Accounts fahren
gestaffelt hoch/runter, Raid-Hysterese greift.

- **Stream-Live-Gate** (`backend/stream_gate.py`, ersetzt den blinden Boot-
  Autostart): Accounts laufen NUR NOCH, wenn ein konfigurierter Streamer wirklich
  live ist. Zentraler Live-Poll (EIN Request, nicht pro Account) via GQL; geht der
  Stream live → Accounts **gestaffelt** hochfahren, geht er offline → gestaffelt
  runterfahren. Asymmetrische Hysterese: online sofort, offline erst nach 3 Checks
  (~3 Min, gegen kurze Stream-Drops), Poll-Fehler → fail-open (Mining läuft
  weiter). **Wichtig für Betrieb:** Wenn der Kanal offline ist, sind die Accounts
  jetzt bewusst gestoppt — das ist gewollt, kein Fehler.
  ENV: `STREAM_GATE_ENABLED` (Default true), `STREAM_GATE_CHECK_INTERVAL` (60s),
  `STREAM_GATE_OFFLINE_CONFIRM` (3), `STREAM_GATE_FAILOPEN_AFTER` (3),
  `STREAM_GATE_RAMP_STEP_MIN/MAX` (20/90s), `STREAM_GATE_DRAIN_STEP_MIN/MAX` (10/45s).
- **Timing-Jitter** (Engine): Bonus-Claim (bisher sofort/ms → 2–12s, größter Tell),
  Raid-Join (1–5s), Moment-Claim (1–6s) laufen jetzt verzögert über daemon-Timer
  (blockieren den WS-Handler nicht); Watch-Kadenz ±15% statt fixem 20/N-Raster;
  Bet-Zeitpunkt leicht früher-jittered. Jeder Account würfelt unabhängig →
  De-Synchronisierung. ENV: `MINER_JITTER_CLAIM/RAID/MOMENT/BET` je als „lo,hi".
- **Warm-up für neue Accounts** (`created_at` → `internal.py` →
  `MINER_ACCOUNT_AGE_DAYS`): frische Accounts wetten die ersten Tage NICHT
  (`MINER_WARMUP_BET_DAYS`, Default 7) und kommen im Stream-Gate-Ramp später dran
  (ältere zuerst). Unbekanntes Alter → als etabliert behandelt (kein Effekt).

## Neu (2026-07-04, implementiert, noch NICHT committet/deployed)

- **Offener Zugang (ohne Login)**: Checkbox auf der Manager-Seite
  „Webseite-Einlösen" (`WEBREDEEM_PUBLIC`, Standard aus). Wenn an, sieht jeder
  mit der URL die Belohnungen und kann **ohne Anmeldung einlösen**; anonyme
  Einlösungen erscheinen als „Gast" (Trigger-Log + Chat-Ansage). Login bleibt
  parallel möglich. Rate-Limits/Cooldowns unverändert.
- Betroffen sind BEIDE Images: Manager (Backend + Manager-UI) und
  `:webredeem` (statisches JS) — nach dem Push also beide Container updaten.

## Kurzfassung

**Alles LIVE und verifiziert.** Die öffentliche Redeem-Webseite läuft unter
**https://redeem.patriq.de** (eigener Container `twitch-miner-webredeem` auf
Unraid, Host-Port **8822**, hinter Cloudflare Tunnel, OHNE Zero-Trust — die
Seite hat eigenen Login). Gesteuert wird alles im Manager unter
**https://miner.patriq.de/web-redeem** („Webseite-Einlösen").

Working tree ist sauber, alles auf `master` gepusht (HEAD `bdb340d`), beide
CI-Workflows grün, beide Container auf Unraid laufen mit den aktuellen Images.

## Was heute gebaut + deployed wurde (6 Commits)

1. `9111401` **Backend-Modul**: `web_redeem{,_manager}.py` — Koordinator ohne
   IRC (Balance/Katalog-Cache alle 45 s), synchroner `trigger()` mit
   richest-free-Rotation über Accounts mit neuem Flag `web_redeemer`; teilt
   die Cooldowns mit Chat-Redeem über `redeem.py` (kein Doppel-Feuern).
   Token-geschützte öffentliche API `/api/public-redeem/*`
   (Token: `/data/webredeem_token` bzw. Env `WEBREDEEM_TOKEN`).
2. `6f4019e` **Webseiten-Benutzer + Chat-Ansage + Manager-UI**: `WebUser`
   (scrypt-Hashes, In-Memory-Sessions 7 Tage gleitend — Manager-Neustart
   loggt alle aus; Login-Throttle 5 Fehlversuche → 60 s). Admin: anlegen /
   Passwort-Reset (erzwingt Änderung) / löschen. Abschaltbare **Chat-Ansage**
   „{user} hat {reward} eingelöst" über eigenen Ansage-Account (HeistIRC).
   Neue Manager-Seite `/web-redeem` mit Status, Items, Branding, Accounts,
   Benutzern, Token.
3. `c746a6e` **Öffentlicher Container** (`webredeem/`, `Dockerfile.webredeem`):
   FastAPI-Proxy (einziges internetfähiges Stück, Token bleibt serverseitig,
   Per-IP- + globale Rate-Limits, läuft als non-root) + statische Seite
   (vanilla JS, Twitch-Dark, j4nkttv-Branding, Punkte-Anzeige,
   Cooldown-Countdowns, Passwort-Selbstservice).
4. `ea13b9f` **Deployment**: CI `deploy-webredeem-ghcr.yml` (Image-Tag
   `:webredeem` im selben GHCR-Package), `unraid-template-webredeem.xml`,
   Setup-Doku `WEBREDEEM.md`.
5. `5a9e1a2` **Selbstregistrierung mit Freischaltung**: „Konto erstellen" auf
   der Seite → `WebUser.approved=false` → Login erst nach Freischaltung im
   Manager (Badge „X Anfrage(n) offen", Freischalten-/Ablehnen-Buttons,
   10-s-Auto-Refresh). Limits: 3 Registrierungen/h/IP, max. 50 offene
   Anfragen. Migration: Bestandsbenutzer bleiben approved (DDL-Default 1).
6. `bdb340d` **Cache-Fix**: Cloudflare cached .js/.css ~4 h am Edge → nach
   Deploys lief altes JS („Konto erstellen tat nichts"). Jetzt Content-Hash
   in den Asset-URLs (`?v=<hash>`, automatisch beim Container-Start
   berechnet) + `immutable` für Assets + `no-cache` fürs HTML → jeder
   zukünftige Deploy erreicht Browser + Edge sofort, kein Purge nötig.

## Live-Zustand (heute Abend verifiziert)

- Modul **aktiv**: Channel `j4nkttv`, 10 Items (SHOOT 25 P … High voice 300 P),
  16 Web-Einlöser-Accounts, ~530k Punkte gesamt.
- Benutzer: `derletztemachtslichtaus`, `jay_be_94`, `patriQ`, `11MinutenGames`
  (alle freigeschaltet; Test-Accounts von mir wieder gelöscht).
- E2E öffentlich getestet: Registrierung → Login-Sperre → Freischaltung →
  Login → Katalog mit Items/Punkten. **Echte Einlösung bewusst NICHT
  getestet** (würde live im Stream auslösen) — erster echter Klick steht aus.
- Chat-Ansage: implementiert, aber noch **aus** (Haken + Ansage-Account auf
  der Manager-Seite setzen, wenn gewünscht).

## Betrieb / Wartung

- **Update-Prozedur** wie beim Manager (siehe UNRAID.md + Memory): push auf
  `master` → CI baut → auf Unraid `docker pull` + Container neu erstellen.
  Für die Webseite: Image `…:webredeem`, Env `MANAGER_URL=http://192.168.178.60:8844`,
  `REDEEM_TOKEN` (liegt in `/mnt/user/appdata/twitch-miner-manager/webredeem_token`),
  `-p 8822:8080`. dockerMan-Template `my-twitch-miner-webredeem.xml` +
  Autostart sind eingerichtet.
- Manager-Neustart = alle Webseiten-Sessions abgemeldet (gewollt simpel);
  Benutzer loggen sich einfach neu ein.
- Cloudflare: Public Hostname `redeem.patriq.de → http://192.168.178.60:8822`
  ist im Zero-Trust-Dashboard angelegt (Tunnel token-basiert; der API-Token in
  `~/.cf_token` hat KEINE Tunnel-Rechte — Tunnel-Änderungen übers Dashboard).

## Offene Punkte

1. Ersten **echten Redeem-Klick** auf der Seite machen (z. B. SHOOT, 25 P) und
   im Manager unter „Letzte Auslösungen" gegenprüfen.
2. Optional **Chat-Ansage** aktivieren (Ansage-Account wählen; „Chat-Verbindung
   testen" gibt es auf der Chat-Einlösen-Seite).
3. Optional: Items/Cooldowns nach den ersten Live-Erfahrungen nachjustieren
   (zusätzlich gelten die Cooldowns der Seite „Einlösen" pro Account/global).
