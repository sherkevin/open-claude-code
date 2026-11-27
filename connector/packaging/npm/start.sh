#!/bin/bash
set -euo pipefail
umask 077
root="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
case "$(uname -m)" in
  x86_64|amd64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo 'Unsupported Linux architecture' >&2; exit 2 ;;
esac
(cd "$root" && sha256sum -c SHA256SUMS --quiet)
binary="$root/bin/linux-$arch/kaiwu-connector"
if [ "${1:-}" = --version ] && [ "$#" = 1 ]; then
  exec "$binary" version
fi
if [ "$#" != 0 ]; then
  echo 'Usage: kaiwu-assist [--version]' >&2
  exit 2
fi
if [ "$(id -u)" = 0 ] || [ ! -t 0 ] || [ ! -t 1 ]; then
  echo 'Run in a visible interactive terminal as a customer-approved non-root account (no sudo or piped consent).' >&2
  exit 2
fi
# Endpoints are release-pinned, not taken from environment variables.
mapfile -t endpoints < "$root/endpoints.conf"
if [ "${#endpoints[@]}" != 3 ]; then
  echo 'Invalid release endpoint configuration' >&2; exit 2
fi
data="$HOME/.local/share/kaiwu-assist-npx"
if [ -L "$data" ]; then echo 'Refusing a symlinked support data directory' >&2; exit 2; fi
mkdir -p "$data"
chmod 700 "$data"
exec 9>"$data/foreground.lock"
flock -n 9 || { echo 'A Kaiwu support terminal is already running. Stop it first.' >&2; exit 2; }
exec "$binary" assist --interactive --direct --data-dir "$data" --cloud "${endpoints[0]}" --edge "${endpoints[1]}" --platform "${endpoints[2]}" --native-relay
