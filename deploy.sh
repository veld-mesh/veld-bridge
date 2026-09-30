#!/usr/bin/env bash
# Update the Pi to the latest commit and restart. Run on the Pi: sudo /opt/veld-bridge/app/deploy.sh
set -euo pipefail

RUN_USER="${VB_USER:-pi}"
APP=/opt/veld-bridge/app
VENV=/opt/veld-bridge/venv
ETC=/etc/veld-bridge

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
cd "$APP"

old=$(sudo -u "$RUN_USER" git rev-parse HEAD)
sudo -u "$RUN_USER" git pull --ff-only
new=$(sudo -u "$RUN_USER" git rev-parse HEAD)
echo "deploy $old -> $new"

changed() { [ "$old" != "$new" ] && sudo -u "$RUN_USER" git diff --name-only "$old" "$new" | grep -q "$1"; }

if changed '^core/pyproject.toml'; then
  sudo -u "$RUN_USER" "$VENV/bin/pip" install -q -e "$APP/core[mesh]"
fi
if changed '^wa/package'; then
  (cd wa && sudo -u "$RUN_USER" env PUPPETEER_SKIP_DOWNLOAD=1 npm ci --omit=dev --no-audit --no-fund)
fi
if changed '^deploy/.*\.service'; then
  install -m 0644 deploy/veld-bridge-core.service deploy/veld-bridge-wa.service /etc/systemd/system/
  systemctl daemon-reload
fi

"$VENV/bin/veldbridge" --config "$ETC/config.yaml" --check

systemctl restart veld-bridge-core.service
systemctl restart veld-bridge-wa.service
sleep 5
curl -fsS http://localhost:8787/health && echo
