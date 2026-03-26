"""Zentrale Logik für EV-Routing und Reichweitenberechnung."""

from typing import Dict, List, Tuple

from physics.ev_physics import EVPhysicsModel            # Wohnt jetzt im 'physics' Ordner
from .valhalla_route import decode_elevation_polyline    # Der Punkt (.) bedeutet: "Ist im selben Ordner wie ich"
from vehicles import VehicleSpecs                        # Bleibt gleich (liegt im Hauptverzeichnis)


def compute_route_energy_kwh(
    route_data: Dict[str, object], 
    vehicle: VehicleSpecs, 
    debug_label: str
) -> Tuple[float, float]:
    """
    Berechnet Energie via EVPhysicsModel über alle Valhalla-Segmente 
    inkl. kinetischer Energie (Beschleunigung/Bremsen).
    """
    segments = route_data.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("Route-Daten enthalten keine valide segments-Liste.")

    # NEU: Wir initialisieren die Physik mit dem ausgewählten Fahrzeug!
    physics_model = EVPhysicsModel(vehicle=vehicle)
    
    total_energy_kwh = 0.0
    total_distance_km = 0.0
    previous_speed_kmh = 0.0

    for idx, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue

        distance_km = float(segment.get("distance_km", 0.0))
        speed_kmh = float(segment.get("speed_kmh", 0.0))
        duration_sec = float(segment.get("duration_sec", 0.0))
        delta_h_m = float(segment.get("delta_h_m", 0.0))

        # Anti-Geister-Segment-Filter (verhindert Vollbremsungen an Kreuzungen)
        if distance_km <= 0.001 or duration_sec <= 0:
            continue

        segment_kwh = physics_model.calculate_energy_kwh(
            distance_km=distance_km,
            speed_kmh=speed_kmh,
            duration_sec=duration_sec,
            delta_h_m=delta_h_m,
            initial_speed_kmh=previous_speed_kmh
        )
        
        segment["segment_kwh"] = segment_kwh
        total_energy_kwh += segment_kwh
        total_distance_km += distance_km

        # Aktuelles Tempo für das nächste Segment speichern (kinetische Energie)
        previous_speed_kmh = speed_kmh

    return total_energy_kwh, total_distance_km


def normalize_elevation_values(raw_values: List[int]) -> List[float]:
    """Normalisiert rohe Höhenwerte auf Meter (Heuristik für Valhalla-Daten)."""
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


def extract_leg_elevation_stats(leg: Dict[str, object]) -> Dict[str, float]:
    """Extrahiert Elevation-Stats (Gain/Loss/Netto) für eine Etappe (Leg)."""
    elevation_raw = leg.get("elevation")
    values_m: List[float] = []

    if isinstance(elevation_raw, list):
        for value in elevation_raw:
            try:
                values_m.append(float(value))
            except (TypeError, ValueError):
                continue
    elif isinstance(elevation_raw, str) and elevation_raw.strip():
        values_m = normalize_elevation_values(decode_elevation_polyline(elevation_raw.strip()))

    if len(values_m) < 2:
        return {"elevation_gain_m": 0.0, "elevation_loss_m": 0.0, "net_elevation_m": 0.0}

    gain = 0.0
    loss = 0.0
    for prev, curr in zip(values_m, values_m[1:]):
        delta = curr - prev
        if delta > 0:
            gain += delta
        elif delta < 0:
            loss += abs(delta)

    net = values_m[-1] - values_m[0]
    return {
        "elevation_gain_m": round(gain, 1),
        "elevation_loss_m": round(loss, 1),
        "net_elevation_m": round(net, 1),
    }


def extract_leg_distance_time(leg: Dict[str, object]) -> Tuple[float, float]:
    """Extrahiert Distanz (km) und Zeit (sec) für eine Etappe."""
    summary = leg.get("summary")
    if isinstance(summary, dict):
        if "length" in summary and "time" in summary:
            return float(summary["length"]), float(summary["time"])

    maneuvers = leg.get("maneuvers", [])
    if isinstance(maneuvers, list):
        distance_km = 0.0
        time_sec = 0.0
        for maneuver in maneuvers:
            if not isinstance(maneuver, dict):
                continue
            distance_km += float(maneuver.get("length", 0.0))
            time_sec += float(maneuver.get("time", 0.0))
        return distance_km, time_sec

    return 0.0, 0.0