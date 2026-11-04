# Plan: Pokémon-Chat-Badges automatisch farmen (Hauptaccount patriQcs)

> **Status: UMGESETZT (2026-11-04)** — Backend `backend/quest.py` (Logik, Tests in `tests/test_quest.py`),
> `backend/quest_manager.py` (Thread: Poller, Lane-Rotation, Watch-Beacons), `backend/routers/quest.py`
> (`/api/quest/*`), UI-Tab „Badges“ (`frontend/src/pages/Quests.tsx`). Modul per UI-Schalter aktivieren.
> Quellen: Twitch-Blog 24.08. + 16.10.2026, StreamDatabase, Twitch-Web-Bundles (GQL-Operationen
> extrahiert), Live-Abfrage des Account-Stands im Container (read-only).

## 1. Was die „Drops" wirklich sind

Die Pokémon-Badges sind **keine klassischen Game-Drops** (`DropCampaign` / `timeBasedDrops`,
die der Miner heute über `Inventory`/`ViewerDropsDashboard` abfragt), sondern Twitchs neues
**„Reward-Drops"/Quest-System**: GQL-Typ `DropsCampaign` mit `rewardGroups`, Tageszähler
(`repeatableConfig`) und einem **Track/Lane-Modell** („equip" eines Pokémon im Drops-Panel).

Regeln (aus den Kampagnendaten, nicht aus Blogposts):

| Feld | Wert | Bedeutung |
|---|---|---|
| `requirementType` | `WATCH`, `minutesWatched: 20` | 20 Minuten in einer passenden Kategorie |
| `repeatableTimes` | 3 | an 3 Tagen |
| `resetIntervalSeconds` | 86400 | ein „Tag" = rollierendes 24-h-Fenster ab erster gezählter Minute (`currentWindow.startedAt/expiresAt`) |
| `shouldCompleteAllRepeatsToEarn` | true | alle 3 Tage nötig, Tage müssen nicht aufeinanderfolgen |
| `isAutoProgressable` | false | zählt nur, wenn die Lane ausgewählt („equipped") ist |
| `categories` | 21 Kategorien | alle Pokémon-Kategorien + Just Chatting, DJs, Kunst, Besondere Events, Sport, Musik, Talkshows & Podcasts, Tiere/Aquarien/Zoos, Co-working, Herstellen & Basteln |
| `channels` | null | jeder Live-Kanal in diesen Kategorien zählt |

Evolution-Kampagne: `3e7f0c98-893c-47a7-9044-1c61173b8f94` „Pokemon Evolutions", ACTIVE
2026-10-16 → **2027-01-30 22:59:59 UTC**. Track `pokemon#aebee668-75ca-4626-99f5-168d369d217b`,
je Starter eine Lane (Lane-ID = Item-ID des verdienten Starter-Badges).

## 2. Ist-Stand patriQcs (Live-Abfrage 2026-11-04 ~21:00)

- Besitzt: **Pichu, Bisasam, Glumanda, Shiggy** (alle 4 Starter). Kein Gastly-Ball → Haunter/Gengar nicht möglich.
- Neue Starter sind seit 01.10./01.11. nicht mehr verdienbar; nur noch **Evolutionen** (7 Stufen):
  Pichu→Pikachu (1), Bisasam→Bisaknosp→Bisaflor (2), Glumanda→Glutexo→Glurak (2), Shiggy→Schillok→Turtok (2).
- Aktuell ausgewählte Lane: **Glumanda → Glutexo**, Stand `grantCount=2` (2 von 3 Tagen),
  heutiges Fenster seit 19:24 UTC, 8/20 Minuten. Pikachu-Lane 0/3 Tage (1 Minute), Schillok/Bisaknosp 0/3.
- Der Miner hilft dem Hauptaccount dabei **heute nicht**: patriQcs schaut nur `j4nkttv` (Kategorie
  Counter-Strike, nicht qualifiziert) und ist per `COVER_EXCLUDE` von den Tarn-Kanälen (meist Just Chatting) ausgenommen.
- Rechnung: 7 Stufen × 3 Tage, nur eine Lane zählt gleichzeitig → **mind. ~20 Tage** am Stück; Deadline 30.01.2027 lässt viel Luft.
  Falls sich zeigt, dass nicht-ausgewählte Lanes doch mitzählen (empirisch am ersten Tag prüfen), sind es nur 6 Tage.

## 3. Benötigte GQL-Operationen (aus den Web-Bundles extrahiert, Volltext-Queries)

Alle lassen sich über den vorhandenen Backend-Helfer `redeem._gql(token, proxies, op, query, vars, extra_headers=account_fp)` schicken (TV-Signatur, Token bleibt im Container).

| Zweck | Operation | Kern |
|---|---|---|
| Fortschritt aller laufenden Reward-Drops | `ViewerRewardDropInventory` | `currentUser.inventory.viewerRewardDropCampaignsInProgress { rewardGroups { id name track lane progressCriteria{…} self { status currentMinutesWatched grantCount nextEligibleAt currentWindow{startedAt expiresAt} earnedReward{…} } rewards{…} } }` |
| Fortschritt je Kanal (inkl. Kategorie-Check) | `DropChannelCampaignsProgress($channelID)` | `channelDropCampaignsProgress(channelID:)` – gleiche Felder + `progressCriteria.categories { id displayName }` |
| Welche Lane ist ausgewählt | `ProgressionTrackSelections($tracks:[ID!]!)` | `trackSelections(tracks:) { id track lane }` |
| Lane wechseln („equip") | **Mutation** `SetProgressionTrackSelection` | `setTrackSelection(input: {track: <track>, lane: <lane>}) { error }` |
| Besessene Badges | `PokemonOwnedBadges` | `currentUser.availableBadges { id setID title }` |
| Verdiente Reward-Items | `BadgeCollectionEarnedRewards` | `inventory.earnedDropRewards(first:100)` |
| Badge im Chat tragen (optional) | Mutation `ChatSettings_SelectGlobalBadge` | `selectGlobalBadge(input:)` |

**Korrektur (Live-Test 2026-11-04):** Für Reward-Drops zählt das Spade-Beacon des Miners NICHT (0 Minuten bei 60 s
und 20 s-Takt). Gezählt werden nur die Player-Events, die der Web-Player an die Edge-Tracking-URL des HLS-Knotens
schickt (base64 im Usher-Master-Manifest `#EXT-X-TWITCH-INFO C=…`; Body `data=<base64 JSON>`; Felder u. a. serving_id,
video_session_id, play_session_id, device_id, platform=web, minutes_logged), plus echte Segment-Downloads. So umgesetzt
in `quest_manager._player_tick`; Ergebnis: 1 Minute pro Minute. Ursprüngliche Annahme (überholt): Die Watch-Zählung
läuft über denselben Spade-`minute-watched`-Beacon, den der Miner schon sendet (`channel_id`, `broadcast_id`, `game`/`game_id` bei `claim_drops=True`). Twitch ordnet die
Kategorie serverseitig über den Broadcast zu; es muss also nur ein **Live-Kanal in einer
qualifizierten Kategorie** geschaut werden. Ein Drop-Fortschritt wird nur auf einem Kanal
gleichzeitig gezählt (j4nkttv ist nicht qualifiziert → kein Konflikt).

Erzeugte Hilfsmittel (Scratchpad dieser Session, nicht im Repo): `extract_ops.js`/`printer.js`
(GraphQL-AST aus den Bundles → Query-Text), `probe_pokemon.py` (Read-only-Abfrage im Container).

## 4. Umsetzung: Backend-Modul „Badge-Quests“ (nur Hauptaccount)

Abweichung vom ursprünglichen Vorschlag: Das Modul sendet die Watch-Beacons **selbst** (gleicher Pfad wie der
Miner: PlaybackAccessToken → usher → Segment-HEAD → Spade), statt die Streamer-Liste des laufenden Miners zu
ändern — kein Restart des Hauptaccount-Miners nötig. Außerdem **Lane-Rotation**: jede Lane hat ihr eigenes
24-h-Fenster, also nach vollem Tag sofort zur nächsten Lane wechseln (bis zu 4 × 20 min am Tag statt 1 × 20 min);
zählt Twitch das nicht, greift der Stall-Fallback (8 min ohne Fortschritt → Lane bis Fensterende pausieren).
Sind alle Lanes für heute voll, pausiert das Modul bis zum frühesten Fensterablauf.

Ursprünglicher Vorschlag:

Analog zu `heist_manager.py`/`cover.py`, zentral im Backend, kein Eingriff in den Miner-Kern:

1. **Poller (alle 5 min, nur `QUEST_ACCOUNTS`, Default `patriQcs`)**
   `ViewerRewardDropInventory` + `ProgressionTrackSelections` → Zustand je Lane (Tage, Minuten, Fenster) in DB/UI.
2. **Lane-Steuerung**
   Konfigurierbare Reihenfolge (Default: angefangene Lane zuerst, dann Glurak, Bisaflor, Turtok, Pikachu).
   Ist die ausgewählte Gruppe fertig (`grantCount == repeatableTimes` bzw. `earnedReward` gesetzt),
   `SetProgressionTrackSelection` auf die nächste Lane. Bei zweistufigen Linien erscheint die zweite
   Stufe (Glurak) als eigene rewardGroup derselben Lane – nach dem Equip des neuen Badges prüfen,
   ob Twitch die Lane automatisch weiterführt oder ein erneuter `setTrackSelection` nötig ist.
3. **Quest-Watch**
   Solange die aktive Lane heute noch Minuten braucht (kein Fenster oder `currentMinutesWatched < 20`
   und `grantCount < 3`): dem Hauptaccount **einen** zusätzlichen Live-Kanal aus dem Cover-Pool
   geben, dessen aktuelle Kategorie in `progressCriteria.categories` liegt (Just Chatting ist im Pool
   reichlich vertreten), für ~25–30 Minuten mit Jitter, dann wieder entfernen. Der Miner schaut max.
   2 Streams (j4nkttv + Quest-Kanal). Tagesfenster ergibt sich automatisch aus dem ersten Beacon.
   Variante ohne Code: einen Just-Chatting-Kanal in die globale Streamer-Liste nehmen – verworfen,
   weil die Liste für alle Accounts gilt.
4. **UI**: Tab „Badges" mit Collection-Stand, aktiver Lane, Tag x/3, Minuten, nächstes Fenster, manuelles Umschalten.
5. **Benachrichtigung** bei Stufe erreicht / Fehler (`setTrackSelection.error`), Logging wie bei Drops.

Aufwand: Backend-Modul + Router + kleiner Frontend-Tab, ca. 1 Tag. Risiko für den Hauptaccount:
gering (Lesen + eine Auswahl-Mutation, die der Web-Client identisch schickt; Watch über den
vorhandenen Beacon vom Heim-IP wie bisher).

## 5. Weiterentwicklung

- Dieselbe Mechanik deckt **alle** Reward-Drops ab (z. B. Streamer-Badges mit `minutesWatched` 240
  wie „HoB Craft Attack" im Inventar) – Modul generisch halten („rewardGroups mit WATCH-Kriterium").
- Neue Pokémon-Runden (Twitch kündigt regelmäßige Runden an) erscheinen automatisch im
  `viewerRewardDropCampaignsInProgress`, sobald ein Ball vorhanden ist; Bälle selbst setzen Sub/Gift voraus (`requirements.subs`).
- Für Alt-Accounts bewusst nicht: zusätzlicher Traffic ohne Nutzen, und Sub-gebundene Bälle entfallen ohnehin.

## 6. Quellen

- https://blog.twitch.tv/en/2026/08/24/pokemon-chat-badges-are-now-on-twitch/
- https://blog.twitch.tv/en/2026/10/16/pokemon-evolutions-and-night-out/
- https://www.streamdatabase.com/events (Pokémon-Evolutions, Night Out)
- https://lovergirlplays.com/blog/pokemon-twitch-drops/ (Sekundärquelle, Fristen)
- Twitch-Web-Bundles (assets.twitch.tv, Chunk `68499-…js`): Operationen `PokemonCampaignSections`, `ViewerRewardDropInventory`, `SetProgressionTrackSelection`, `DropChannelCampaignsProgress`
