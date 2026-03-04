"""Intelligente Ladesaeulen-Suche im Lade-Fenster (15% bis 5% SoC)."""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import requests

from app_config import get_env_float, get_env_int, get_env_str
from ev_physics import EVPhysicsModel
from valhalla_route import ValhallaRouteError, get_valhalla_route

OCM_API_URL = get_env_str("OCM_API_URL")
OCM_API_KEY = get_env_str("OCM_API_KEY")
OCM_USER_AGENT = get_env_str("OCM_USER_AGENT", "RouteZero/1.0")
OCM_TIMEOUT_SECONDS = get_env_int("OCM_TIMEOUT_SECONDS", 20)
OCM_CONNECTION_TYPE_ID = get_env_int("OCM_CONNECTION_TYPE_ID", 33)
OCM_MIN_POWER_KW = get_env_int("OCM_MIN_POWER_KW", 100)
CHARGER_PREFILTER_RADIUS_KM = get_env_float("CHARGER_PREFILTER_RADIUS_KM", 5.0)
CHARGER_PREFILTER_SAMPLE_STEP = get_env_int("CHARGER_PREFILTER_SAMPLE_STEP", 10)

VALHALLA_ROUTE_URL = get_env_str("VALHALLA_ROUTE_URL")
VALHALLA_TIMEOUT_SECONDS = get_env_int("VALHALLA_TIMEOUT_SECONDS", 15)
VALHALLA_ELEVATION_INTERVAL_M = get_env_int("VALHALLA_ELEVATION_INTERVAL_M", 30)

BATTERY_CAPACITY_KWH = get_env_float("BATTERY_CAPACITY_KWH", 75.0)
SOC_START = get_env_float("SOC_START", 1.0)
SOC_WINDOW_START = get_env_float("SOC_WINDOW_START", 0.15)
SOC_WINDOW_END = get_env_float("SOC_WINDOW_END", 0.05)


def get_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Berechnet den initialen Kompasswinkel (0-359 Grad)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)

    x = math.sin(delta_lon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lon)

    bearing_deg = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    return int(round(bearing_deg)) % 360


def _to_valhalla_location(coord: Sequence[float], heading: int | None = None) -> Dict[str, object]:
    """Konvertiert [lat, lon] nach {'lon': x, 'lat': y, 'heading': h}."""
    if len(coord) < 2:
        raise ValueError("Koordinate muss mindestens [lat, lon] enthalten.")
    location: Dict[str, object] = {"lon": float(coord[1]), "lat": float(coord[0])}
    if heading is not None:
        location["heading"] = int(heading) % 360
    return location


def _build_valhalla_payload(locations: List[Dict[str, object]]) -> Dict[str, object]:
    """Valhalla-Payload analog zu valhalla_route._build_route_payload."""
    return {
        "locations": locations,
        "costing": "auto",
        "costing_options": {"auto": {}},
        "directions_options": {"units": "kilometers", "elevation": True},
        "elevation_interval": VALHALLA_ELEVATION_INTERVAL_M,
        "format": "json",
    }


def _get_trip_time_min(locations: List[Dict[str, object]]) -> float:
    """Führt einen Valhalla-Route-Request aus und gibt Gesamtzeit in Minuten zurück."""
    if not VALHALLA_ROUTE_URL:
        raise ValueError("VALHALLA_ROUTE_URL fehlt. Bitte in .env setzen.")

    payload = _build_valhalla_payload(locations)
    response = requests.post(VALHALLA_ROUTE_URL, json=payload, timeout=VALHALLA_TIMEOUT_SECONDS)
    response.raise_for_status()

    data = response.json()
    trip = data.get("trip")
    if not isinstance(trip, dict):
        raise ValueError("Valhalla-Antwort enthält kein 'trip' Objekt.")

    summary = trip.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("Valhalla-Antwort enthält keine 'summary'.")

    return float(summary["time"]) / 60.0


def get_detour_min(
    charger_lat: float,
    charger_lon: float,
    window_start: Dict[str, object],
    window_end: Dict[str, object],
    baseline_time_min: float,
) -> float:
    """Berechnet den echten Umweg in Minuten über Start -> Charger -> Ende."""
    try:
        locations = [
            dict(window_start),
            {"lat": float(charger_lat), "lon": float(charger_lon)},
            dict(window_end),
        ]
        trip_time_min = _get_trip_time_min(locations)
        return trip_time_min - baseline_time_min
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return 999.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Grobe Luftliniendistanz zwischen zwei Koordinaten."""
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c


def is_within_window_radius(
    charger_lat: float,
    charger_lon: float,
    charging_window_coords: Sequence[Sequence[float]],
    max_distance_km: float = CHARGER_PREFILTER_RADIUS_KM,
    sample_step: int = CHARGER_PREFILTER_SAMPLE_STEP,
) -> bool:
    """Prüft grob, ob eine Säule innerhalb des Radius zur Fenster-Route liegt."""
    if not charging_window_coords:
        return False

    step = max(1, sample_step)
    sampled_coords = list(charging_window_coords[::step])
    if charging_window_coords[-1] not in sampled_coords:
        sampled_coords.append(charging_window_coords[-1])

    for point in sampled_coords:
        if len(point) < 2:
            continue
        if haversine_km(charger_lat, charger_lon, float(point[0]), float(point[1])) <= max_distance_km:
            return True
    return False


def get_bounding_box(coords: List[List[float]], margin: float = 0.02) -> Tuple[float, float, float, float]:
    """Berechnet Bounding-Box inkl. Puffer um die Ladefenster-Koordinaten."""
    if not coords:
        raise ValueError("Koordinatenliste fuer Bounding Box ist leer.")

    valid_coords = [point for point in coords if len(point) >= 2]
    if not valid_coords:
        raise ValueError("Koordinatenliste enthaelt keine gueltigen [lat, lon]-Punkte.")

    min_lat = min(float(point[0]) for point in valid_coords)
    max_lat = max(float(point[0]) for point in valid_coords)
    min_lon = min(float(point[1]) for point in valid_coords)
    max_lon = max(float(point[1]) for point in valid_coords)

    min_lat -= margin
    max_lat += margin
    min_lon -= margin
    max_lon += margin
    return min_lat, max_lat, min_lon, max_lon


def _extract_max_power_kw(connections: object) -> float:
    """Liest die maximale PowerKW aus dem OCM-Connections-Array."""
    if not isinstance(connections, list):
        return 0.0

    max_power = 0.0
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        try:
            power_kw = float(connection.get("PowerKW", 0.0))
        except (TypeError, ValueError):
            power_kw = 0.0
        max_power = max(max_power, power_kw)
    return max_power


def _shape_slice(
    decoded_shape: Sequence[Sequence[float]], begin_index: int, end_index: int
) -> List[List[float]]:
    """Schneidet Koordinaten zwischen begin/end Shape-Index aus."""
    if not decoded_shape:
        return []

    max_idx = len(decoded_shape) - 1
    begin = max(0, min(max_idx, begin_index))
    end = max(0, min(max_idx, end_index))
    if end < begin:
        begin, end = end, begin

    points: List[List[float]] = []
    for point in decoded_shape[begin : end + 1]:
        if len(point) < 2:
            continue
        points.append([float(point[0]), float(point[1])])
    return points


def _append_without_duplicate(target: List[List[float]], source: Sequence[Sequence[float]]) -> None:
    """Fuegt Punkte an und verhindert doppelten Nahtpunkt zwischen Segmenten."""
    if not source:
        return
    if target and len(source[0]) >= 2 and target[-1] == [float(source[0][0]), float(source[0][1])]:
        for point in source[1:]:
            if len(point) >= 2:
                target.append([float(point[0]), float(point[1])])
        return
    for point in source:
        if len(point) >= 2:
            target.append([float(point[0]), float(point[1])])


def find_charging_window(
    decoded_shape: Sequence[Sequence[float]],
    segments: Sequence[Dict[str, object]],
    battery_capacity_kwh: float = BATTERY_CAPACITY_KWH,
) -> Tuple[List[List[float]], float | None, float | None]:
    """Ermittelt Koordinaten fuer den SoC-Bereich 15% -> 5%."""
    physics_model = EVPhysicsModel()
    current_kwh = battery_capacity_kwh * SOC_START
    start_threshold_kwh = battery_capacity_kwh * SOC_WINDOW_START
    end_threshold_kwh = battery_capacity_kwh * SOC_WINDOW_END

    charging_window_coords: List[List[float]] = []
    window_start_km: float | None = None
    window_end_km: float | None = None
    in_window = False
    cumulative_km = 0.0

    for segment in segments:
        distance_km = float(segment.get("distance_km", 0.0))
        speed_kmh = float(segment.get("speed_kmh", 0.0))
        duration_sec = float(segment.get("duration_sec", 0.0))
        delta_h_m = float(segment.get("delta_h_m", 0.0))

        begin_shape_index = int(segment.get("begin_shape_index", -1))
        end_shape_index = int(segment.get("end_shape_index", -1))
        actual_begin_idx = begin_shape_index
        actual_end_idx = end_shape_index

        segment_start_km = cumulative_km
        segment_end_km = cumulative_km + distance_km

        segment_kwh = physics_model.calculate_energy_kwh(
            distance_km=distance_km,
            speed_kmh=speed_kmh,
            duration_sec=duration_sec,
            delta_h_m=delta_h_m,
        )
        segment["segment_kwh"] = segment_kwh

        current_before = current_kwh
        current_after = current_before - segment_kwh
        exits_window_this_segment = False

        if not in_window and current_before >= start_threshold_kwh and current_after < start_threshold_kwh:
            in_window = True
            if current_before != current_after and distance_km > 0:
                fraction = (current_before - start_threshold_kwh) / (current_before - current_after)
                fraction = max(0.0, min(1.0, fraction))
                window_start_km = segment_start_km + (distance_km * fraction)
                actual_begin_idx = begin_shape_index + int(
                    fraction * (end_shape_index - begin_shape_index)
                )
            else:
                window_start_km = segment_start_km

        if in_window and current_before >= end_threshold_kwh and current_after < end_threshold_kwh:
            if current_before != current_after and distance_km > 0:
                fraction = (current_before - end_threshold_kwh) / (current_before - current_after)
                fraction = max(0.0, min(1.0, fraction))
                window_end_km = segment_start_km + (distance_km * fraction)
                actual_end_idx = begin_shape_index + int(
                    fraction * (end_shape_index - begin_shape_index)
                )
            else:
                window_end_km = segment_end_km
            exits_window_this_segment = True

        if in_window:
            coords = _shape_slice(decoded_shape, actual_begin_idx, actual_end_idx)
            _append_without_duplicate(charging_window_coords, coords)

        if exits_window_this_segment:
            break

        current_kwh = current_after
        cumulative_km = segment_end_km

    if in_window and window_end_km is None:
        window_end_km = cumulative_km

    return charging_window_coords, window_start_km, window_end_km


def find_chargers(bounding_box: Tuple[float, float, float, float]) -> List[Dict[str, object]]:
    """Findet CCS-Schnelllader in einer Bounding-Box bei OCM."""
    if not OCM_API_URL:
        raise ValueError("OCM_API_URL fehlt. Bitte in .env setzen.")
    if not OCM_API_KEY:
        raise ValueError("OCM_API_KEY fehlt. Bitte in .env setzen.")

    min_lat, max_lat, min_lon, max_lon = bounding_box
    boundingbox = f"({max_lat:.6f},{min_lon:.6f}),({min_lat:.6f},{max_lon:.6f})"

    params = {
        "boundingbox": boundingbox,
        "maxresults": 500,
        "connectiontypeid": OCM_CONNECTION_TYPE_ID,
        "minpowerkw": OCM_MIN_POWER_KW,
    }
    headers = {"User-Agent": OCM_USER_AGENT}
    headers["X-API-Key"] = OCM_API_KEY

    response = requests.get(OCM_API_URL, params=params, headers=headers, timeout=OCM_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        return []

    chargers: List[Dict[str, object]] = []
    for station in data:
        if not isinstance(station, dict):
            continue

        operator_info = station.get("OperatorInfo")
        address_info = station.get("AddressInfo")
        connections = station.get("Connections")

        operator_name = "Unbekannt"
        if isinstance(operator_info, dict):
            operator_name = str(operator_info.get("Title") or "Unbekannt")

        latitude: float | None = None
        longitude: float | None = None
        if isinstance(address_info, dict):
            try:
                raw_lat = address_info.get("Latitude")
                raw_lon = address_info.get("Longitude")
                latitude = float(raw_lat) if raw_lat is not None else None
                longitude = float(raw_lon) if raw_lon is not None else None
            except (TypeError, ValueError):
                latitude = None
                longitude = None

        chargers.append(
            {
                "operator": operator_name,
                "max_power_kw": _extract_max_power_kw(connections),
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    return chargers


def print_chargers(chargers: Sequence[Dict[str, object]]) -> None:
    """Gibt Ladesaeulen nach Score sortiert als Tabelle aus."""
    if not chargers:
        print("Keine passenden Ladesaeulen im Lade-Fenster gefunden.")
        return

    print("\n=== Ladesaeulen im Lade-Fenster ===")
    print(f"{'Rank':>4} | {'Betreiber':<30} | {'max kW':>7} | {'Umweg (min)':>11} | {'Score':>8}")
    print("-" * 78)
    for rank, charger in enumerate(chargers, start=1):
        operator = str(charger.get("operator", "Unbekannt"))
        max_power_kw = float(charger.get("max_power_kw", 0.0))
        detour_min = float(charger.get("detour_min", 999.0))
        score = float(charger.get("score", -9999.0))
        print(f"{rank:4d} | {operator[:30]:<30} | {max_power_kw:7.1f} | {detour_min:11.1f} | {score:8.1f}")


if __name__ == "__main__":
    start = [9.993682, 53.551086]  # Hamburg [lon, lat]
    end = [11.581981, 48.135125]  # Muenchen [lon, lat]

    try:
        route_data = get_valhalla_route(start, end)
        decoded_shape = route_data.get("decoded_shape", [])
        segments = route_data.get("segments", [])

        if not isinstance(decoded_shape, list) or not isinstance(segments, list):
            raise ValueError("Route-Daten haben ein ungueltiges Format.")

        charging_window_coords, window_start_km, window_end_km = find_charging_window(
            decoded_shape=decoded_shape,
            segments=segments,
            battery_capacity_kwh=BATTERY_CAPACITY_KWH,
        )

        if window_start_km is None:
            print("Lade-Fenster nicht erreicht: SoC faellt auf dieser Route nicht unter 15%.")
            raise SystemExit(0)

        if window_end_km is None:
            print("Lade-Fenster begonnen, aber 5% SoC auf der Route nicht erreicht.")
            raise SystemExit(0)

        print(
            f"Lade-Fenster: Start bei ca. {window_start_km:.1f} km, "
            f"Ende bei ca. {window_end_km:.1f} km."
        )

        if len(charging_window_coords) < 2:
            raise ValueError("Lade-Fenster enthält zu wenige Koordinaten für Detour-Routing.")

        window_start_coord = charging_window_coords[0]
        window_end_coord = charging_window_coords[-1]

        start_heading = get_bearing(
            float(charging_window_coords[0][0]),
            float(charging_window_coords[0][1]),
            float(charging_window_coords[1][0]),
            float(charging_window_coords[1][1]),
        )
        end_heading = get_bearing(
            float(charging_window_coords[-2][0]),
            float(charging_window_coords[-2][1]),
            float(charging_window_coords[-1][0]),
            float(charging_window_coords[-1][1]),
        )

        window_start = _to_valhalla_location(window_start_coord, heading=start_heading)
        window_end = _to_valhalla_location(window_end_coord, heading=end_heading)
        baseline_time_min = _get_trip_time_min(
            [window_start, window_end]
        )
        print(f"Korrigierte Baseline-Zeit: {baseline_time_min:.1f} min")

        bounding_box = get_bounding_box(charging_window_coords)
        chargers = find_chargers(bounding_box)

        candidates: List[Dict[str, object]] = []
        for charger in chargers:
            lat = charger.get("latitude")
            lon = charger.get("longitude")
            if lat is None or lon is None:
                continue

            charger_lat = float(lat)
            charger_lon = float(lon)

            if not is_within_window_radius(charger_lat, charger_lon, charging_window_coords):
                continue

            detour_min = get_detour_min(
                charger_lat=charger_lat,
                charger_lon=charger_lon,
                window_start=window_start,
                window_end=window_end,
                baseline_time_min=baseline_time_min,
            )
            max_kw = float(charger.get("max_power_kw", 0.0))
            score = max_kw - (detour_min * 15.0)

            charger["detour_min"] = detour_min
            charger["score"] = score
            candidates.append(charger)

        ranked = sorted(candidates, key=lambda item: float(item.get("score", -9999.0)), reverse=True)
        print_chargers(ranked)
    except (ValhallaRouteError, requests.RequestException, ValueError) as exc:
        print(f"Fehler bei der intelligenten Ladesaeulen-Suche: {exc}")
