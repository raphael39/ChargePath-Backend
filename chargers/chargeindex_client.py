"""Client fuer die ChargeIndex API (chargeindex.eu / Hetzner).

Ersetzt die frueher genutzte OpenChargeMap-Bbox-Suche. Wir nutzen den
``POST /v1/chargers/along-route``-Endpoint, der Ladestationen innerhalb
eines Korridors entlang einer GeoJSON-LineString zurueckliefert. Die
Antwort ist bereits nach Fahrt-Reihenfolge sortiert (``along_route_m``)
und enthaelt Preise direkt aus dem National Access Point.

Das Output-Format wird auf die Keys gemappt, die der bestehende
Downstream-Code (``charger_ranking.get_best_charger`` und die Debug-
Karten in ``route_planner``) liest.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Sequence, Tuple

import requests

from app_config import get_env_float, get_env_int, get_env_str


# --- Endpoint-Konfiguration aus der .env -------------------------------------

CHARGEINDEX_BASE_URL = get_env_str("CHARGEINDEX_BASE_URL", "http://178.104.37.14:8080")
# Default 30s — bei langen Routen muss der Server eine LineString mit
# tausenden Punkten gegen jeden Charger in der Region projizieren. 15s waren
# fuer kurze Test-Routen ok, skalieren aber nicht.
CHARGEINDEX_TIMEOUT_SECONDS = get_env_int("CHARGEINDEX_TIMEOUT_SECONDS", 30)
CHARGEINDEX_CORRIDOR_M_DEFAULT = get_env_float("CHARGEINDEX_CORRIDOR_M", 4000.0)
CHARGEINDEX_MIN_KW_DEFAULT = get_env_float("CHARGEINDEX_MIN_KW", 150.0)
CHARGEINDEX_LIMIT_DEFAULT = get_env_int("CHARGEINDEX_LIMIT", 100)
CHARGEINDEX_RETRIES = get_env_int("CHARGEINDEX_RETRIES", 2)  # einmal Backoff-Retry plus Originalversuch

# API-Limits gemaess Doku
_API_MAX_VERTICES = 10_000
_API_CORRIDOR_RANGE = (100.0, 20_000.0)

# Praxis-Limit: oberhalb dieser Vertex-Zahl wird die Polyline server-seitig
# zwar akzeptiert, fuehrt aber unter Last zu Timeouts (O(N x M) Geometrie
# pro Charger). 1500 Punkte = bei typischen Lade-Fenstern ~50m Aufloesung —
# weit dichter als noetig fuer einen 4km-Korridor.
_PRACTICAL_MAX_VERTICES = 1_500


# --- Startup-Log: zeigt sofort beim Modul-Load die effektiven Config-Werte.
# Praktisch um zu sehen, ob ein .env-Update wirklich angekommen ist
# (siehe: ``app_config.load_dotenv`` nutzt setdefault, also setzt es alte
# Env-Werte beim Hot-Reload NICHT neu — nur ein voller uvicorn-Restart hilft).
print(
    f"[chargeindex] Config geladen: timeout={CHARGEINDEX_TIMEOUT_SECONDS}s, "
    f"retries={CHARGEINDEX_RETRIES}, corridor={CHARGEINDEX_CORRIDOR_M_DEFAULT:.0f}m, "
    f"min_kw={CHARGEINDEX_MIN_KW_DEFAULT:.0f}, limit={CHARGEINDEX_LIMIT_DEFAULT}, "
    f"max_vertices={_PRACTICAL_MAX_VERTICES}, base_url={CHARGEINDEX_BASE_URL}"
)


class ChargeIndexError(RuntimeError):
    """Wird ausgeloest, wenn der ChargeIndex-Endpoint nicht antwortet
    oder ein nicht-200-Status zurueckgibt."""


def _swap_to_lon_lat(coords_lat_lon: Sequence[Sequence[float]]) -> List[List[float]]:
    """``[lat, lon]`` -> ``[lon, lat]`` fuer GeoJSON.

    Valhalla liefert das Polyline-Decode als ``[lat, lon]``; GeoJSON
    erwartet ``[lon, lat]``. Hier wird genau dieser Swap erledigt.
    """
    swapped: List[List[float]] = []
    for pair in coords_lat_lon:
        if len(pair) < 2:
            continue
        lat = float(pair[0])
        lon = float(pair[1])
        swapped.append([lon, lat])
    return swapped


def _decimate(coords: List[List[float]], max_vertices: int) -> List[List[float]]:
    """Stellt sicher, dass der Polylinie-POST das API-Limit einhaelt.

    Bei extrem dichten Window-Slices (sehr seltener Fall) wird gleichmaessig
    ausgeduennt; Anfangs- und Endpunkt bleiben immer erhalten.
    """
    n = len(coords)
    if n <= max_vertices:
        return coords
    step = n / float(max_vertices)
    keep: List[List[float]] = [coords[0]]
    i = 0.0
    while True:
        idx = int(i + step)
        if idx >= n - 1:
            break
        keep.append(coords[idx])
        i += step
    keep.append(coords[-1])
    return keep


def _map_station(station: Dict[str, Any]) -> Dict[str, Any]:
    """Mappt eine ChargeIndex-Station auf das interne Charger-Format.

    Pflicht-Keys (von ``charger_ranking`` gelesen): ``latitude``,
    ``longitude``, ``operator_name``, ``max_power_kw``, ``price_per_kwh``.
    Zusaetzlich werden weitere Felder durchgereicht, damit die Debug-
    Karte und spaetere Auswertungen darauf zugreifen koennen.
    """
    operator_name = station.get("operator_name") or "Unbekannt"
    return {
        # Pflicht-Felder fuer Downstream-Code
        "latitude": station.get("latitude"),
        "longitude": station.get("longitude"),
        "operator_name": operator_name,
        # Backward-compat: alter OCM-Code las teilweise "operator"
        "operator": operator_name,
        "max_power_kw": float(station.get("max_kw") or 0.0),
        "price_per_kwh": station.get("latest_per_kwh_eur"),
        # Zusaetzliche Felder fuer Tracing/UI
        "id": station.get("source_id"),
        "source_id": station.get("source_id"),
        "name": station.get("name"),
        "address": station.get("address"),
        "city": station.get("city"),
        "post_code": station.get("post_code"),
        "country": station.get("country"),
        "evse_count": station.get("evse_count"),
        "distance_m": station.get("distance_m"),
        "along_route_m": station.get("along_route_m"),
        "plugs": station.get("plugs", []),
    }


def _post_along_route_with_retry(
    coords_lon_lat: List[List[float]],
    corridor_value: float,
    min_kw_value: float,
    limit_value: int,
) -> Dict[str, Any]:
    """POSTet den along-route Request mit Retry-Backoff.

    Bei Timeout oder 5xx wird bis zu ``CHARGEINDEX_RETRIES`` Mal wiederholt,
    mit exponentialem Backoff (1s, 2s, 4s, ...). Bei 4xx-Fehlern (z.B.
    invalid input) gibt es keinen Retry — das wuerde immer wieder schief
    gehen.
    """
    url = f"{CHARGEINDEX_BASE_URL.rstrip('/')}/v1/chargers/along-route"
    params = {"corridor_m": corridor_value, "min_kw": min_kw_value, "limit": limit_value}
    body = {"route": {"type": "LineString", "coordinates": coords_lon_lat}}

    last_exc: Exception | None = None
    last_status: int | None = None
    last_err_msg: str = ""

    for attempt in range(max(1, CHARGEINDEX_RETRIES + 1)):
        try:
            resp = requests.post(
                url, params=params, json=body,
                timeout=CHARGEINDEX_TIMEOUT_SECONDS,
                headers={"Content-Type": "application/json"},
            )
        except requests.Timeout as exc:
            last_exc = exc
            last_err_msg = f"Timeout nach {CHARGEINDEX_TIMEOUT_SECONDS}s"
        except requests.RequestException as exc:
            last_exc = exc
            last_err_msg = str(exc)
        else:
            if resp.status_code == 200:
                return resp.json()
            last_status = resp.status_code
            try:
                last_err_msg = resp.json().get("error", resp.text)
            except ValueError:
                last_err_msg = resp.text
            # 4xx -> kein Retry, sofortiges Aufgeben
            if 400 <= resp.status_code < 500:
                break

        # Backoff bevor naechster Versuch
        if attempt < CHARGEINDEX_RETRIES:
            sleep_s = 1.0 * (2 ** attempt)
            print(f"[chargeindex] Versuch {attempt + 1} fehlgeschlagen ({last_err_msg}), "
                  f"retry in {sleep_s:.0f}s...")
            time.sleep(sleep_s)

    # Alle Versuche aufgebraucht
    vertex_count = len(coords_lon_lat)
    diag = f"vertices={vertex_count}, corridor={corridor_value}m, min_kw={min_kw_value}"
    if last_status is not None:
        raise ChargeIndexError(
            f"ChargeIndex along-route HTTP {last_status} nach {CHARGEINDEX_RETRIES + 1} Versuchen: "
            f"{last_err_msg} ({diag})"
        )
    raise ChargeIndexError(
        f"ChargeIndex along-route nicht erreichbar nach {CHARGEINDEX_RETRIES + 1} Versuchen: "
        f"{last_err_msg} ({diag})"
    ) from last_exc


def find_chargers_along_route(
    window_coords_lat_lon: Sequence[Sequence[float]],
    corridor_m: float | None = None,
    min_kw: float | None = None,
    limit: int | None = None,
) -> List[Dict[str, Any]]:
    """Sucht Ladestationen in einem Korridor entlang des Lade-Fensters.

    :param window_coords_lat_lon: Geordnete Liste von ``[lat, lon]``-Punkten,
        die das Lade-Fenster aus ``find_charging_window`` beschreiben.
    :param corridor_m: Halbbreite des Korridors in Metern (Default aus .env).
    :param min_kw: Mindest-Stationsleistung in kW (Default aus .env, 150 = HPC).
    :param limit: Maximal zurueckgegebene Stationen (1-500).
    :returns: Liste von Charger-Dicts im internen Format. Bereits nach
        Fahrt-Reihenfolge sortiert (Server-seitig).
    :raises ChargeIndexError: bei Netzwerk- oder API-Fehlern (nach Retries).
    :raises ValueError: bei zu wenigen Window-Koordinaten (< 2).
    """
    corridor_value = float(corridor_m if corridor_m is not None else CHARGEINDEX_CORRIDOR_M_DEFAULT)
    corridor_value = max(_API_CORRIDOR_RANGE[0], min(_API_CORRIDOR_RANGE[1], corridor_value))

    min_kw_value = float(min_kw if min_kw is not None else CHARGEINDEX_MIN_KW_DEFAULT)
    if min_kw_value < 0:
        min_kw_value = 0.0

    limit_value = int(limit if limit is not None else CHARGEINDEX_LIMIT_DEFAULT)
    limit_value = max(1, min(500, limit_value))

    # Koordinaten umformen + ausduennen.
    # Wir nutzen ``_PRACTICAL_MAX_VERTICES`` (1500) statt des API-Hard-Limits
    # (10000), weil der ChargeIndex-Server O(N x M)-Geometrie macht und bei
    # langen Polylinen + vielen Chargern unter Last Timeouts wirft.
    coords_lon_lat = _swap_to_lon_lat(window_coords_lat_lon)
    if len(coords_lon_lat) < 2:
        raise ValueError("Lade-Fenster benoetigt mindestens 2 Punkte fuer ChargeIndex along-route.")

    raw_vertex_count = len(coords_lon_lat)
    coords_lon_lat = _decimate(coords_lon_lat, _PRACTICAL_MAX_VERTICES)
    if raw_vertex_count > _PRACTICAL_MAX_VERTICES:
        print(f"[chargeindex] Polyline auf {len(coords_lon_lat)} Punkte ausgeduennt "
              f"(war {raw_vertex_count}, Schwelle {_PRACTICAL_MAX_VERTICES})")

    payload = _post_along_route_with_retry(coords_lon_lat, corridor_value, min_kw_value, limit_value)
    raw_stations = payload.get("stations", []) or []
    return [_map_station(s) for s in raw_stations if isinstance(s, dict)]


def get_route_meta(
    window_coords_lat_lon: Sequence[Sequence[float]],
    corridor_m: float | None = None,
    min_kw: float | None = None,
    limit: int | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Komfort-Variante: liefert zusaetzlich die ``query``- und ``count``-
    Metadaten, die die ChargeIndex-API mitschickt (``vertex_count``,
    ``route_length_m``, ...). Praktisch fuer Logging.
    """
    corridor_value = float(corridor_m if corridor_m is not None else CHARGEINDEX_CORRIDOR_M_DEFAULT)
    corridor_value = max(_API_CORRIDOR_RANGE[0], min(_API_CORRIDOR_RANGE[1], corridor_value))
    min_kw_value = max(0.0, float(min_kw if min_kw is not None else CHARGEINDEX_MIN_KW_DEFAULT))
    limit_value = max(1, min(500, int(limit if limit is not None else CHARGEINDEX_LIMIT_DEFAULT)))

    coords_lon_lat = _swap_to_lon_lat(window_coords_lat_lon)
    if len(coords_lon_lat) < 2:
        raise ValueError("Lade-Fenster benoetigt mindestens 2 Punkte fuer ChargeIndex along-route.")
    coords_lon_lat = _decimate(coords_lon_lat, _PRACTICAL_MAX_VERTICES)

    payload = _post_along_route_with_retry(coords_lon_lat, corridor_value, min_kw_value, limit_value)
    stations = [_map_station(s) for s in (payload.get("stations") or []) if isinstance(s, dict)]
    meta = {
        "query": payload.get("query", {}),
        "count": payload.get("count", len(stations)),
        "generated_at": payload.get("generated_at"),
    }
    return stations, meta


# --- Dedup --------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Echte Distanz zwischen zwei GPS-Punkten in Metern (haversine)."""
    R = 6371000.0
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _are_same_station(
    a: Dict[str, Any],
    b: Dict[str, Any],
    distance_threshold_m: float,
    kw_tolerance: float = 1.0,
    price_tolerance_eur: float = 0.01,
) -> bool:
    """Bewertet, ob zwei ChargeIndex-Charger praktisch identisch sind."""
    if str(a.get("operator_name") or "") != str(b.get("operator_name") or ""):
        return False

    kw_a = float(a.get("max_power_kw") or 0.0)
    kw_b = float(b.get("max_power_kw") or 0.0)
    if abs(kw_a - kw_b) > kw_tolerance:
        return False

    p_a = a.get("price_per_kwh")
    p_b = b.get("price_per_kwh")
    if p_a is None and p_b is None:
        pass  # beide unbekannt -> match (gleicher Fallback wirkt)
    elif p_a is None or p_b is None:
        return False
    elif abs(float(p_a) - float(p_b)) > price_tolerance_eur:
        return False

    d = _haversine_m(
        float(a.get("latitude") or 0.0), float(a.get("longitude") or 0.0),
        float(b.get("latitude") or 0.0), float(b.get("longitude") or 0.0),
    )
    return d <= distance_threshold_m


def dedup_chargers(
    stations: List[Dict[str, Any]],
    distance_threshold_m: float = 200.0,
) -> List[Dict[str, Any]]:
    """Fasst Charger zusammen, die vermutlich am selben Standort dieselbe
    Lade-Erfahrung bieten — Welsenergy-Problem.

    Zwei Charger gelten als identisch, wenn:
    - Distanz <= ``distance_threshold_m`` (haversine, Default 200m)
    - Identischer ``operator_name``
    - ``max_power_kw`` innerhalb 1 kW
    - ``price_per_kwh`` innerhalb 0.01 EUR oder beide NULL

    Aus jeder Duplikat-Gruppe wird der naechstgelegene Charger (kleinste
    ``distance_m`` zur Route) als Repraesentant gewaehlt. Tiebreaker:
    ``along_route_m`` aufsteigend, dann ``source_id`` lexikografisch.

    Der Repraesentant erhaelt zwei Zusatzfelder:
    - ``merged_count``: Anzahl der zusammengefassten EVSEs (>=1)
    - ``merged_source_ids``: Liste der zusammengefassten Original-IDs
    """
    n = len(stations)
    used = [False] * n
    result: List[Dict[str, Any]] = []

    for i in range(n):
        if used[i]:
            continue
        cluster = [stations[i]]
        used[i] = True
        for j in range(i + 1, n):
            if used[j]:
                continue
            if _are_same_station(stations[i], stations[j], distance_threshold_m):
                cluster.append(stations[j])
                used[j] = True

        # Repraesentant: kleinste distance_m -> along_route_m -> source_id
        rep = min(cluster, key=lambda x: (
            float(x.get("distance_m") if x.get("distance_m") is not None else float("inf")),
            float(x.get("along_route_m") if x.get("along_route_m") is not None else float("inf")),
            str(x.get("source_id") or ""),
        ))
        merged = dict(rep)
        merged["merged_count"] = len(cluster)
        merged["merged_source_ids"] = [c.get("source_id") for c in cluster if c.get("source_id")]
        result.append(merged)

    return result
