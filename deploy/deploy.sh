#!/usr/bin/env bash
#
# Deploy-Skript: synct den lokalen Code auf den Hetzner-Server und
# startet den RouteZero-Service neu.
#
# Auf dem Mac ausfuehren:
#   ./deploy/deploy.sh
#
# Konfiguration via Umgebungsvariablen (siehe Defaults unten).

set -euo pipefail

SERVER="${ROUTEZERO_SERVER:-route.chargeindex.eu}"
SSH_USER="${ROUTEZERO_SSH_USER:-root}"
APP_DIR="${ROUTEZERO_APP_DIR:-/opt/routezero}"
APP_USER="routezero"

echo ">>> Synct Code nach ${SSH_USER}@${SERVER}:${APP_DIR}"

# rsync mit Exclude-Liste:
# - .git, .venv, .cache: Server hat eigene Versionen
# - .env: NIE ueber rsync — nutzt scp einmalig (siehe README)
# - __pycache__, *.pyc: Build-Artefakte
# - debug_map_stop_*.html: Dev-Output
# - .DS_Store: Mac-Kruscht
rsync -avz --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '.cache' \
    --exclude 'logs' \
    --exclude '.env' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'debug_map_stop_*.html' \
    --exclude '.DS_Store' \
    --exclude '.vscode' \
    --exclude '.claude' \
    --exclude '.pytest_cache' \
    -e ssh \
    ./ "${SSH_USER}@${SERVER}:${APP_DIR}/"

echo ">>> Ownership korrigieren (rsync laeuft als root, App-Files brauchen ${APP_USER})"
ssh "${SSH_USER}@${SERVER}" "chown -R ${APP_USER}:${APP_USER} ${APP_DIR}"

echo ">>> Python-Dependencies aktualisieren"
ssh "${SSH_USER}@${SERVER}" bash <<EOF
set -e
cd ${APP_DIR}
if [[ ! -d .venv ]]; then
    echo "    Virtualenv anlegen..."
    sudo -u ${APP_USER} python3 -m venv .venv
fi
sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/pip install --quiet --upgrade pip
sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/pip install --quiet -r requirements.txt
EOF

echo ">>> systemd-Service installieren/aktualisieren"
ssh "${SSH_USER}@${SERVER}" bash <<EOF
set -e
cp ${APP_DIR}/deploy/routezero.service /etc/systemd/system/routezero.service
systemctl daemon-reload
systemctl enable routezero
EOF

echo ">>> Caddyfile installieren/aktualisieren"
ssh "${SSH_USER}@${SERVER}" bash <<EOF
set -e
cp ${APP_DIR}/deploy/Caddyfile /etc/caddy/Caddyfile
mkdir -p /var/log/caddy
chown -R caddy:caddy /var/log/caddy
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
EOF

echo ">>> Service neu starten"
ssh "${SSH_USER}@${SERVER}" "systemctl restart routezero"
sleep 2
ssh "${SSH_USER}@${SERVER}" "systemctl is-active routezero" \
    || (echo "FEHLER: Service nicht aktiv. Logs:"; \
        ssh "${SSH_USER}@${SERVER}" "journalctl -u routezero -n 30 --no-pager"; \
        exit 1)

echo ""
echo "=== Deploy fertig ==="
echo ""
echo "Sanity-Check:"
echo "  curl -s https://${SERVER}/cache/stats | jq"
echo ""
echo "Live-Logs:"
echo "  ssh ${SSH_USER}@${SERVER} 'journalctl -u routezero -f'"
echo ""
