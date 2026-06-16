# Mullvad als Proxy-Quelle (stabil, eigene IP pro Account)

Free-Proxys sterben ständig. Mullvad ist deutlich stabiler. Mullvads SOCKS5-Relays
sind aber **nur erreichbar, wenn der Container in einem Mullvad-WireGuard-Tunnel läuft**.
Setup in zwei Teilen: **(1)** Tunnel-Sidecar, **(2)** Relays in der WebUI hinzufügen.

## Wie es funktioniert
- Ein **gluetun**-Container hält die Mullvad-WireGuard-Verbindung (ein Basis-Server).
- Der Miner-Container läuft **im Netzwerk von gluetun** (`--net=container:mullvad`).
- Innerhalb des Tunnels sind Mullvads **Multihop-SOCKS5-Relays** erreichbar, z. B.
  `de-ber-wg-socks5-001.relays.mullvad.net:1080` – **jedes Relay = eigene Exit-IP**.
- In der WebUI fügst du diese Relays über **Proxys → „Mullvad"** hinzu und weist jedem
  Account ein anderes Relay zu → verschiedene IPs, alle stabil.

## 1. gluetun-Sidecar (Unraid → Add Container, Advanced)
- **Repository:** `qmcgaw/gluetun`
- **Extra Parameters:** `--cap-add=NET_ADMIN`
- **Variablen:**
  - `VPN_SERVICE_PROVIDER=mullvad`
  - `VPN_TYPE=wireguard`
  - `WIREGUARD_PRIVATE_KEY=<dein Key>` *(Mullvad-Konto → WireGuard-Konfig generieren)*
  - `WIREGUARD_ADDRESSES=<z. B. 10.x.x.x/32 aus der Mullvad-Konfig>`
  - `SERVER_COUNTRIES=Germany` *(Basis-Server; optional)*
  - `FIREWALL_OUTBOUND_SUBNETS=192.168.178.0/24` *(damit dein LAN/die WebUI erreichbar bleibt)*
- **Wichtig – Ports:** Weil der Miner gleich im gluetun-Netz hängt, wird der WebUI-Port
  **am gluetun-Container** veröffentlicht: dort Port **`8844 → 8844`** (oder `8000`) mappen,
  nicht mehr am Miner-Container.

## 2. Miner-Container ins Tunnel-Netz hängen
In der Edit-Seite des `twitch-miner-multi`-Containers:
- **Network Type:** `None`, und in **Extra Parameters** ergänzen:
  `--net=container:mullvad` *(Name des gluetun-Containers)*
- Den **Port `8844`** am Miner-Container **entfernen** (wird jetzt über gluetun veröffentlicht).
- Das `/data`-Mapping (`/mnt/user/appdata/twitch-miner-manager`) **bleibt** wie es ist.

> Hinweis: Setzt du `WEB_PORT`, dann am gluetun-Container denselben Port mappen.

## 3. Relays in der WebUI hinzufügen
Proxys → **Mullvad** → Land (z. B. `de`) + Anzahl → **Relays hinzufügen**.
Sie werden ohne Test hinzugefügt (außerhalb des Tunnels nicht erreichbar). Danach:
**„Alle testen"** – jetzt sollten sie ✅ sein. Dann pro Account ein Relay zuweisen.

## Failover
Der Backend-Monitor prüft laufende Accounts regelmäßig (Twitch-Erreichbarkeit) und
reagiert auf Laufzeit-Fehler. Fällt ein Relay aus, wird automatisch auf ein anderes
funktionierendes umgezogen; ist keins frei, läuft der Account im Notfall direkt
weiter und wird wieder auf einen Proxy gelegt, sobald einer verfügbar ist.
Tuning per ENV: `PROXY_CHECK_INTERVAL` (Sek, Default 120), `PROXY_FAIL_THRESHOLD`
(Default 2), `PROXY_ALLOW_DIRECT` (Default true), `PROXY_MONITOR_ENABLED` (Default true).

## Hinweis zu Twitch
Mullvad-IPs sind VPN-/Datacenter-IPs – Twitch erkennt die als VPN und kann eher
Login-Verifizierungen verlangen. Für reines Punkte-Mining meist unkritisch, aber
nicht „unsichtbar". Für maximale Tarnung wären Residential-Proxys nötig (teurer).
