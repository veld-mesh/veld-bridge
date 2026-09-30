#!/bin/sh
# Run from a laptop checkout: deploy/nas/push.sh [core] [wa]
# Copies the committed tree (HEAD) to the NAS, then rebuilds and restarts the
# named services (default: both). `push.sh core` leaves WhatsApp linked.
# Needs SSH key login and passwordless sudo for that user on the NAS.
# VB_HOST=user@nas (required), VB_DIR (default /volume1/docker/veld-bridge).
set -eu
NAS=${VB_HOST:?set VB_HOST=user@your-nas}
DIR=${VB_DIR:-/volume1/docker/veld-bridge}
cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain)" ]; then
  echo "uncommitted changes; only HEAD is deployed" >&2
fi
git archive --format=tar HEAD | ssh "$NAS" "sudo -n tar -x -C $DIR/src"
# shellcheck disable=SC2029  # $* is meant to expand here
ssh "$NAS" "cd $DIR/src && sudo -n /usr/local/bin/docker compose up -d --build $*"
sleep 15
curl -s -m 5 "http://${NAS#*@}:8787/health"; echo
