#!/usr/bin/env bash
# Entrypoint for the web image. Optionally brings up a Mullvad WireGuard tunnel
# so the per-account SOCKS5 relays (*.socks5.relays.mullvad.net, reachable only
# inside the tunnel) work, then starts the web/API server.
#
# Mullvad config source (first match wins):
#   * MULLVAD_WG_CONF   -> full WireGuard config text (paste from Mullvad)
#   * /data/mullvad.conf -> a WireGuard config file
# Required extras on the container: --cap-add=NET_ADMIN and --device /dev/net/tun
#
# Split tunnel by default: only Mullvad's 10.64.0.0/10 (DNS 10.64.0.1 + the
# 10.124.x relays) is routed through wg0, so the WebUI stays reachable on the
# LAN. Set MULLVAD_FULL_TUNNEL=true to route everything through Mullvad.
set -u

WEB_PORT="${WEB_PORT:-8000}"
WG_CONF="/etc/wireguard/wg0.conf"
SRC=""

if [ -n "${MULLVAD_WG_CONF:-}" ]; then
  SRC="env"
  printf '%s\n' "$MULLVAD_WG_CONF" > "$WG_CONF"
elif [ -f /data/mullvad.conf ]; then
  SRC="/data/mullvad.conf"
  cp /data/mullvad.conf "$WG_CONF"
fi

if [ -n "$SRC" ]; then
  echo ">>> Mullvad WireGuard config from: $SRC"
  mkdir -p /etc/wireguard
  chmod 600 "$WG_CONF"

  # Split vs full tunnel.
  if [ "${MULLVAD_FULL_TUNNEL:-false}" = "true" ]; then
    ALLOWED="0.0.0.0/0, ::/0"
  else
    ALLOWED="10.64.0.0/10"
  fi
  # Force our AllowedIPs and drop DNS= (we manage resolv.conf ourselves so we
  # don't depend on resolvconf being installed). Add a keepalive if missing.
  sed -i -E "s|^[[:space:]]*AllowedIPs[[:space:]]*=.*|AllowedIPs = ${ALLOWED}|I" "$WG_CONF"
  sed -i -E "/^[[:space:]]*DNS[[:space:]]*=.*/Id" "$WG_CONF"
  grep -qiE '^[[:space:]]*PersistentKeepalive' "$WG_CONF" || \
    sed -i -E "/^\[Peer\]/a PersistentKeepalive = 25" "$WG_CONF"

  if wg-quick up wg0; then
    echo ">>> WireGuard up. Using Mullvad DNS (10.64.0.1) for *.relays.mullvad.net."
    printf 'nameserver 10.64.0.1\nnameserver 1.1.1.1\n' > /etc/resolv.conf
    # quick sanity: can we reach Mullvad's internal DNS host?
    if ! ping -c1 -W3 10.64.0.1 >/dev/null 2>&1; then
      echo "!!! WARN: Mullvad gateway 10.64.0.1 not reachable yet (tunnel may still be settling)."
    fi
  else
    echo "!!! WARN: 'wg-quick up wg0' FAILED. Need --cap-add=NET_ADMIN and --device /dev/net/tun,"
    echo "!!!       and a kernel with WireGuard support. Starting WITHOUT tunnel -> Mullvad relays"
    echo "!!!       will be unreachable, but the app and direct mining still work."
  fi
else
  echo ">>> No Mullvad config (MULLVAD_WG_CONF / /data/mullvad.conf) — starting without tunnel."
fi

exec uvicorn backend.main:app --host 0.0.0.0 --port "${WEB_PORT}"
