#!/usr/bin/env bash
#
# Zwei Modi:
#   1) MULTI:  ENV ACCOUNTS="acc1,acc2,acc3"  -> ein Prozess pro Account in EINEM Container
#   2) SINGLE: ENV TWITCH_USERNAME=acc  (oder Argument)  -> nur ein Account
#
# Streamer kommen für alle aus ENV STREAMERS oder streamers.txt.
set -u
cd /usr/src/app

if [ -n "${ACCOUNTS:-}" ]; then
  # Komma/Leerzeichen/Zeilenumbruch als Trenner. -d '' liest die GANZE Eingabe
  # (inkl. Zeilenumbrüchen) statt nur bis zum ersten \n — sonst würden Accounts
  # nach einem Zeilenumbruch stillschweigend verschluckt.
  IFS=$', \n' read -r -d '' -a accs <<< "$ACCOUNTS" || true

  pids=()
  shutdown() {
    echo ">>> stoppe alle Accounts..."
    kill "${pids[@]}" 2>/dev/null
  }
  trap shutdown SIGTERM SIGINT

  for a in "${accs[@]}"; do
    [ -z "$a" ] && continue
    echo ">>> starte Account: $a"
    TWITCH_USERNAME="$a" python -u run.py "$a" &
    pids+=("$!")
  done

  # Laufen lassen, bis alle Prozesse enden (oder Container gestoppt wird)
  wait
else
  exec python -u run.py "$@"
fi
