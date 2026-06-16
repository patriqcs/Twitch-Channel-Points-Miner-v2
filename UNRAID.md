# Twitch Miner auf Unraid – alle Accounts in EINEM Container

Fertiges Image in deiner GitHub Container Registry (privat – deine Unraid-Docker
ist als `patriqcs` eingeloggt und kann es ziehen):

```
ghcr.io/patriqcs/twitch-miner-multi:latest
```

Du brauchst **nur einen Container**. Über die Variable `ACCOUNTS` startet er pro
Account einen eigenen Prozess; alle schauen dieselben `STREAMERS`.

## Container anlegen

Unraid → **Docker** → **Add Container** → *Advanced View*:

| Feld | Wert |
|---|---|
| **Name** | `twitch-miner` |
| **Repository** | `ghcr.io/patriqcs/twitch-miner-multi:latest` |
| **Variable** | Key `ACCOUNTS` → Value `acc01,acc02,acc03,acc04,acc05,acc06,acc07,acc08,acc09,acc10` |
| **Variable** | Key `STREAMERS` → Value `streamer1,streamer2,streamer3` |
| **Variable** *(optional)* | Key `TZ` → Value `Europe/Berlin` |
| **Path** | Container `/usr/src/app/cookies` → Host `/mnt/user/appdata/twitch-miner/cookies` |

→ **Apply**. Trag bei `ACCOUNTS` deine echten Twitch-Usernamen ein (Komma-getrennt,
so viele du willst), bei `STREAMERS` die gemeinsamen Streamer.

## Einloggen (einmalig pro Account, kein Passwort/2FA)

Twitch nutzt einen Geräte-Code. Nach dem Start: Container anklicken → **Logs**.
Pro Account erscheint dort:
```
[acc01] Open https://www.twitch.tv/activate
[acc01] and enter this code: WSRYBXSD
[acc02] and enter this code: K7P2NDQA
...
```
→ `https://www.twitch.tv/activate` öffnen, jeweils **mit dem passenden Account
eingeloggt**, den Code eintippen, bestätigen. Mach das für jeden Account
(jeder Code ist 30 Min gültig). Jede Log-Zeile ist mit `[username]` markiert.

Danach liegen die Logins unter `/mnt/user/appdata/twitch-miner/cookies/<username>.pkl`
und überstehen Neustarts/Updates – kein erneuter Login nötig.

## Bedienung

| Aktion | Wo |
|---|---|
| Status / Login-Codes / Logs | Docker-Tab → Container → **Logs** |
| Account hinzufügen/entfernen | Variable `ACCOUNTS` bearbeiten → Apply |
| Streamer ändern | Variable `STREAMERS` bearbeiten → Apply |
| Image aktualisieren | Container → **Force Update** |

## Hinweise

- Ein Container, viele Prozesse: fällt ein Account aus, laufen die anderen weiter.
- **RAM:** Analytics ist aus → sparsam, auch bei 10 Accounts.
- ⚠️ **Ban-Risiko:** 10 Accounts von derselben IP auf identische Streamer sieht
  für Twitch nach View-Botting aus (ToS-Verstoß) – einzelne/alle Accounts können
  gesperrt werden. Deine Entscheidung.

---

### Lieber pro Account ein eigener Container?

Geht auch: denselben Image-Link nehmen, aber statt `ACCOUNTS` die Variable
`TWITCH_USERNAME=acc01` setzen (ein Container pro Account). Oder per
„Docker Compose Manager" die `docker-compose.yml` aus dem Repo verwenden.
