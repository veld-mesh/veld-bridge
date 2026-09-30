#!/usr/bin/env bash
# Fresh Raspberry Pi OS Lite (64-bit) -> running bridge. Idempotent; safe to re-run.
#   curl -fsSL https://raw.githubusercontent.com/veld-mesh/veld-bridge/main/install.sh | sudo bash
# or, from a checkout:  sudo ./install.sh
set -euo pipefail

REPO="${VB_REPO:-https://github.com/veld-mesh/veld-bridge.git}"
BRANCH="${VB_BRANCH:-main}"
RUN_USER="${VB_USER:-pi}"
PREFIX=/opt/veld-bridge
APP="$PREFIX/app"
VENV="$PREFIX/venv"
ETC=/etc/veld-bridge
STATE=/var/lib/veld-bridge
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n== %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
id "$RUN_USER" >/dev/null 2>&1 || { echo "user $RUN_USER does not exist" >&2; exit 1; }

log "apt packages"
apt-get update -q
apt-get install -y -q git ca-certificates curl python3 python3-venv chromium sqlite3

if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 22 ]; then
  log "Node.js 22 (NodeSource)"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -q nodejs
fi

log "app checkout in $APP"
install -d -o "$RUN_USER" -g "$RUN_USER" "$PREFIX"
if [ ! -d "$APP/.git" ]; then
  sudo -u "$RUN_USER" git clone --branch "$BRANCH" "$REPO" "$APP"
fi

log "python venv"
[ -d "$VENV" ] || sudo -u "$RUN_USER" python3 -m venv "$VENV"
sudo -u "$RUN_USER" "$VENV/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$VENV/bin/pip" install -q -e "$APP/core[mesh]"

log "node deps (system chromium, no puppeteer download)"
(cd "$APP/wa" && sudo -u "$RUN_USER" env PUPPETEER_SKIP_DOWNLOAD=1 npm ci --omit=dev --no-audit --no-fund)

log "config in $ETC"
install -d -m 0750 -o root -g "$RUN_USER" "$ETC"
[ -f "$ETC/config.yaml" ] || install -m 0640 -o root -g "$RUN_USER" "$APP/config.example.yaml" "$ETC/config.yaml"
[ -f "$ETC/wa.env" ] || install -m 0640 -o root -g "$RUN_USER" "$APP/deploy/wa.env.example" "$ETC/wa.env"

log "state dir $STATE (0700: holds the WhatsApp session)"
install -d -m 0700 -o "$RUN_USER" -g "$RUN_USER" "$STATE"
usermod -aG dialout "$RUN_USER"

log "systemd units"
install -m 0644 "$APP/deploy/veld-bridge-core.service" /etc/systemd/system/
install -m 0644 "$APP/deploy/veld-bridge-wa.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now veld-bridge-core.service veld-bridge-wa.service

"$VENV/bin/veldbridge" --config "$ETC/config.yaml" --check || true

cat <<EOF

Installed. Next:
  1. Edit $ETC/config.yaml: mesh.serial_path and mesh.pocket_node (see README).
  2. sudo systemctl restart veld-bridge-core
  3. Link WhatsApp: journalctl -u veld-bridge-wa -f   (scan the QR, or open $STATE/qr.png)
  4. Health: curl -s http://localhost:8787/health
EOF
