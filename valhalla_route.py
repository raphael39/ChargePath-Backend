"""Valhalla routing client.

Dieses Modul kapselt die Anfrage an eine Valhalla-Route-API,
parst die wichtigsten Kennzahlen und decodiert die von Valhalla
zurückgegebene Polyline6-Geometrie in [lat, lon]-Koordinaten.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple, TypedDict, Union
import math
import requests

from app_config import get_env_int, get_env_str

VALHALLA_ROUTE_URL = get_env_str("VALHALLA_ROUTE_URL")
ELEVATION_INTERVAL_M = get_env_int("VALHALLA_ELEVATION_INTERVAL_M", 30)
VALHALLA_TIMEOUT_SECONDS = get_env_int("VALHALLA_TIMEOUT_SECONDS", 15)

CoordinateInput = Union[Dict[str, float], Sequence[float]]
LatLon = Tuple[float, float]


class ValhallaRouteError(Exception):
    """Spezifischer Fehler für Valhalla-Routing-Probleme."""


class SegmentData(TypedDict):
    """Typdefinition für ein extrahiertes Routing-Segment."""
    begin_shape_index: int
    end_shape_index: int
    distance_km: float
    duration_sec: float
    speed_kmh: float
    delta_h_m: float
    segment_kwh: float
    instruction: str


def normalize_coordinate(coord: CoordinateInput) -> Dict[str, float]:
    """Normalisiert eine Koordinate in Valhalla-Format {"lon": x, "lat": y}."""
    if isinstance(coord, dict):
        if "lon" not in coord or "lat" not in coord:
            raise ValueError("Coordinate dict muss 'lon' und 'lat' enthalten.")
        lon = float(coord["lon"])
        lat = float(coord["lat"])
        return {"lon": lon, "lat": lat}

    if isinstance(coord, (tuple, list)) and len(coord) == 2:
        lon = float(coord[0])
        lat = float(coord[1])
        return {"lon": lon, "lat": lat}

    raise ValueError("Koordinate muss Dict {'lon','lat'} oder [lon, lat] sein.")


def decode_polyline6(encoded: str) -> List[List[float]]:
    """Decodiert eine Valhalla-Polyline (Precision=6) in [lat, lon]-Punkte."""
    if not encoded:
        return []

    coordinates: List[List[float]] = []
    index = 0
    lat = 0
    lon = 0
    factor = 1e6

    while index < len(encoded):
        shift = 0
        result = 0
        while True:
            if index >= len(encoded):
                raise ValhallaRouteError("Ungültige Polyline: unerwartetes Ende (lat).")
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break

        delta_lat = ~(result >> 1) if result & 1 else (result >> 1)
        lat += delta_lat

        shift = 0
        result = 0
        while True:
            if index >= len(encoded):
                raise ValhallaRouteError("Ungültige Polyline: unerwartetes Ende (lon).")
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break

        delta_lon = ~(result >> 1) if result & 1 else (result >> 1)
        lon += delta_lon

        coordinates.append([lat / factor, lon / factor])

    return coordinates


def decode_elevation_polyline(encoded: str) -> List[int]:
    """Decodiert Valhalla-Elevation (1D, delta-kodiert, Integer-Meter)."""
    if not encoded:
        return []

    values: List[int] = []
    index = 0
    current = 0

    while index < len(encoded):
        shift = 0
        result = 0

        while True:
            if index >= len(encoded):
                raise ValhallaRouteError("Ungültige Elevation-Polyline: unerwartetes Ende.")
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break

        delta = ~(result >> 1) if result & 1 else (result >> 1)
        current += delta
        values.append(current)

    return values


def _normalize_elevation_values(raw_values: List[int]) -> List[float]:
    """Normalisiert rohe Höhenwerte auf Meter."""
    if not raw_values:
        return []

    max_abs = max(abs(v) for v in raw_values)
    if max_abs > 120_000:
        scale = 100.0
    elif max_abs > 12_000:
        scale = 10.0
    else:
        scale = 1.0

    return [float(v) / scale for v in raw_values]


def _build_route_payload(start: Dict[str, float], end: Dict[str, float]) -> Dict[str, object]:
    """Erstellt den Request-Body für Valhalla."""
    return {
        "locations": [start, end],
        "costing": "auto",
        "costing_options": {
            "auto": {}  # <-- WICHTIG: Das hatte ich gelöscht, Valhalla braucht es oft!
        },
        "directions_options": {
            "units": "kilometers",
            "elevation": True,
        },
        "elevation_interval": ELEVATION_INTERVAL_M,
        "format": "json",
    }


def _extract_route_data(response_json: Dict[str, object]) -> Dict[str, object]:
    """Extrahiert Distanz, Dauer und zerteilt Maneuvers in 30m-Höhen-Slices."""
    try:
        trip = response_json["trip"]
        summary = trip["summary"]
        legs = trip["legs"]
        shape = legs[0]["shape"]

        distance_km = float(summary["length"])
        duration_min = float(summary["time"]) / 60.0
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValhallaRouteError(f"Unerwartetes Antwortformat: {exc}") from exc

    decoded_coordinates = decode_polyline6(shape)
    segments: List[SegmentData] = []

    for leg in legs:
        if not isinstance(leg, dict):
            continue

        elevation_raw = leg.get("elevation")
        elevation_values_m = []

        if isinstance(elevation_raw, str) and elevation_raw.strip():
            elevation_values_m = _normalize_elevation_values(
                decode_elevation_polyline(elevation_raw.strip())
            )
        elif isinstance(elevation_raw, list):
            elevation_values_m = [float(v) for v in elevation_raw if v is not None]

        if not elevation_values_m:
            for maneuver in leg.get("maneuvers", []):
                if not isinstance(maneuver, dict):
                    continue
                maneuver_el = maneuver.get("elevation")
                if isinstance(maneuver_el, list):
                    elevation_values_m.extend([float(v) for v in maneuver_el if v is not None])

        leg_elevation_interval_m = float(leg.get("elevation_interval", ELEVATION_INTERVAL_M))
        
        # Cursor für die Gesamtdistanz des Legs (in Metern)
        global_dist_m = 0.0

        for maneuver in leg.get("maneuvers", []):
            if not isinstance(maneuver, dict):
                continue

            maneuver_dist_m = float(maneuver.get("length", 0.0)) * 1000.0
            maneuver_dur_sec = float(maneuver.get("time", 0.0))
            instruction = str(maneuver.get("instruction", ""))
            begin_shape_index = int(maneuver.get("begin_shape_index", -1))
            end_shape_index = int(maneuver.get("end_shape_index", -1))
            
            # Geschwindigkeit für das gesamte Manöver berechnen
            speed_kmh = (maneuver_dist_m / 1000.0 / maneuver_dur_sec) * 3600.0 if maneuver_dur_sec > 0 else 0.0

            # --- NEUE LOGIK: Manöver in kleinere Slices unterteilen ---
            # Wenn das Manöver z.B. 1000m lang ist, zerteilen wir es in ~33 Stücke à 30m
            
            slice_length_m = leg_elevation_interval_m if leg_elevation_interval_m > 0 else 30.0
            num_slices = int(maneuver_dist_m // slice_length_m)
            remainder_m = maneuver_dist_m % slice_length_m
            
            slices_to_create = []
            for _ in range(num_slices):
                slices_to_create.append(slice_length_m)
            if remainder_m > 0:
                slices_to_create.append(remainder_m)

            for slice_m in slices_to_create:
                if slice_m <= 0:
                    continue

                start_idx_m = global_dist_m
                end_idx_m = global_dist_m + slice_m
                
                delta_h = 0.0
                if elevation_values_m and len(elevation_values_m) > 1 and leg_elevation_interval_m > 0:
                    max_idx = len(elevation_values_m) - 1
                    start_idx = min(max_idx, max(0, int(round(start_idx_m / leg_elevation_interval_m))))
                    end_idx = min(max_idx, max(0, int(round(end_idx_m / leg_elevation_interval_m))))
                    delta_h = float(elevation_values_m[end_idx] - elevation_values_m[start_idx])

                slice_dur_sec = (slice_m / maneuver_dist_m) * maneuver_dur_sec if maneuver_dist_m > 0 else 0

                segments.append({
                    "begin_shape_index": begin_shape_index,
                    "end_shape_index": end_shape_index,
                    "distance_km": slice_m / 1000.0,
                    "duration_sec": slice_dur_sec,
                    "speed_kmh": speed_kmh,
                    "delta_h_m": delta_h,
                    "segment_kwh": 0.0,
                    "instruction": instruction
                })
                
                global_dist_m += slice_m

    return {
        "distance_km": distance_km,
        "duration_min": duration_min,
        "shape": shape,
        "decoded_shape": decoded_coordinates,
        "segments": segments,
    }


def get_valhalla_route(
    start_coords: CoordinateInput,
    end_coords: CoordinateInput,
    endpoint: str = VALHALLA_ROUTE_URL,
    timeout_seconds: int = VALHALLA_TIMEOUT_SECONDS,
) -> Dict[str, object]:
    """Berechnet eine Route von A nach B über die Valhalla API."""
    start = normalize_coordinate(start_coords)
    end = normalize_coordinate(end_coords)
    if not endpoint:
        raise ValhallaRouteError("VALHALLA_ROUTE_URL fehlt. Bitte in .env setzen.")
    payload = _build_route_payload(start, end)

    try:
        response = requests.post(endpoint, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as exc:
        raise ValhallaRouteError(f"Valhalla API Error: {exc}") from exc
    except ValueError as exc:
        raise ValhallaRouteError(f"Invalid JSON: {exc}") from exc

    return _extract_route_data(data)