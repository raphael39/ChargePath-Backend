# routezero CLI — alle wichtigen Server-Commands an einer Stelle
#
# Installation (einmalig):
#   echo 'source ~/Desktop/3_Career/Projects/RouteZero/deploy/rz.zsh' >> ~/.zshrc
#   source ~/.zshrc
#
# Danach Commands wie:
#   rz deploy          # Code synchronisieren + Service neustarten
#   rz logs            # Live-Server-Logs verfolgen
#   rz health          # Schnell-Check ob alles laeuft
#   rz                 # Hilfe-Uebersicht

# ===== Konfiguration (per Environment ueberschreibbar) =====
export RZ_LOCAL_DIR="${RZ_LOCAL_DIR:-$HOME/Desktop/3_Career/Projects/RouteZero}"
export RZ_SERVER="${RZ_SERVER:-route.chargeindex.eu}"
export RZ_SSH_USER="${RZ_SSH_USER:-root}"
export RZ_APP_DIR="${RZ_APP_DIR:-/opt/routezero}"
export RZ_API_BASE="${RZ_API_BASE:-https://${RZ_SERVER}}"

# Helper: ins Projekt-Verzeichnis wechseln (immer absolut, nicht relativ zu cwd)
_rz_cd() {
  if [[ ! -d "$RZ_LOCAL_DIR" ]]; then
    echo "❌ RouteZero-Verzeichnis nicht gefunden: $RZ_LOCAL_DIR" >&2
    echo "   Setze RZ_LOCAL_DIR in der Shell falls Pfad anders ist." >&2
    return 1
  fi
  cd "$RZ_LOCAL_DIR"
}

# Hauptfunktion
rz() {
  local cmd="${1:-help}"
  shift 2>/dev/null

  case "$cmd" in
    deploy|d)
      _rz_cd || return 1

      # === Git-Schritt: Auto-Commit + Push vor Deploy ===
      if [[ -d .git ]]; then
        local msg
        if [[ -n "$1" ]]; then
          msg="$*"
        else
          msg="Deploy $(date '+%Y-%m-%d %H:%M')"
        fi

        git add -A
        if git diff --cached --quiet; then
          echo "✓ Keine ungestagten Aenderungen — Skip commit"
        else
          echo "📝 Commit: \"$msg\""
          git commit -m "$msg" || { echo "❌ Commit fehlgeschlagen" >&2; return 1; }
        fi

        # Push pruefen — nur wenn Remote konfiguriert
        if git remote get-url origin >/dev/null 2>&1; then
          # Check ob lokale Commits vor Remote stehen
          local unpushed
          unpushed=$(git log @{u}.. --oneline 2>/dev/null | wc -l | tr -d ' ')
          if [[ "$unpushed" -gt 0 ]]; then
            echo "⬆️  Pushing $unpushed Commit(s) zum Remote..."
            if ! git push; then
              echo "⚠️  Push fehlgeschlagen. Deploy trotzdem fortsetzen? [y/N]"
              read -r answer
              [[ "$answer" != "y" && "$answer" != "Y" ]] && return 1
            fi
          else
            echo "✓ Remote ist aktuell"
          fi
        else
          echo "ℹ️  Kein Git-Remote konfiguriert, Skip push"
        fi
      fi

      # === Eigentlicher Deploy ===
      echo "🚀 Code-Sync + Service-Restart auf $RZ_SERVER"
      ./deploy/deploy.sh
      ;;

    env-push|env)
      _rz_cd || return 1
      if [[ ! -f .env ]]; then
        echo "❌ Keine .env im lokalen Projekt gefunden." >&2
        return 1
      fi
      echo "🔑 .env nach $RZ_SERVER kopieren..."
      scp .env "$RZ_SSH_USER@$RZ_SERVER:$RZ_APP_DIR/.env" \
        && ssh "$RZ_SSH_USER@$RZ_SERVER" \
            "chown routezero:routezero $RZ_APP_DIR/.env && chmod 600 $RZ_APP_DIR/.env && systemctl restart routezero" \
        && echo "✅ .env aktualisiert, Service neugestartet"
      ;;

    logs|log)
      echo "📜 Live-Logs (Ctrl+C zum Beenden)..."
      ssh "$RZ_SSH_USER@$RZ_SERVER" "journalctl -u routezero -f --output=cat"
      ;;

    logs-tail|tail)
      local n="${1:-50}"
      ssh "$RZ_SSH_USER@$RZ_SERVER" "journalctl -u routezero -n $n --no-pager --output=cat"
      ;;

    ssh|connect|shell)
      echo "🔗 SSH zu $RZ_SSH_USER@$RZ_SERVER..."
      ssh "$RZ_SSH_USER@$RZ_SERVER"
      ;;

    status|st)
      ssh "$RZ_SSH_USER@$RZ_SERVER" "systemctl status routezero --no-pager"
      ;;

    restart|rs)
      echo "🔄 Service-Neustart..."
      ssh "$RZ_SSH_USER@$RZ_SERVER" \
        "systemctl restart routezero && sleep 2 && systemctl is-active routezero"
      ;;

    stop)
      ssh "$RZ_SSH_USER@$RZ_SERVER" "systemctl stop routezero && systemctl is-active routezero || echo stopped"
      ;;

    start)
      ssh "$RZ_SSH_USER@$RZ_SERVER" "systemctl start routezero && sleep 2 && systemctl is-active routezero"
      ;;

    health|check)
      echo "🩺 Cache-Stats:"
      curl -s "$RZ_API_BASE/cache/stats" | jq -C . 2>/dev/null || curl -s "$RZ_API_BASE/cache/stats"
      echo ""
      echo "🚗 Vehicles count:"
      curl -s "$RZ_API_BASE/vehicles" | jq '.count' 2>/dev/null
      echo ""
      echo "⏱️  /plan-route Smoke-Test (Berlin → München):"
      time curl -sX POST "$RZ_API_BASE/plan-route" \
        -H 'Content-Type: application/json' \
        -d '{"start_lat":52.5200,"start_lon":13.4050,"dest_lat":48.1351,"dest_lon":11.5820,"vehicle_id":"tesla_model_3_lr_2024_highland","initial_soc":0.8,"price_time_weight":3.0}' \
        | jq '.summary | {vehicle, total_distance_km, drive_time_min, charge_time_min, num_stops, trip_energy_kwh}' 2>/dev/null
      ;;

    cache-stats|cs)
      curl -s "$RZ_API_BASE/cache/stats" | jq -C . 2>/dev/null
      ;;

    cache-clear|cc)
      curl -sX POST "$RZ_API_BASE/cache/clear" | jq -C . 2>/dev/null
      ;;

    plan)
      # Beispiel: rz plan 52.52 13.40 48.13 11.58 tesla_model_3_lr_2024_highland 0.8
      if [[ $# -lt 4 ]]; then
        echo "Usage: rz plan <start_lat> <start_lon> <dest_lat> <dest_lon> [vehicle_id] [initial_soc]"
        return 1
      fi
      local vehicle="${5:-tesla_model_3_lr_2024_highland}"
      local soc="${6:-0.8}"
      curl -sX POST "$RZ_API_BASE/plan-route" \
        -H 'Content-Type: application/json' \
        -d "{\"start_lat\":$1,\"start_lon\":$2,\"dest_lat\":$3,\"dest_lon\":$4,\"vehicle_id\":\"$vehicle\",\"initial_soc\":$soc,\"price_time_weight\":3.0}" \
        | jq -C .
      ;;

    cd|goto)
      _rz_cd
      ;;

    commit|c)
      _rz_cd || return 1
      local msg="${*:-WIP $(date '+%Y-%m-%d %H:%M')}"
      git add -A
      if git diff --cached --quiet; then
        echo "✓ Nichts zu committen"
      else
        git commit -m "$msg" && echo "✅ Commit: \"$msg\""
      fi
      ;;

    push|p)
      _rz_cd || return 1
      git push
      ;;

    gs|git-status)
      _rz_cd || return 1
      git status -sb
      ;;

    log-git|gl)
      _rz_cd || return 1
      git log --oneline -n "${1:-10}"
      ;;

    open|browser)
      open "$RZ_API_BASE/vehicles"
      ;;

    help|--help|-h|"")
      cat <<EOF

🔧 routezero CLI — Server: $RZ_SSH_USER@$RZ_SERVER

  Deploy (mit Auto-Git)
    rz deploy [msg]        git add -A + commit + push + Code-Sync + Restart (alias: d)
    rz env-push            .env hochladen + Service-Restart (alias: env)
    rz restart             Service neustarten (alias: rs)
    rz stop / start        Service stoppen / starten

  Git (manuell)
    rz commit [msg]        git add -A + commit (alias: c)
    rz push                git push (alias: p)
    rz gs                  git status -sb
    rz gl [N]              git log letzte N (Default 10)

  Beobachten
    rz logs                Live-Tail vom Server (Ctrl+C zum Beenden)
    rz logs-tail [N]       Letzte N Zeilen (Default 50, alias: tail)
    rz status              Systemd-Status (alias: st)
    rz health              Cache + Vehicles + Smoke-Test in einem Rutsch

  Cache
    rz cache-stats         Aktuelle Cache-Belegung (alias: cs)
    rz cache-clear         HTTP-Cache leeren (alias: cc)

  Direkt-Zugriff
    rz ssh                 SSH-Verbindung oeffnen (alias: connect, shell)
    rz cd                  Ins lokale Projekt-Verzeichnis (alias: goto)
    rz open                /vehicles im Browser oeffnen
    rz plan <lat1> <lon1> <lat2> <lon2> [vehicle] [soc]
                           Schnell-Routenplanung von der Konsole

  Config via Env-Vars: RZ_SERVER, RZ_SSH_USER, RZ_APP_DIR, RZ_LOCAL_DIR

EOF
      ;;

    *)
      echo "❓ Unbekanntes Kommando: '$cmd' — siehe 'rz help'"
      return 1
      ;;
  esac
}

# Zsh-Completion fuer Sub-Commands
if [[ -n "$ZSH_VERSION" ]]; then
  _rz_completion() {
    local -a commands
    commands=(
      'deploy:git commit + push + Code-Sync + Service-Restart'
      'env-push:.env hochladen'
      'commit:git add -A + commit (optional Message)'
      'push:git push'
      'gs:git status'
      'gl:git log (letzte N)'
      'logs:Live-Logs am Server'
      'logs-tail:Letzte N Log-Zeilen'
      'ssh:SSH-Verbindung'
      'status:Service-Status'
      'restart:Service neustarten'
      'stop:Service stoppen'
      'start:Service starten'
      'health:API + Smoke-Test'
      'cache-stats:Cache-Status'
      'cache-clear:Cache leeren'
      'plan:Schnell-Routenplanung'
      'cd:Ins Projekt-Verzeichnis'
      'open:/vehicles im Browser'
      'help:Hilfe-Uebersicht'
    )
    _describe 'rz command' commands
  }
  compdef _rz_completion rz 2>/dev/null
fi
