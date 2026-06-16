# Mullvad als Proxy-Quelle (eingebaut, eigene IP pro Account)

Free-Proxys sterben ständig — Mullvad ist deutlich stabiler. Mullvads SOCKS5-Relays
sind aber **nur im Mullvad-WireGuard-Tunnel** erreichbar (öffentlich lösen die
`*.socks5.relays.mullvad.net`-Namen nicht auf). Der Tunnel ist **direkt ins Image
eingebaut** — kein zweiter Container nötig.

## Wie es funktioniert
- Der Container baut beim Start einen Mullvad-WireGuard-Tunnel auf (`wg-quick`).
- **Split-Tunnel:** nur Mullvads `10.64.0.0/10` läuft durch den Tunnel (DNS `10.64.0.1`
  + die `10.124.x`-Relays). Dein **LAN/WebUI bleibt direkt erreichbar**.
- Die Miner nutzen pro Account ein **Multihop-SOCKS5-Relay** (`…-socks5-…relays.mullvad.net:1080`)
  → **jedes Relay = eigene Exit-IP**, alle stabil über Mullvad.

## 1. Mullvad-WireGuard-Config holen
Im Mullvad-Konto → **WireGuard configuration** → Land/Server wählen → Config generieren
(gibt `PrivateKey`, `Address`, `PublicKey`, `Endpoint`). Den **gesamten Text** der `.conf`
brauchst du gleich.

## 2. Container konfigurieren (Unraid → Edit)
Das Template bringt schon mit: `--cap-add=NET_ADMIN --device=/dev/net/tun` (in *Extra Parameters*).
Dann **eine** der beiden Varianten:
- **Variable `MULLVAD_WG_CONF`** = kompletter Inhalt deiner Mullvad-`.conf` (einfügen), **oder**
- Datei unter **`/data/mullvad.conf`** ablegen (im appdata-Ordner `…/twitch-miner-manager/`).

Optional: `MULLVAD_FULL_TUNNEL=true` routet *alles* über Mullvad (Standard `false` =
WebUI bleibt im LAN). `AllowedIPs`/`DNS` werden automatisch passend gesetzt — du musst
in der Config nichts anpassen.

Container starten. Im Log sollte stehen: `>>> WireGuard up. Using Mullvad DNS …`.
(Fehlt der Tunnel, läuft die App trotzdem — nur die Relays sind dann nicht erreichbar.)

## 3. Relays hinzufügen & zuweisen
WebUI → **Proxys → „Mullvad"** → Land (z. B. `de`) + Anzahl → **Relays hinzufügen**
(werden ohne Test angelegt). Danach **„Alle testen"** → jetzt sollten sie ✅ sein
(jetzt sind sie über den Tunnel erreichbar). Dann pro Account ein Relay zuweisen.

## 4. Failover (automatisch)
Der Backend-Monitor prüft laufende Accounts regelmäßig auf **Twitch-Erreichbarkeit**
und reagiert auf Laufzeit-Connection-Fehler. Fällt ein Relay aus → automatisch auf ein
anderes funktionierendes umziehen; ist keins frei → im Notfall direkt weiter, und wieder
auf einen Proxy gelegt, sobald einer verfügbar ist.
ENV-Tuning: `PROXY_CHECK_INTERVAL` (Sek, Default 120), `PROXY_FAIL_THRESHOLD` (Default 2),
`PROXY_ALLOW_DIRECT` (Default true), `PROXY_MONITOR_ENABLED` (Default true).

## Voraussetzungen / Hinweise
- **NET_ADMIN-Cap + `/dev/net/tun`** sind nötig (Template setzt sie). Ohne sie schlägt
  `wg-quick up` fehl (Log-Warnung) und die App läuft ohne Tunnel weiter.
- Unraid-Kernel hat WireGuard-Support → kein extra Modul nötig.
- Mullvad-IPs sind VPN-/Datacenter-IPs; Twitch kann eher Login-Verifizierung verlangen.
  Für reines Punkte-Mining meist unkritisch, aber nicht „unsichtbar".
