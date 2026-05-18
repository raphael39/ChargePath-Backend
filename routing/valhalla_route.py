"""Valhalla Routing API Client und Polyline-Decoder."""

import math
import requests
from typing import Dict, List, Any

from app_config import get_env_str, get_env_int

# --- KONFIGURATION ---
VALHALLA_ROUTE_URL = get_env_str("VALHALLA_ROUTE_URL", "http://localhost:8002/route")
VALHALLA_TIMEOUT_SECONDS = get_env_int("VALHALLA_TIMEOUT_SECONDS", 15)
# Sampling-Distanz fuer die Elevation-Polyline in Metern.
# 30m ist Valhalla-Default und ein guter Kompromiss zwischen Genauigkeit
# (Steigungen ueber kurze Strecken erfasst) und Payload-Groesse.
VALHALLA_ELEVATION_INTERVAL_M = get_env_int("VALHALLA_ELEVATION_INTERVAL_M", 30)

# Smoothing-Fenster (Anzahl Samples) fuer die Hoehen-Polyline vor der
# Gain/Loss-Berechnung. Eliminiert SRTM-Pixel-Rauschen und respektiert die
# DIN-Mindest-Krümmungsradien fuer Autobahn-Trassen (vertikale Wellen unter
# ~200m Länge sind nicht real). Default 11 Samples × 30m = 330m Fenster.
# 1 = kein Smoothing, hoehere Werte = staerker geglaettet.
ELEVATION_SMOOTHING_WINDOW = get_env_int("ELEVATION_SMOOTHING_WINDOW", 11)


class ValhallaRouteError(Exception):
    """Eigener Fehler für Valhalla-Abstürze oder Routen-Probleme."""
    pass


def _moving_average(values: List[float], window: int) -> List[float]:
    """Gleitender Mittelwert mit symmetrischem Fenster (Edge-Padding).

    Behaelt die Listen-Laenge bei, wendet kleinere Fenster an den Raendern an
    (kein "lost samples" am Anfang/Ende).
    Eigenschaft: Net-Differenz (values[-1] - values[0]) ist invariant unter
    diesem Filter.
    """
    if window <= 1 or len(values) < 2:
        return list(values)
    half = window // 2
    n = len(values)
    smoothed: List[float] = []
    for i in range(n):
        start = max(0, i - half)
        end = min(n, i + half + 1)
        smoothed.append(sum(values[start:end]) / (end - start))
    return smoothed


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


def _parse_leg(leg: Dict[str, Any]) -> Dict[str, Any]:
    """Wandelt ein einzelnes Valhalla-Leg in unser Segments-Format um.

    Wichtig zur Elevation-Indexierung: Valhalla liefert die ``elevation``-
    Polyline an festen Distanz-Intervallen (z.B. alle 30 m, siehe
    ``elevation_interval``-Feld auf dem Leg). Die ``shape``-Polyline ist
    geometrisch gesampelt (dichter an Kurven, duenner auf Geraden) und
    hat eine voellig andere Sampling-Rate. ``begin_shape_index`` und
    ``end_shape_index`` zeigen auf ``shape`` — nicht auf ``elevation``.
    Wir muessen also pro Segment die kumulative Fahrtdistanz mitlaufen
    lassen und ueber ``offset_m / elevation_interval_m`` die korrekten
    elevation-Indices berechnen.

    Wird sowohl von ``get_valhalla_route`` (1-Leg-Trip) als auch von
    ``get_route_via_intermediate`` (2-Leg-Trip) verwendet.
    """
    encoded_shape = leg.get("shape", "")
    decoded_shape = decode_polyline6(encoded_shape) if encoded_shape else []

    # Stadia Maps / Valhalla liefert ``elevation`` als reine Liste von
    # Floats in Metern (NICHT encoded). Falls aus aelterer Doku-Erwartung
    # mal eine encoded Polyline-String kommt, fangen wir das ab.
    elev_raw = leg.get("elevation")
    elev_m: List[float] = []
    if isinstance(elev_raw, list):
        elev_m = [float(v) for v in elev_raw]
    elif isinstance(elev_raw, str) and elev_raw:
        # Encoded Format (Legacy) — Skalierungs-Heuristik wie frueher
        decoded_elev = decode_elevation_polyline(elev_raw)
        if decoded_elev:
            max_abs = max(abs(v) for v in decoded_elev)
            scale = 100.0 if max_abs > 120_000 else 10.0 if max_abs > 12_000 else 1.0
            elev_m = [float(v) / scale for v in decoded_elev]

    # Smoothing gegen SRTM-Pixel-Rauschen: ohne Filter wuerden ±2-5m
    # DEM-Schwankungen die Gain/Loss-Werte um Faktor 1.5-2× ueberzeichnen.
    # Default-Fenster 11 Samples × 30m = 330m entspricht der DIN-Mindest-
    # Wellenlaenge fuer Autobahn-Trassen.
    if elev_m and ELEVATION_SMOOTHING_WINDOW > 1:
        elev_m = _moving_average(elev_m, ELEVATION_SMOOTHING_WINDOW)

    # Valhalla liefert das Sampling-Intervall mit. Falls fehlt: Default 30 m.
    elevation_interval_m = float(leg.get("elevation_interval", VALHALLA_ELEVATION_INTERVAL_M))

    segments = []
    cumulative_distance_m = 0.0  # entlang des Legs

    for man in leg.get("maneuvers", []):
        dist_km = float(man.get("length", 0.0))
        time_sec = float(man.get("time", 0.0))
        b_idx = int(man.get("begin_shape_index", 0))
        e_idx = int(man.get("end_shape_index", 0))
        speed_kmh = (dist_km / (time_sec / 3600.0)) if time_sec > 0 else 0.0

        # Elevation: pro Segment ueber ALLE 30m-Samples iterieren und Gain/Loss
        # separat aufsummieren. So gehen Auf-Ab-Profile innerhalb eines langen
        # Maneuvers nicht verloren (Valhalla aggregiert lange Autobahn-Abschnitte
        # gerne in ein einziges Maneuver, z.B. 109km am Stueck).
        # delta_h_m bleibt als Netto-Differenz (gain - loss) erhalten,
        # zusaetzlich werden elevation_gain_m und elevation_loss_m exponiert,
        # damit die Physik mit den richtigen Effizienzen rechnen kann.
        delta_h_m = 0.0
        elev_gain_m = 0.0
        elev_loss_m = 0.0
        if elev_m and elevation_interval_m > 0:
            seg_start_m = cumulative_distance_m
            seg_end_m = cumulative_distance_m + dist_km * 1000.0
            n = len(elev_m)
            start_idx = max(0, min(int(seg_start_m / elevation_interval_m), n - 1))
            end_idx = max(0, min(int(seg_end_m / elevation_interval_m), n - 1))
            # Netto wie vorher
            delta_h_m = elev_m[end_idx] - elev_m[start_idx]
            # Profil-Aufschluesselung: alle Mikro-Hoehenaenderungen aufsummieren
            for i in range(start_idx, end_idx):
                step = elev_m[i + 1] - elev_m[i]
                if step > 0:
                    elev_gain_m += step
                elif step < 0:
                    elev_loss_m += -step

        segments.append({
            "distance_km": dist_km,
            "duration_sec": time_sec,
            "speed_kmh": speed_kmh,
            "delta_h_m": delta_h_m,
            "elevation_gain_m": elev_gain_m,
            "elevation_loss_m": elev_loss_m,
            "begin_shape_index": b_idx,
            "end_shape_index": e_idx,
        })
        cumulative_distance_m += dist_km * 1000.0

    leg_summary = leg.get("summary", {}) if isinstance(leg.get("summary"), dict) else {}
    return {
        "distance_km": float(leg_summary.get("length", 0.0)),
        "duration_sec": float(leg_summary.get("time", 0.0)),
        "decoded_shape": decoded_shape,
        "segments": segments,
    }


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
        # WICHTIG: elevation_interval auf Root-Level, NICHT in directions_options.
        # Stadia Maps / Valhalla liefert sonst keinen "elevation"-Polyline zurueck.
        "elevation_interval": VALHALLA_ELEVATION_INTERVAL_M,
        "directions_options": {"units": "kilometers"},
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

    summary = trip.get("summary", {})
    parsed = _parse_leg(legs[0])
    # Fallback fuer Trip-Summary-Distanz (Doppelpunkt-Robustheit)
    parsed["distance_km"] = float(summary.get("length", parsed["distance_km"]))
    return parsed


def get_route_via_intermediate(
    start: List[float],
    via: List[float],
    end: List[float],
) -> Dict[str, Any]:
    """3-Punkt-Valhalla-Call (Start -> Via -> End) mit Elevation.

    Praktisch fuer Detour + Energie in einem Call: ``legs[0]`` gibt die
    Strecke (mit Segmenten und Hoehenprofil) von Start zum Via-Punkt
    (Charger), ``legs[1]`` die Strecke vom Charger zurueck zum End-Punkt
    (Window-End). Aus der Summary kommt die Gesamtzeit fuer den Detour-
    Vergleich.

    start/via/end: ``[lon, lat]``-Paare.
    """
    payload = {
        "locations": [
            {"lon": start[0], "lat": start[1]},
            {"lon": via[0], "lat": via[1]},
            {"lon": end[0], "lat": end[1]},
        ],
        "costing": "auto",
        # elevation_interval auf Root-Level (siehe Hinweis in get_valhalla_route)
        "elevation_interval": VALHALLA_ELEVATION_INTERVAL_M,
        "directions_options": {"units": "kilometers"},
        "format": "json",
    }

    try:
        resp = requests.post(VALHALLA_ROUTE_URL, json=payload, timeout=VALHALLA_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise ValhallaRouteError(f"Valhalla 3-Punkt-Trip Fehler: {e}")

    trip = data.get("trip", {})
    legs = trip.get("legs", [])
    if len(legs) < 2:
        raise ValhallaRouteError("Valhalla 3-Punkt-Trip hat weniger als 2 Legs zurueckgegeben.")

    summary = trip.get("summary", {})
    return {
        "total_distance_km": float(summary.get("length", 0.0)),
        "total_time_min": float(summary.get("time", 0.0)) / 60.0,
        "legs": [_parse_leg(legs[0]), _parse_leg(legs[1])],
    }