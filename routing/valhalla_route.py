"""Valhalla Routing API Client und Polyline-Decoder."""

import math
import requests
from typing import Dict, List, Any

from app_config import get_env_str, get_env_int

# --- KONFIGURATION ---
VALHALLA_ROUTE_URL = get_env_str("VALHALLA_ROUTE_URL", "http://localhost:8002/route")
VALHALLA_TIMEOUT_SECONDS = get_env_int("VALHALLA_TIMEOUT_SECONDS", 15)


class ValhallaRouteError(Exception):
    """Eigener Fehler für Valhalla-Abstürze oder Routen-Probleme."""
    pass


def decode_polyline6(encoded: str) -> List[List[float]]:
    """Entschlüsselt die 2D-Shape-Polylines von Valhalla (Precision 6)."""
    inv = 1.0 / 1e6
    decoded = []
    previous = [0, 0]
    i = 0
    length = len(encoded)
    
    while i < length:
        ll = [0, 0]
        for j in [0, 1]:
            shift = 0
            byte = 0x20
            res = 0
            while byte >= 0x20:
                byte = ord(encoded[i]) - 63
                i += 1
                res |= (byte & 0x1F) << shift
                shift += 5
            comp = ~(res >> 1) if (res & 1) else (res >> 1)
            previous[j] += comp
            ll[j] = previous[j] * inv
        # Valhalla liefert [Lat, Lon]
        decoded.append([ll[0], ll[1]])
        
    return decoded


def decode_elevation_polyline(encoded: str) -> List[int]:
    """Entschlüsselt die 1D-Höhen-Polylines von Valhalla."""
    decoded = []
    previous = 0
    i = 0
    length = len(encoded)
    
    while i < length:
        shift = 0
        byte = 0x20
        res = 0
        while byte >= 0x20:
            byte = ord(encoded[i]) - 63
            i += 1
            res |= (byte & 0x1F) << shift
            shift += 5
        comp = ~(res >> 1) if (res & 1) else (res >> 1)
        previous += comp
        decoded.append(previous)
        
    return decoded


def get_valhalla_route(start: List[float], dest: List[float]) -> Dict[str, Any]:
    """
    Holt die Route von Valhalla und zerteilt sie in physikalische Manöver-Segmente.
    start/dest: [lon, lat]
    """
    payload = {
        "locations": [
            {"lon": start[0], "lat": start[1]},
            {"lon": dest[0], "lat": dest[1]}
        ],
        "costing": "auto",
        "directions_options": {"units": "kilometers", "elevation": True},
        "format": "json"
    }

    try:
        resp = requests.post(VALHALLA_ROUTE_URL, json=payload, timeout=VALHALLA_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise ValhallaRouteError(f"Valhalla API Fehler: {e}")

    trip = data.get("trip", {})
    legs = trip.get("legs", [])
    if not legs:
        raise ValhallaRouteError("Valhalla hat keine Route gefunden.")

    leg = legs[0]
    summary = trip.get("summary", {})
    total_distance_km = float(summary.get("length", 0.0))

    # Geometrie und Höhe entschlüsseln
    encoded_shape = leg.get("shape", "")
    decoded_shape = decode_polyline6(encoded_shape) if encoded_shape else []

    encoded_elev = leg.get("elevation", "")
    decoded_elev = decode_elevation_polyline(encoded_elev) if encoded_elev else []

    # Höhenwerte normalisieren (Valhalla-Heuristik)
    elev_m = [0.0] * len(decoded_shape)
    if decoded_elev:
        max_abs = max(abs(v) for v in decoded_elev)
        scale = 100.0 if max_abs > 120_000 else 10.0 if max_abs > 12_000 else 1.0
        elev_m = [float(v) / scale for v in decoded_elev]

    # Segmente (Manöver) für die Physik-Engine bauen
    segments = []
    for man in leg.get("maneuvers", []):
        dist_km = float(man.get("length", 0.0))
        time_sec = float(man.get("time", 0.0))
        b_idx = int(man.get("begin_shape_index", 0))
        e_idx = int(man.get("end_shape_index", 0))

        # Geschwindigkeit für dieses Manöver berechnen
        speed_kmh = (dist_km / (time_sec / 3600.0)) if time_sec > 0 else 0.0

        # Höhendifferenz aus den dekodierten Punkten holen
        delta_h_m = 0.0
        if 0 <= b_idx < len(elev_m) and 0 <= e_idx < len(elev_m):
            delta_h_m = elev_m[e_idx] - elev_m[b_idx]

        segments.append({
            "distance_km": dist_km,
            "duration_sec": time_sec,
            "speed_kmh": speed_kmh,
            "delta_h_m": delta_h_m,
            "begin_shape_index": b_idx,
            "end_shape_index": e_idx
        })

    return {
        "distance_km": total_distance_km,
        "decoded_shape": decoded_shape,
        "segments": segments
    }