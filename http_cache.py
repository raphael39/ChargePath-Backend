"""HTTP-Response-Cache fuer alle externen API-Aufrufe.

Nutzt ``requests-cache``, um die ``requests``-Bibliothek global zu patchen.
Identische Requests (gleicher Endpoint, gleiche Query-Params, gleicher Body)
werden 7 Tage lang aus einem SQLite-Cache bedient — d.h. der zweite identische
``/plan-route``-Call kommt instant aus dem Cache, ohne Valhalla- oder
ChargeIndex-Roundtrip.

Gecacht wird:
- Valhalla (Routen, Elevation) — Karten-Daten aendern sich kaum binnen Tagen
- ChargeIndex (Stationen + Preise entlang der Route) — Preis-Snapshots sind
  per Doku stuendlich aktualisiert; 7 Tage TTL bedeutet schlimmstenfalls
  veraltete Preise um diese Spanne. Wenn das Problem ist: TTL via
  ``HTTP_CACHE_TTL_DAYS`` in der .env runtersetzen.

Cache-Datei: ``.cache/route_zero_http.sqlite`` (gitignored). Loeschen
erzwingt frische Daten beim naechsten Call.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from app_config import get_env_int, get_env_str


CACHE_DIR_DEFAULT = ".cache"
CACHE_FILENAME = "route_zero_http"


def install_http_cache() -> bool:
    """Aktiviert den globalen requests-Cache. Returns True bei Erfolg.

    Faellt still zurueck, wenn ``requests-cache`` nicht installiert ist —
    der Server laeuft dann unverpatcht weiter (jeder Call geht live).
    """
    try:
        from requests_cache import install_cache
    except ImportError:
        print("[http_cache] requests-cache nicht installiert — Caching DEAKTIVIERT.")
        print("[http_cache] Installation: pip install requests-cache")
        return False

    ttl_days = get_env_int("HTTP_CACHE_TTL_DAYS", 7)
    cache_dir = get_env_str("HTTP_CACHE_DIR", CACHE_DIR_DEFAULT)

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    cache_path = os.path.join(cache_dir, CACHE_FILENAME)

    install_cache(
        cache_name=cache_path,
        backend="sqlite",
        expire_after=timedelta(days=ttl_days),
        # POSTs muessen explizit erlaubt werden — sowohl Valhalla als auch
        # der ChargeIndex along-route-Endpoint nutzen POST.
        allowable_methods=("GET", "POST"),
        # Nur Erfolgs-Responses cachen. Fehler/Timeouts werden NICHT
        # persistiert, sonst wuerde ein einmaliger 500er 7 Tage lang
        # gespiegelt.
        allowable_codes=(200,),
        # Identische Bodies sind der wichtigste Cache-Key-Bestandteil;
        # Headers koennen pro Call leicht variieren (User-Agent, etc.).
        match_headers=False,
        # POST-Body fliesst standardmaessig nicht in den Cache-Key ein.
        # Wir aktivieren das, damit identische Routen-Bodies einen Hit erzeugen.
        ignored_parameters=[],
    )

    print(
        f"[http_cache] AKTIVIERT — SQLite unter '{cache_path}.sqlite', "
        f"TTL {ttl_days} Tage, Methoden GET+POST."
    )
    return True


def get_cache_stats() -> dict:
    """Liefert ein paar Cache-Metriken fuer einen optionalen ``/cache/stats``-Endpoint."""
    try:
        from requests_cache import get_cache
    except ImportError:
        return {"installed": False}

    cache = get_cache()
    if cache is None:
        return {"installed": False}

    try:
        # ``response_count`` ist die effizienteste Variante; Fallback ueber Iteration
        try:
            total = cache.response_count()
        except Exception:
            total = sum(1 for _ in cache.responses)
        return {
            "installed": True,
            "backend": type(cache).__name__,
            "cached_responses": total,
        }
    except Exception as exc:
        return {"installed": True, "error": str(exc)}


def clear_http_cache() -> None:
    """Komplett-Reset des Caches (z.B. fuer einen ``/cache/clear``-Endpoint)."""
    try:
        from requests_cache import clear
    except ImportError:
        return
    clear()
