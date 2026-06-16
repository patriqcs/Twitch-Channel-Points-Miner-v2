#!/usr/bin/env bash
#
# Stoppt alle laufenden Miner-Prozesse (run.py), die von diesem Ordner gestartet wurden.

set -uo pipefail
cd "$(dirname "$0")"

PIDS=$(pgrep -f "run.py" || true)
if [ -z "$PIDS" ]; then
  echo "Keine laufenden Miner gefunden."
  exit 0
fi

echo "Stoppe Miner-Prozesse: $PIDS"
# shellcheck disable=SC2086
kill $PIDS
echo "Gesendet (SIGTERM). Prüfen mit: pgrep -af run.py"
