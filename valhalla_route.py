"""Valhalla routing client.

Dieses Modul kapselt die Anfrage an eine Valhalla-Route-API,
parst die wichtigsten Kennzahlen und decodiert die von Valhalla
zurückgegebene Polyline6-Geometrie in [lat, lon]-Koordinaten.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple, TypedDict, Union

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
    """Normalisiert eine Koordinate in Valhalla-Format {"lon": x, "lat": y}.

    Erlaubte Eingaben:
    - Dict mit Schlüsseln "lon"/"lat"
    - Tuple/Liste im Format [lon, lat]
    """
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
    """Decodiert eine Valhalla-Polyline (Precision=6) in [lat, lon]-Punkte.

    Valhalla nutzt standardmäßig eine codierte Polyline mit Skalierung 1e6.
    Rückgabeformat: [[lat, lon], ...]
    """
    if not encoded:
        return []

    coordinates: List[List[float]] = []
    index = 0
    lat = 0
    lon = 0
    factor = 1e6

    while index < len(encoded):
        # Latitude-Differenz dekodieren
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

        # Longitude-Differenz dekodieren
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
    """Normalisiert rohe Höhenwerte auf Meter.

    In manchen Setups sind Werte in 0.1m/0.01m skaliert. Diese Heuristik
    greift nur bei unplausibel großen Höhen und lässt normale Meterwerte
    unverändert.
    """
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
            "auto": {
                # Einige Valhalla/Stadia-Versionen reagieren konsistenter auf
                # einen expliziten auto-Block.
            }
        },
        "directions_options": {
            "units": "kilometers",
            "elevation": True,
        },
        "elevation_interval": ELEVATION_INTERVAL_M,
        "format": "json",
    }


def _extract_route_data(response_json: Dict[str, object]) -> Dict[str, object]:
    """Extrahiert Distanz, Dauer, Shape und Segmentdaten aus einer Valhalla-Antwort."""
    try:
        trip = response_json["trip"]
        if not isinstance(trip, dict):
            raise TypeError("trip ist kein Dict")

        summary = trip["summary"]
        if not isinstance(summary, dict):
            raise TypeError("summary ist kein Dict")

        legs = trip["legs"]
        if not isinstance(legs, list):
            raise TypeError("legs ist keine Liste")

        shape = legs[0]["shape"]
        if not isinstance(shape, str):
            raise TypeError("shape ist kein String")

        distance_km = float(summary["length"])  # Valhalla typischerweise in km
        duration_min = float(summary["time"]) / 60.0  # Zeit in Sekunden -> Minuten
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValhallaRouteError(f"Unerwartetes Antwortformat von Valhalla: {exc}") from exc

    decoded_coordinates = decode_polyline6(shape)
    segments: List[SegmentData] = []

    for leg in legs:
        if not isinstance(leg, dict):
            continue

        elevation_values_m: List[float] = []
        elevation_raw = leg.get("elevation")

        if isinstance(elevation_raw, str):
            if elevation_raw.strip():
                elevation_values_m = _normalize_elevation_values(
                    decode_elevation_polyline(elevation_raw.strip())
                )
        elif isinstance(elevation_raw, list):
            numeric_values: List[float] = []
            for value in elevation_raw:
                try:
                    numeric_values.append(float(value))
                except (TypeError, ValueError):
                    continue
            elevation_values_m = numeric_values

        maneuvers = leg.get("maneuvers", [])
        if not isinstance(maneuvers, list):
            continue

        leg_elevation_interval_m = float(leg.get("elevation_interval", ELEVATION_INTERVAL_M))
        leg_distance_cursor_km = 0.0

        # Fallback: Manche Antworten enthalten Elevation nicht auf leg-Ebene.
        # Dann prüfen wir optional maneuver.elevation (falls vorhanden).
        if not elevation_values_m:
            maneuver_elevation_values: List[float] = []
            for maneuver in maneuvers:
                if not isinstance(maneuver, dict):
                    continue
                maneuver_elevation = maneuver.get("elevation")
                if isinstance(maneuver_elevation, list):
                    for value in maneuver_elevation:
                        try:
                            maneuver_elevation_values.append(float(value))
                        except (TypeError, ValueError):
                            continue
            if maneuver_elevation_values:
                elevation_values_m = maneuver_elevation_values

        for maneuver in maneuvers:
            if not isinstance(maneuver, dict):
                continue

            distance_segment_km = float(maneuver.get("length", 0.0))
            duration_segment_sec = float(maneuver.get("time", 0.0))
            speed_kmh = (
                (distance_segment_km / duration_segment_sec) * 3600.0
                if duration_segment_sec > 0
                else 0.0
            )
            begin_shape_index = int(maneuver.get("begin_shape_index", -1))
            end_shape_index = int(maneuver.get("end_shape_index", -1))
            delta_h_m = 0.0

            if elevation_values_m:
                max_idx = len(elevation_values_m) - 1
                if 0 <= begin_shape_index <= max_idx and 0 <= end_shape_index <= max_idx:
                    delta_h_m = float(
                        elevation_values_m[end_shape_index] - elevation_values_m[begin_shape_index]
                    )
                elif leg_elevation_interval_m > 0:
                    # Bei elevation_interval ist elevation meist distanzbasiert sampled.
                    start_m = leg_distance_cursor_km * 1000.0
                    end_m = (leg_distance_cursor_km + distance_segment_km) * 1000.0
                    start_idx = min(max_idx, max(0, int(round(start_m / leg_elevation_interval_m))))
                    end_idx = min(max_idx, max(0, int(round(end_m / leg_elevation_interval_m))))
                    delta_h_m = float(elevation_values_m[end_idx] - elevation_values_m[start_idx])

            segments.append(
                {
                    "begin_shape_index": begin_shape_index,
                    "end_shape_index": end_shape_index,
                    "distance_km": distance_segment_km,
                    "duration_sec": duration_segment_sec,
                    "speed_kmh": speed_kmh,
                    "delta_h_m": delta_h_m,
                    "segment_kwh": 0.0,
                    "instruction": str(maneuver.get("instruction", "")),
                }
            )
            leg_distance_cursor_km += distance_segment_km

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
    """Berechnet eine Route von A nach B über die Valhalla API.

    Args:
        start_coords: Dict {'lon','lat'} oder [lon, lat]
        end_coords: Dict {'lon','lat'} oder [lon, lat]
        endpoint: Valhalla /route Endpoint
        timeout_seconds: Request-Timeout

    Returns:
        Dict mit Distanz (km), Dauer (min), codierter und decodierter Geometrie.
    """
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
        raise ValhallaRouteError(f"Valhalla API nicht erreichbar oder Request fehlgeschlagen: {exc}") from exc
    except ValueError as exc:
        raise ValhallaRouteError(f"Antwort ist kein valides JSON: {exc}") from exc

    return _extract_route_data(data)
