#!/usr/bin/env bash
#
# Bootstrap-Skript fuer einen frischen Ubuntu-24.04-Server auf Hetzner.
# Idempotent — kann mehrfach ausgefuehrt werden ohne Schaden.
#
# Voraussetzungen:
#   - Ubuntu 24.04 LTS
#   - root-Zugang (oder sudo)
#   - DNS-A-Record route.chargeindex.eu zeigt auf die Server-IP
#
# Ausfuehren:
#   ssh root@94.130.184.127
#   curl -fsSL https://example.com/bootstrap.sh | bash
#   # ODER nach rsync:
#   bash /tmp/bootstrap.sh

set -euo pipefail

APP_USER="routezero"
APP_DIR="/opt/routezero"
DOMAIN="route.chargeindex.eu"

echo ">>> [1/8] System update & basics installieren"
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git rsync curl ufw fail2ban \
    debian-keyring debian-archive-keyring apt-transport-https

echo ">>> [2/8] Caddy installieren (offizielles APT-Repo)"
if ! command -v caddy &>/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
else
    echo "    Caddy schon installiert: $(caddy version | head -1)"
fi

echo ">>> [3/8] System-User '${APP_USER}' anlegen"
if ! id -u "${APP_USER}" &>/dev/null; then
    useradd --system --create-home --home-dir "${APP_DIR}" \
            --shell /usr/sbin/nologin "${APP_USER}"
else
    echo "    User existiert schon"
fi

echo ">>> [4/8] App-Verzeichnis vorbereiten"
mkdir -p "${APP_DIR}/.cache" "${APP_DIR}/logs"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo ">>> [5/8] Firewall konfigurieren (UFW)"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP (Caddy Cert-Renewal)'
ufw allow 443/tcp comment 'HTTPS (Caddy)'
ufw --force enable

echo ">>> [6/8] fail2ban fuer SSH aktivieren"
systemctl enable --now fail2ban

echo ">>> [7/8] systemd-Service installieren"
if [[ -f "${APP_DIR}/deploy/routezero.service" ]]; then
    cp "${APP_DIR}/deploy/routezero.service" /etc/systemd/system/routezero.service
    systemctl daemon-reload
    systemctl enable routezero
    echo "    Service installiert (noch nicht gestartet — erst nach Code-Setup)"
else
    echo "    WARN: ${APP_DIR}/deploy/routezero.service noch nicht da."
    echo "          Code zuerst via deploy.sh hochladen, dann erneut ausfuehren."
fi

echo ">>> [8/8] Caddyfile installieren"
if [[ -f "${APP_DIR}/deploy/Caddyfile" ]]; then
    cp "${APP_DIR}/deploy/Caddyfile" /etc/caddy/Caddyfile
    mkdir -p /var/log/caddy
    chown -R caddy:caddy /var/log/caddy
    systemctl reload caddy 2>/dev/null || systemctl restart caddy
    echo "    Caddyfile aktiv. Caddy holt sich beim ersten HTTPS-Request automatisch ein Zertifikat."
else
    echo "    WARN: ${APP_DIR}/deploy/Caddyfile noch nicht da."
fi

echo ""
echo "=== Bootstrap abgeschlossen ==="
echo ""
echo "Naechste Schritte:"
echo "  1. Code hochladen:  ./deploy/deploy.sh (auf deinem Mac)"
echo "  2. .env einspielen: scp .env routezero@${DOMAIN}:${APP_DIR}/.env"
echo "  3. Service starten: ssh root@${DOMAIN} 'systemctl start routezero'"
echo "  4. Logs pruefen:    ssh root@${DOMAIN} 'journalctl -u routezero -f'"
echo "  5. Test:            curl https://${DOMAIN}/cache/stats"
echo ""
