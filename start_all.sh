#!/usr/bin/env bash
#
# Startet pro Account aus accounts.txt einen eigenen Miner-Prozess im Hintergrund.
# Alle Accounts schauen dieselben Streamer (aus streamers.txt).
#
# WICHTIG: Jeder Account muss EINMAL interaktiv eingeloggt werden, damit ein
# Cookie (cookies/<username>.pkl) entsteht. Das geht im Hintergrund nicht (2FA).
# Dafür legt dieses Skript fehlende Logins zuerst nacheinander interaktiv an
# und startet danach alle Accounts im Hintergrund.

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
mkdir -p logs cookies

mapfile -t ACCOUNTS < <(grep -vE '^\s*(#|$)' accounts.txt)

if [ "${#ACCOUNTS[@]}" -eq 0 ]; then
  echo "Keine Accounts in accounts.txt eingetragen."
  exit 1
fi

# 1) Fehlende Logins zuerst interaktiv anlegen (Passwort + 2FA)
for u in "${ACCOUNTS[@]}"; do
  if [ ! -f "cookies/${u}.pkl" ]; then
    echo ""
    echo "=== Erst-Login für Account: ${u} ==="
    echo "Bitte Passwort und ggf. 2FA eingeben. Danach läuft der Account kurz an;"
    echo "sobald 'Login successful' / der Miner startet, mit STRG+C abbrechen."
    "$PY" run.py "$u" || true
  fi
done

# 2) Alle Accounts im Hintergrund starten
echo ""
echo "=== Starte alle Accounts im Hintergrund ==="
for u in "${ACCOUNTS[@]}"; do
  if [ ! -f "cookies/${u}.pkl" ]; then
    echo "  ! ${u}: kein Cookie -> übersprungen (Login fehlgeschlagen?)"
    continue
  fi
  nohup "$PY" run.py "$u" > "logs/${u}.log" 2>&1 &
  echo "  + ${u}  (PID $!, Log: logs/${u}.log)"
done

echo ""
echo "Fertig. Live mitlesen:   tail -f logs/*.log"
echo "Alle stoppen:            ./stop_all.sh"
