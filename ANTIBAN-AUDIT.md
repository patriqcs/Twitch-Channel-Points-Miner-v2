# Anti-Ban-Härtungs-Audit (2026-07-14)

## Umsetzungsstatus (Branch `antiban-hardening-2026-07-14`)

Umgesetzt + verifiziert (Import/Compile/Typecheck/Logik-Tests grün): **K1, K2,
H1, H3, H5, M1, M2, M3(Teil), M4, M5, M6, M7, N1, N2, N4.**

- **H2** — bewusst NICHT im Code (rein operativ: Residential-Proxys eintragen).
- **H4 (TLS/JA3)** — bewusst ZURÜCKGESTELLT, nicht in diesem Branch. Grund: ein
  Umstieg der HTTP-/WS-Transportschicht auf `curl_cffi`/`tls-client` ist ein
  mehrdateiiger, abhängigkeits­einführender Umbau, der auf einer Live-Flotte hier
  nicht end-to-end testbar ist — und falsch gemacht (z.B. Chrome-JA3 hinter
  TV-App-UA) erzeugt er einen NEUEN Widerspruch statt ihn zu beheben. Braucht ein
  eigenes, live-getestetes Vorgehen mit einem Android/OkHttp-Profil hinter einem
  Default-off-Flag. Siehe H4 unten.
- **M3** — der sichere Teil (ERR_BADAUTH terminal schließen) ist drin; der aktive
  OAuth-Refresh-Flow ist zurückgestellt (ungetesteter Auth-Pfad auf Live-Flotte;
  bestehendes Auto-Pull fängt das Ban-Signal bereits ab).
- **N3** — Anzahl-Streuung umgesetzt; Pool-Vergrößerung bewusst NICHT mit
  erfundenen Kanalnamen (ein Tarn-Kanal muss real+groß+häufig-live sein) —
  operativ über die UI zu erweitern.

Betriebs-Env fürs Deployment zusätzlich empfohlen: `PROXY_ALLOW_DIRECT=false`,
`MAX_ACCOUNTS_PER_PROXY=1`, `MULLVAD_FULL_TUNNEL=false` (Code-Defaults sind jetzt
schon sicher; die Envs machen es explizit).

---


> Vollständiger Code-Audit gegen Twitch-Bans, durchgeführt über 3 parallele
> Prüf-Cluster (Fingerprint/Auth/TLS · Timing/Verhalten/Wetten ·
> Account-Graph/Proxy/IRC). Alle Befunde sind am echten Code mit Datei:Zeile
> belegt. Randbedingungen: `patriqcs` bleibt in jedem Pfad ausgenommen,
> garantierte Boni (Raid, Stream-Start, Bonus-Claim) werden von keinem Fix
> berührt.

## Kurzfazit

Die bestehende Anti-Detection-Suite ist auf der Verhaltensebene stark (zentraler
Live-Poll, gestaffeltes Ramp-up, Timing-Jitter, Warm-up, Tarn-Kanäle, Tag/Nacht,
Session-Churn, Per-Account-Fingerprint). Die verbleibenden Ban-Risiken liegen in
drei Bereichen, die das bisherige Tuning **nicht** abdeckt:

1. **Transport-/Netzwerk-Ebene** — Klartext-IRC (6667) im Miner-Core, Direct-IP-
   Fallback auf die echte Heim-IP, Datacenter-Only-Exits (Mullvad). Das ist die
   billigste UND wirkungsvollste Angriffsfläche.
2. **Koordinations-Signale** — All-in-Wetten aller Accounts auf **dieselbe Seite**
   und eine **fleet-weit identische** Watch-Kadenz. Das sind aktive
   Cartel-/Kohorten-Tells, die kein Fingerprint verdeckt.
3. **Client-Kohärenz** — TLS/JA3 verrät den Python-Stack hinter TV-App-UA;
   Client-Version ist inkonsistent (Miner dynamisch, Backend stale) und wird pro
   GQL-Call über einen Homepage-GET geholt.

**Top-3-Hebel:** (a) Residential-IP je Account + Direct-Fallback aus (operativ,
kein Code); (b) Wett-Richtung/Teilnahme streuen; (c) Klartext-IRC schließen.

---

## Risiko-Register (nach Schwere)

### KRITISCH

**K1 · Klartext-IRC (Port 6667) im Miner-Core, Chat für jeden Account an** ·
Netzwerk-Transport · verifiziert
`TwitchChannelPointsMiner/constants.py:6` (`IRC_PORT = 6667`), `Chat.py:75-78`
(sendet `oauth:<TV-Token>` im Klartext), `miner_runner.py:424`
(`chat=ChatPresence.ONLINE` für alle). Das Backend-Heist-Modul läuft bereits auf
TLS 6697 (`backend/heist.py:38,292`), der Miner-Core-Chatpfad aber nicht.
**Tell:** Kein echter Client spricht unverschlüsseltes IRC 6667 — echte
Chatsitzungen laufen WSS/TLS. Ein TV-Token, der im Klartext über 6667
authentifiziert, ist auf Transportebene sofort als Bot klassifizierbar; N
Accounts mit exakt diesem Muster = Netzwerksignal.
**Fix:** `Chat.py` analog `heist.py` auf TLS wrappen + `IRC_PORT=6697`, ODER
(empfohlen, wenn Chat-Präsenz nicht gebraucht wird) `chat=ChatPresence.NEVER` in
`miner_runner.py:424` → IRC-Verbindung entfällt komplett.
**Aufwand:** klein / trivial. **Nebenwirkung:** keine für Boni; patriqcs sollte
ebenfalls TLS bzw. Chat-aus fahren.

**K2 · All-in-Wette: alle Accounts setzen auf DASSELBE Outcome** ·
Koordination · verifiziert
`backend/prediction.py:441,536` — ein einziges `outcome_id` geht an jeden
Kandidaten; gestreut wird nur Höhe (`pct = random.uniform`, `:525`) und Zeitabstand
(`:560`), **nie die Richtung**.
**Tell:** ~12 Accounts, die (a) nur bei Predictions dieses einen Kanals aktiv
werden, (b) alle dieselbe Seite wählen, (c) bis zu 70–100 % setzen = klassisches
koordiniertes Wett-Cartel, serverseitig direkt auswertbar. %- und Timing-Streuung
verschleiern Höhe/Takt, nicht die Korrelation (Richtung + Selektivität).
**Fix:** (1) Teilnahmequote < 100 % — pro Runde neu würfeln, 10–30 % auslassen;
(2) gelegentlich einige Accounts aufs Gegen-Outcome (nur 2-Outcome-Events);
(3) Fenster verbreitern (Default `spacing 3–15s`, `:112-113` → bei 12 Accounts
oft auf Sekunden gestaucht); (4) Einsatz-Cap-Streuung nach unten.
**Aufwand:** mittel (nur `prediction.py` + UI-Tunable). **Nebenwirkung:** leicht
niedrigere Gewinn-Erwartung (bewusste Anti-Detection-Kosten); patriqcs via
`excluded_usernames` ausgenommen.

### HOCH

**H1 · Direct-Fallback bei Proxy-Ausfall → Egress über die echte Heim-IP** ·
IP/Proxy · verifiziert
`backend/config.py:73` (`PROXY_ALLOW_DIRECT` Default **True**),
`proxy_monitor.py:189-193` (setzt `proxy_id=None` wenn kein Relay frei). Container
läuft im Split-Tunnel (`docker-entrypoint-web.sh:37-39`, nur `10.64.0.0/10` über
`wg0`) → ein Account ohne Proxy routet **nicht** über Mullvad, sondern über die
echte Heim-/ISP-IP.
**Tell:** Ein Farm-Account (voller TV-Fingerprint, folgt j4nkttv) taucht plötzlich
von der residential Heim-IP auf — genau der IP, an der auch der echte patriqcs
hängt. Verknüpft die Bot-Flotte direkt mit der echten Person; bei Sammelausfall
teilen sich mehrere Bots diese eine IP.
**Fix:** `PROXY_ALLOW_DIRECT=false` (Env; besser Default im Code drehen). Dann
greift `proxy_monitor.py:194-196` „no replacement" → Account pausiert statt zu
leaken. „Lieber pausiert als geleakt."
**Aufwand:** trivial. **Nebenwirkung:** patriqcs ausgenommen (`no_proxy=True`,
`models.py:47-50`; übersprungen in `proxy_monitor.py:158-160`).

**H2 · Datacenter-Only-Exits (Mullvad), kein Residential** · IP/Proxy · operativ ·
verifiziert
`MULLVAD.md:50`, `proxy_util`/`routers/proxies.py:160-205`. Alle ~13 Accounts
sitzen in einem AS/Datacenter-Adressraum → als VPN/Datacenter klassifizierbar,
unabhängig von jedem Verhaltens-Tuning. **Größter offener Hebel.**
**Fix (kein Code nötig — System kann es schon):** Pro Account einen Residential-
SOCKS5/HTTP-Endpoint über **Proxys → Bulk-Import** eintragen
(`scheme://user:pass@host:port`), als **`socks5h`** (Remote-DNS) statt `socks5`,
damit auch DNS am Residential-Exit auflöst. Mullvad ggf. nur als Failover behalten
— dann zwingend mit H1 (`PROXY_ALLOW_DIRECT=false`).
**Aufwand:** rein operativ (Proxys beschaffen + eintragen).

**H3 · Watch-Kadenz fleet-weit identisch** · Timing-Korrelation · verifiziert
`Twitch.py:588` — `next = now + uniform(0.85,1.15) * 20/len(streamers)`. Alle
Accounts senden minute-watched mit **identischer Basis (20s)** und **identischem
Jitter-Band (±15 %)**. Phase driftet auseinander (gut), aber Rate/Varianz sind
eine fleet-weite Konstante → über Minuten gemitteltes Sende-Intervall-Histogramm
ist bei allen ~13 Accounts identisch (Peak 20/N s). 20s ist zudem aggressiv
(Web-Client ~60s).
**Fix:** Pro Account persistente Basis-Kadenz (z.B. 18–26s, aus device_id
abgeleitet) + pro Account leicht variabler Jitter-Umfang. Env-Tunable
`MINER_WATCH_BASE`/`MINER_WATCH_JITTER`.
**Aufwand:** niedrig. **Nebenwirkung:** Watch-Streak (braucht < 7 min) bleibt
sicher solange Basis << 60s.

**H4 · TLS/JA3-Fingerprint = Python-Stack hinter TV-App-UA** · TLS/JA3 ·
verifiziert (Stack) / plausibel (JA3-Detail)
Alle HTTP-Calls über `requests.Session` (urllib3 + Python-OpenSSL, HTTP/1.1);
PubSub über `websocket-client`. Kein `curl_cffi`/`tls-client` im Repo.
**Tell:** ClientHello (JA3), ALPN und HTTP/1.1-statt-HTTP/2 gehören zum
Python/OpenSSL-Stack, der präsentierte UA ist aber Android-TV-App
(OkHttp/BoringSSL). Twitch/Cloudflare lesen JA3+ALPN+HTTP-Version serverseitig →
**der tiefste Tell**; alle Header-Fixes sind dagegen Kosmetik.
**Fix:** HTTP-Layer auf `curl_cffi` (impersonate) / `tls-client` mit Android-
OkHttp-JA3-Profil; PubSub-WS über denselben Impersonator. Wirkungsvollster, aber
aufwändigster Hebel.
**Aufwand:** hoch (SOCKS5-Integration + Retry/Timeout-Adapter portieren,
`websocket-client` wrappen).

**H5 · Identischer StreamerSettings-Feature-Vektor über alle Accounts** ·
Account-Graph · verifiziert
`internal.py:36-55` (Farm-Streamer steht bei jedem Account zuerst),
`miner_runner.py:417-425` (byte-identische Defaults: `follow_raid`,
`claim_drops`, `claim_moments`, `watch_streak`, `chat=ONLINE` …).
**Tell:** „Alle folgen denselben Kanal mit exakt demselben Feature-Set" ist das
Kern-Botnetz-Muster; Tarn-Kanäle mildern nur die Follow-Liste, nicht die
Verhaltensdefaults. (Positiv: Client-Fingerprint ist **nicht** geteilt —
`models.py:71-73` erzeugt device_id/UA pro Account.)
**Fix:** Einige Booleans pro Account deterministisch variieren
(`claim_moments`/`follow_raid`/`community_goals` per `md5(account_id)`-Gate),
Exclude-Set (patriqcs) überspringen. Realer Haupthebel bleibt aber H2.
**Aufwand:** mittel.

### MITTEL

**M1 · Homepage-GET vor jedem GQL-Call** · Verhalten/Effizienz · verifiziert
`Twitch.py:378,411` rufen `update_client_version()` inline im Header-Dict;
`:450-467` macht jedes Mal ein ungecachtes `session.get(www.twitch.tv)`.
**Tell:** Ein Account, der sich als Android-TV-App ausgibt, lädt vor jedem
API-Call zuerst die Web-Desktop-Homepage (Homepage-GET → GQL-POST → …) — passt zu
keinem echten Client, doppelter Traffic.
**Fix:** Version einmalig bei Login/Start cachen (periodischer Refresh), in
`:378/:411` `self.client_version` statt Aufruf. **Aufwand:** niedrig.

**M2 · Client-Version inkonsistent & stale** · Fingerprint · verifiziert
Miner scrapt live das Web-Twilight-Build (`Twitch.py:378,450`); Backend nutzt
hartkodiert `TWITCH_CLIENT_VERSION = "ef92…"` (`redeem.py:326`,
`prediction.py:61`), kohortenweit identisch, nie aktualisiert.
**Tell:** Derselbe Account trägt in Miner-Traffic eine frische, in Backend-Traffic
eine seit Monaten fixe Version; Backend-Wert zudem kohortenweit gleich & stale.
**Fix:** Backend dieselbe Quelle cachen lassen wie Miner (oder für TV-Traffic
`Client-Version` ganz weglassen — TV-App sendet kein Twilight-Build).
**Aufwand:** niedrig-mittel.

**M3 · Kein Token-Refresh; ERR_BADAUTH → Fehler-Loop mit totem Token** ·
Auth-Lifecycle · verifiziert
`TwitchLogin.py:186-188,216-218` verwirft den `refresh_token`; kein Refresh-Pfad.
`WebSocketsPool.py:538-551` loggt ERR_BADAUTH nur, ohne Re-Login/Stop → bei jedem
Reconnect wird derselbe tote Token erneut ge-LISTEN't.
**Tell:** Ein Account, der dauerhaft mit abgelaufenem Token weiter-listet, erzeugt
ein auffälliges wiederkehrendes Auth-Failure-Muster (ERR_BADAUTH war Ban-Ursache
der 08.07.-Welle).
**Fix:** (a) `refresh_token` speichern + Refresh gegen `id.twitch.tv/oauth2/token`;
(b) bei ERR_BADAUTH das Re-LISTEN dieses Sockets stoppen, damit das Backend-
Ban-Signal greift statt eines stillen Loops. **Aufwand:** mittel.

**M4 · Zeitlich unveränderliche md5-Zuweisung wird selbst zum Fingerabdruck** ·
Verhalten · verifiziert
`cover.py:135-138` und `diurnal.py:64-66` seeden nur aus `account_id`, ohne
Zeitkomponente → ein Account schaut über Monate exakt dieselben 3 Tarn-Kanäle und
schläft jede Nacht im selben Fenster. Null Drift = perfekt stabiler Verhaltens-Hash.
**Fix:** Grobe Zeit-Epoche in den Seed (`…:{iso_week//3}` für Cover; ±20–30 min
Tages-Jitter fürs Schlaf-Fenster). **Aufwand:** niedrig.

**M5 · MAX_ACCOUNTS_PER_PROXY=5 → bis 5 Accounts teilen eine Exit-IP** ·
IP/Proxy · verifiziert
`config.py:43`, erzwungen `proxy_monitor.py:149-150`. Beim Failover können mehrere
Farm-Accounts (gleicher Streamer, gleiches Verhalten) auf eine IP zusammenfallen =
Netzwerk-Korrelationsgraph.
**Fix:** `MAX_ACCOUNTS_PER_PROXY=1` (genug Relays bei ~13 Accounts vorhanden).
**Aufwand:** trivial.

**M6 · Backend `_gql` Default-Client-Id ist Web — Fail-Open-Fußangel** ·
Fingerprint · verifiziert (im Scope korrekt genutzt)
`redeem.py:314,484` — Default `TWITCH_WEB_CLIENT_ID`; TV-Signatur nur via
`extra_headers`. Aktuell überall korrekt übergeben, aber jeder künftige Aufruf ohne
`fp` schickt TV-Token hinter Web-Client-Id, python-UA, ohne X-Device-Id.
**Fix:** Default auf TV-Signatur setzen (fail-safe). Zusätzlich `chat_redeemer`/
`web_redeem_manager` gegenprüfen (außerhalb dieses Audit-Scopes). **Aufwand:**
niedrig.

**M7 · Korrelierter Reconnect-Sturm bei gemeinsamem Tunnel-Ausfall** ·
Timing · plausibel
`WebSocketsPool.py:218-226` — per-Connection-Backoff, aber alle teilen den
Mullvad-Tunnel; fällt die Strecke gemeinsam aus, laufen alle dieselbe
n-Progression und geben gleichzeitig frei → geclusterte Reconnects.
**Fix:** Zufalls-Offset in den Backoff (`min(2**n,60) * uniform(0.7,1.4)`).
**Aufwand:** niedrig.

### NIEDRIG / Feinschliff

- **N1 · Accept-Language fehlt auf GQL** (`TwitchLogin.py:233` nur `en-US` beim
  Login) — pro Account plausible, geo-passende `Accept-Language` persistieren.
- **N2 · ≥3 Client-Session-Ids pro Account** (`Twitch.py:154`, `redeem.py:328`,
  `prediction.py:63`) — optional zentral eine teilen; niedrige Priorität.
- **N3 · Tarn-Kanal-Häufungen** (`cover.py`, Pool 22 / 3 pro Account, ~35 % der
  Paare teilen ≥1 Kanal) — Pool auf 40+ vergrößern, `count` 2–4 mischen.
- **N4 · Anzahl gleichzeitig Offline-Präsenter fix** (Default 2) — ±1-Wackeln.

---

## Bereits korrekt gelöst — NICHT anfassen

- **IRC läuft über den Account-Proxy** (`Chat.py:15-53,77`) — kein Real-IP-Leak zu
  Twitch (nur der Klartext-Port K1 bleibt das Problem).
- **Per-Account-Fingerprint** (device_id/ua_app/ua_web persistiert,
  `models.py:71-73`) — kein geteilter UA/device-Cluster.
- **patriqcs-Ausnahme** doppelt abgesichert (`COVER_EXCLUDE` + `no_proxy`).
- **Getrennte Exit-IP je Relay im Normalbetrieb** (Split-Tunnel,
  `MULLVAD_FULL_TUNNEL` aus) — außer beim Direct-Fallback (→ H1).
- **Ramp-up / Churn / Offline-Präsenz** (`stream_gate.py`) — Minuten-Entzerrung,
  randomisierte Fenster, zufällige Opfer.
- **Reaktive Aktionen alle gejittert** (Bonus 2–12s, Raid 1–5s, Moment 1–6s) —
  kein ms-Sofort-Feuern.
- **Client-Integrity bewusst NICHT nachrüsten** — TV-Client-Id verlangt keins;
  Nachrüstung bräuchte Headless-Browser/KPSDK-PoW. Stattdessen auf Integrity-/
  Auth-Fehler monitoren (Auto-Pull existiert). **Single-Point-of-Failure**, falls
  Twitch Integrity auch für die TV-Id erzwingt.
- **TLS CERT_NONE** — Opt-in, Default aus, serverseitig nicht als Fingerprint
  sichtbar. Kein Fix.
- **Persisted-Query-Hashes** — Fehler = `PersistedQueryNotFound` (Funktionsfehler),
  kein Detektionssignal; Prediction-Pfad hat bereits Volltext-Fallback.

---

## Operative Empfehlungen (nicht Code)

1. **Residential-Proxys je Account** (H2) — größter realer Hebel. Über bestehende
   Proxy-Tabelle als `socks5h` eintragen. Mullvad nur als Failover + `PROXY_ALLOW_DIRECT=false`.
2. **Entzerrte Account-Erstellung + Telefon-Verify** — wie in STAND.md notiert.
3. **`MAX_ACCOUNTS_PER_PROXY=1`, `PROXY_ALLOW_DIRECT=false`, `MULLVAD_FULL_TUNNEL=false`**
   als Betriebs-Env setzen.

## Offene Fragen an den Betreiber

1. Chat-Präsenz im Miner tatsächlich gebraucht (Mention-Logging) — oder darf sie
   für Farm-Accounts komplett aus (K1 → `ChatPresence.NEVER`)?
2. Bereitschaft, Residential-Proxys zu beschaffen (H2)? Falls nein, bleibt das
   Datacenter-Restrisiko der dominante Faktor.
3. Wett-Strategie: Wie viel Gewinn-Erwartung darf für Anti-Detection geopfert
   werden (Teilnahmequote, Gegenwetten) — K2?
4. Soll der aufwändige TLS/JA3-Umbau (H4) angegangen werden, oder erst nach den
   billigen Hebeln neu bewerten?
