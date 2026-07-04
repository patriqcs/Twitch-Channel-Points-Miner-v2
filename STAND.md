# STAND — Web-Redeem (Stand: 2026-07-04)

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
