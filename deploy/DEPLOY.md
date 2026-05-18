# RouteZero — Deployment auf Hetzner

Schritt-fuer-Schritt-Anleitung fuer das erstmalige Deployment des RouteZero-Backends auf einen Hetzner CX22 mit Ubuntu 24.04, Caddy fuer HTTPS und systemd fuer Service-Management.

## Voraussetzungen

- Hetzner Cloud CX22 (oder groesser) mit Ubuntu 24.04
- SSH-Key auf dem Server hinterlegt
- DNS A-Record `route.chargeindex.eu` zeigt auf die Server-IP (94.130.184.127)
- Lokales `.env` mit allen API-Keys (StadiaMaps, OCM, ChargeIndex etc.)

## Erster Deploy

### 1. Code auf den Server kopieren

Vom Mac aus:

```bash
cd ~/Desktop/3_Career/Projects/RouteZero
chmod +x deploy/bootstrap.sh deploy/deploy.sh
rsync -avz --exclude '.git' --exclude '.venv' --exclude '.cache' --exclude '.env' \
    -e ssh ./ root@route.chargeindex.eu:/opt/routezero/
```

### 2. Bootstrap auf dem Server ausfuehren

```bash
ssh root@route.chargeindex.eu
cd /opt/routezero
bash deploy/bootstrap.sh
```

Das Skript:
- Updated das System
- Installiert Python, Caddy, ufw, fail2ban
- Legt den Linux-User `routezero` an
- Konfiguriert die Firewall (nur 22/80/443 offen)
- Installiert systemd-Service und Caddyfile
- Aktiviert HTTPS automatisch via Let's Encrypt

### 3. .env hochladen

Auf dem Mac:

```bash
scp .env root@route.chargeindex.eu:/opt/routezero/.env
ssh root@route.chargeindex.eu 'chown routezero:routezero /opt/routezero/.env && chmod 600 /opt/routezero/.env'
```

WICHTIG: `.env` enthaelt API-Keys und sollte NIEMALS in Git landen. Mode 600 bedeutet: nur der Owner kann lesen.

### 4. Python-Virtualenv und Deps installieren

Auf dem Server (als root):

```bash
cd /opt/routezero
sudo -u routezero python3 -m venv .venv
sudo -u routezero .venv/bin/pip install --upgrade pip
sudo -u routezero .venv/bin/pip install -r requirements.txt
```

### 5. Service starten

```bash
systemctl start routezero
systemctl status routezero
```

### 6. Verifikation

Vom Mac aus:

```bash
# HTTPS-Endpoint testen
curl -s https://route.chargeindex.eu/cache/stats

# Vehicles-Endpoint
curl -s https://route.chargeindex.eu/vehicles | jq '.count'

# Test-Route (Berlin -> Salzburg)
curl -X POST https://route.chargeindex.eu/plan-route \
    -H 'Content-Type: application/json' \
    -d '{
        "start_lat": 51.5142273, "start_lon": 7.4652789,
        "dest_lat": 47.7981346, "dest_lon": 13.0464806,
        "vehicle_id": "tesla_model_3_lr",
        "initial_soc": 0.48,
        "price_time_weight": 3.0
    }' | jq '.summary'
```

## Updates / Re-Deploy

Nach Code-Aenderungen einfach:

```bash
./deploy/deploy.sh
```

Macht: rsync hochladen, deps aktualisieren, Service-Restart, Sanity-Check.

## Logs anschauen

```bash
# Live-Tail der App-Logs
ssh root@route.chargeindex.eu 'journalctl -u routezero -f'

# Letzte 100 Zeilen
ssh root@route.chargeindex.eu 'journalctl -u routezero -n 100 --no-pager'

# Caddy-Logs (HTTPS-Zugriff, Cert-Renewal)
ssh root@route.chargeindex.eu 'journalctl -u caddy -f'

# Strukturierte JSON-Logs der Requests
ssh root@route.chargeindex.eu 'tail -f /var/log/caddy/routezero.log'
```

## Cache-Verwaltung

```bash
# Cache-Status pruefen
curl https://route.chargeindex.eu/cache/stats

# Cache leeren (z.B. wenn neue Preise von ChargeIndex erwartet werden)
curl -X POST https://route.chargeindex.eu/cache/clear
```

## Server-Pflege

### Auto-Updates aktivieren (empfohlen)

```bash
ssh root@route.chargeindex.eu
apt install unattended-upgrades
dpkg-reconfigure unattended-upgrades  # Y waehlen
```

### Backups

Hetzner-Backups in der Cloud-Console aktivieren (20 % vom Server-Preis, ~0.90 EUR/Monat). Macht taegliche Snapshots, sehr empfohlen.

### Monitoring (optional, fuer spaeter)

Health-Check-Endpoint koennte man hinzufuegen — derzeit reicht `/cache/stats` als Lebenszeichen. Externer Uptime-Check via Hetzner Status Cloud oder UptimeRobot (kostenlos).

## Rollback bei Problemen

```bash
# Service stoppen
ssh root@route.chargeindex.eu 'systemctl stop routezero'

# Auf Mac: vorherigen Commit auschecken, dann neu deployen
cd ~/Desktop/3_Career/Projects/RouteZero
git checkout HEAD~1   # oder spezifischer Commit-Hash
./deploy/deploy.sh
```

## Troubleshooting

### "502 Bad Gateway" bei HTTPS-Request

Backend nicht gestartet oder gecrasht.

```bash
ssh root@route.chargeindex.eu 'systemctl status routezero'
ssh root@route.chargeindex.eu 'journalctl -u routezero -n 50'
```

### Caddy bekommt kein Let's-Encrypt-Zertifikat

- DNS pruefen: `dig route.chargeindex.eu +short` muss Server-IP zeigen
- Port 80 muss offen sein (UFW): `ufw status`
- Caddy-Logs: `journalctl -u caddy -n 50`

### Cache-Probleme nach Deploy

```bash
ssh root@route.chargeindex.eu
curl -X POST http://127.0.0.1:8000/cache/clear
```

### High-Load oder OOM

`MemoryMax=2G` in der systemd-Unit limitiert RouteZero auf 2 GB. Bei OOM:

```bash
ssh root@route.chargeindex.eu 'journalctl -u routezero -k -n 100'  # zeigt OOM-Kills
```

CX22 hat 4 GB total, Caddy + System brauchen ~500 MB, also bleiben ~3.5 GB fuer RouteZero. Falls 2 GB Limit eng wird: `MemoryMax=3G` setzen.
