# Webseite-Einlösen (öffentliche Redeem-Webseite)

Besucher lösen Kanalpunkte-Belohnungen per Klick auf einer Webseite aus
(z. B. `https://redeem.patriq.de`) — dasselbe wie Chat-Einlösen, nur ohne
Chat. Wer was einlösen darf, steuert der Miner Manager auf der Seite
**Webseite-Einlösen**.

## Architektur

```
Internet ──(Cloudflare Tunnel/Reverse Proxy)──> twitch-miner-webredeem :8080
                                                       │  X-Redeem-Token
                                                       ▼
                                     twitch-miner-manager :8000  (NUR LAN!)
```

- **twitch-miner-manager** (bestehender Container): hat KEINE Authentifizierung
  und darf **niemals** direkt ins Internet.
- **twitch-miner-webredeem** (neuer Container, `Dockerfile.webredeem`): das
  einzige öffentliche Stück. Statische Seite + Proxy für genau fünf API-Calls
  (Login, Logout, Passwort ändern, Katalog, Einlösen). Das Zugriffs-Token
  bleibt serverseitig; zusätzlich Per-IP- und globale Rate-Limits.

## Funktionen

- **Login-Pflicht**: Nur Webseiten-Benutzer (im Manager angelegt, Username +
  Passwort) sehen Belohnungen und können einlösen. Benutzer ändern ihr
  Passwort selbst; nach einem Admin-Reset wird die Änderung erzwungen.
- **Punkte-Anzeige**: Summe der verfügbaren Punkte aller Web-Einlöser-Accounts.
- **Belohnungs-Karten** mit Kosten, Beschreibung und Live-Cooldown-Countdown.
- **Chat-Ansage (abschaltbar)**: Ein Ansage-Account postet im Twitch-Chat,
  welcher Benutzer was eingelöst hat (Vorlage mit `{user}`, `{reward}`,
  `{cost}`).
- **Branding**: Titel, Untertitel und Offline-Text sind im Manager editierbar;
  Standard ist j4nkttv inkl. Link zu twitch.tv/j4nkttv.

## Setup auf Unraid

1. **Manager aktualisieren** (Image mit Web-Redeem-Modul) und im Manager unter
   **Webseite-Einlösen** konfigurieren:
   - Channel (z. B. `j4nkttv`), Belohnungen (per „Belohnungen laden" mappen),
     Cooldowns, Texte.
   - **Web-Einlöser**-Haken bei den Accounts setzen, die Punkte ausgeben dürfen.
   - **Webseiten-Benutzer** anlegen (ohne Passwort-Eingabe wird eins generiert
     und einmalig angezeigt).
   - Optional **Chat-Ansage** aktivieren (Ansage-Account + Text).
   - **Token anzeigen** klicken und kopieren (liegt auch unter
     `/mnt/user/appdata/twitch-miner-manager/webredeem_token`).
   - Modul mit **Starten** aktivieren.
2. **Webredeem-Container anlegen** — Template `unraid-template-webredeem.xml`
   (Image `ghcr.io/patriqcs/twitch-channel-points-miner-v2:webredeem`):
   - `MANAGER_URL`: z. B. `http://192.168.178.60:8000` (Unraid-IP + Manager-
     Port). Auf einem gemeinsamen Custom-Docker-Netz geht auch
     `http://twitch-miner-manager:8000`.
   - `REDEEM_TOKEN`: das kopierte Token.
   - Port 8080 nach Belieben mappen.
3. **Öffentlich machen** (empfohlen: Cloudflare Tunnel):
   - Im bestehenden `cloudflared` eine Route anlegen, z. B.
     `redeem.patriq.de -> http://UNRAID-IP:8080`.
   - `TRUST_PROXY` bleibt `true`, damit die Rate-Limits die echte Besucher-IP
     (X-Forwarded-For) verwenden.
   - Den Manager-Port (8000) NICHT veröffentlichen.

## Lokaler Test

```bash
docker build -f Dockerfile.webredeem -t twitch-miner-webredeem .
docker run --rm -p 8080:8080 \
  -e MANAGER_URL=http://192.168.178.60:8000 \
  -e REDEEM_TOKEN=<Token aus dem Manager> \
  twitch-miner-webredeem
```

## Sicherheit

- Token nur zwischen den beiden Containern; der Browser bekommt eine eigene
  Session (7 Tage gleitend, in-memory — Manager-Neustart heißt neu einloggen).
- Passwörter als scrypt-Hashes; Login-Throttle (5 Fehlversuche -> 60 s Sperre)
  zusätzlich zu den Per-IP-Limits des Webseiten-Containers.
- Einlösen: max. 1 Klick / 2 s und 10 / min pro IP, 60 / min global; dazu die
  Cooldowns pro Belohnung (Manager) und pro Account/global (Seite „Einlösen").
- Der Webredeem-Container läuft als unprivilegierter User und kennt keine
  Twitch-Zugangsdaten — selbst bei Kompromittierung sind nur Katalog + Trigger
  mit den konfigurierten Limits erreichbar.
