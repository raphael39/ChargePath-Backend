"""ChargeRout FastAPI Backend mit Multi-Stop EV-Routing."""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app_config import get_env_int, get_env_str
from charge_points import (
    find_chargers,
    find_charging_window,
    get_bearing,
    get_bounding_box,
    get_detour_min,
    is_within_window_radius,
)
from ev_physics import EVPhysicsModel
from valhalla_route import (
    ValhallaRouteError,
    decode_elevation_polyline,
    decode_polyline6,
    get_valhalla_route,
)

VALHALLA_ROUTE_URL = get_env_str("VALHALLA_ROUTE_URL")
VALHALLA_TIMEOUT_SECONDS = get_env_int("VALHALLA_TIMEOUT_SECONDS", 15)
VALHALLA_ELEVATION_INTERVAL_M = get_env_int("VALHALLA_ELEVATION_INTERVAL_M", 30)

BATTERY_CAPACITY_KWH = 75.0
TARGET_RESERVE_KWH = BATTERY_CAPACITY_KWH * 0.10
CHARGE_TIME_BUFFER_FACTOR = 1.15
MAX_CHARGE_SOC = 0.70
MAX_STOPS = 12

app = FastAPI(title="ChargeRout API")


class RouteRequest(BaseModel):
    start_lon: float
    start_lat: float
    dest_lon: float
    dest_lat: float


def _to_location(coord_lon_lat: Sequence[float]) -> Dict[str, float]:
    """Konvertiert [lon, lat] zu Valhalla-Location."""
    if len(coord_lon_lat) < 2:
        raise ValueError("Koordinate muss im Format [lon, lat] vorliegen.")
    return {"lon": float(coord_lon_lat[0]), "lat": float(coord_lon_lat[1])}


def _to_location_from_latlon(coord_lat_lon: Sequence[float], heading: int | None = None) -> Dict[str, object]:
    """Konvertiert [lat, lon] zu Valhalla-Location, optional mit heading."""
    if len(coord_lat_lon) < 2:
        raise ValueError("Koordinate muss im Format [lat, lon] vorliegen.")
    location: Dict[str, object] = {"lat": float(coord_lat_lon[0]), "lon": float(coord_lat_lon[1])}
    if heading is not None:
        location["heading"] = int(heading) % 360
    return location


def _build_payload(locations: List[Dict[str, object]]) -> Dict[str, object]:
    """Erstellt den Route-Payload analog zu valhalla_route.py."""
    return {
        "locations": locations,
        "costing": "auto",
        "costing_options": {"auto": {}},
        "directions_options": {"units": "kilometers", "elevation": True},
        "elevation_interval": VALHALLA_ELEVATION_INTERVAL_M,
        "format": "json",
    }


def _request_trip(locations: List[Dict[str, object]]) -> Dict[str, object]:
    """Fuehrt einen Valhalla-Trip-Request aus."""
    if not VALHALLA_ROUTE_URL:
        raise ValueError("VALHALLA_ROUTE_URL fehlt. Bitte in .env setzen.")

    response = requests.post(
        VALHALLA_ROUTE_URL,
        json=_build_payload(locations),
        timeout=VALHALLA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Valhalla-Antwort hat ein ungueltiges Format.")
    return data


def _extract_summary(trip_response: Dict[str, object]) -> Tuple[float, float]:
    """Extrahiert total distance (km) und time (min) aus der Trip-Response."""
    trip = trip_response.get("trip")
    if not isinstance(trip, dict):
        raise ValueError("Valhalla-Antwort enthält kein 'trip'-Objekt.")
    summary = trip.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("Valhalla-Antwort enthält keine 'summary'.")

    total_distance_km = float(summary["length"])
    total_time_min = float(summary["time"]) / 60.0
    return total_distance_km, total_time_min


def _compute_route_energy_kwh(route_data: Dict[str, object], debug_label: str) -> Tuple[float, float]:
    """Berechnet Energie via EVPhysicsModel über alle Valhalla-Segmente inkl. kinetischer Energie."""
    segments = route_data.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("Route-Daten enthalten keine valide segments-Liste.")

    physics_model = EVPhysicsModel()
    total_energy_kwh = 0.0
    total_distance_km = 0.0
    
    # NEU: Startgeschwindigkeit ist 0 km/h
    previous_speed_kmh = 0.0

    for idx, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue

        distance_km = float(segment.get("distance_km", 0.0))
        speed_kmh = float(segment.get("speed_kmh", 0.0))
        duration_sec = float(segment.get("duration_sec", 0.0))
        delta_h_m = float(segment.get("delta_h_m", 0.0))

        if distance_km <= 0.001 or duration_sec <= 0:
            continue

        # NEU: initial_speed_kmh übergeben!
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

        segment_kwh100 = (segment_kwh / distance_km) * 100.0 if distance_km > 0 else 0.0
        
        # NEU: Aktuelles Tempo für die nächste Runde speichern
        previous_speed_kmh = speed_kmh

    return total_energy_kwh, total_distance_km


def _normalize_elevation_values(raw_values: List[int]) -> List[float]:
    """Normalisiert rohe Höhenwerte auf Meter (Heuristik)."""
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


def _extract_leg_elevation_stats(leg: Dict[str, object]) -> Dict[str, float]:
    """Extrahiert Elevation-Stats (gain/loss/net) für ein Leg."""
    elevation_raw = leg.get("elevation")
    values_m: List[float] = []

    if isinstance(elevation_raw, list):
        for value in elevation_raw:
            try:
                values_m.append(float(value))
            except (TypeError, ValueError):
                continue
    elif isinstance(elevation_raw, str) and elevation_raw.strip():
        values_m = _normalize_elevation_values(decode_elevation_polyline(elevation_raw.strip()))

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


def _extract_leg_distance_time(leg: Dict[str, object]) -> Tuple[float, float]:
    """Extrahiert Distanz (km) und Zeit (sec) für ein Leg."""
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


@app.post("/plan-route")
async def plan_route(request: RouteRequest) -> Dict[str, object]:
    try:
        start = [request.start_lon, request.start_lat]
        destination = [request.dest_lon, request.dest_lat]

        current_start = [request.start_lon, request.start_lat]
        current_soc = 1.0
        stops: List[Dict[str, float]] = []
        chargers_info: List[Dict[str, object]] = []
        remaining_leg_distance_km = 0.0
        remaining_leg_energy_needed_kwh = 0.0

        while True:
            print(f"\n{'='*60}")
            print(f"--- RUNDE {len(stops) + 1} ---")
            print(f"Aktueller Standort: {current_start}")
            print(f"Aktueller Akku: {current_soc * 100:.1f} %")
            
            route_data = get_valhalla_route(current_start, destination)
            distance_to_destination_km = float(route_data.get("distance_km", 0.0))
            
            # Wir rufen hier _compute_route_energy_kwh auf (ich gehe davon aus, dass du dort 
            # die vielen Prints auskommentiert hast, damit wir hier den Überblick behalten)
            energy_needed_to_destination_kwh, _ = _compute_route_energy_kwh(
                route_data, f"Reststrecke {len(stops) + 1}"
            )
            available_energy_kwh = current_soc * BATTERY_CAPACITY_KWH

            print(f"Distanz ans Ziel: {distance_to_destination_km:.1f} km")
            print(f"Energie benötigt: {energy_needed_to_destination_kwh:.1f} kWh")
            print(f"Energie im Akku:  {available_energy_kwh:.1f} kWh")

            # Ziel ist ohne weiteren Stopp erreichbar (inkl. Zielreserve)
            if available_energy_kwh - energy_needed_to_destination_kwh >= TARGET_RESERVE_KWH:
                print(">>> Ziel ist erreichbar! Breche Schleife ab. <<<")
                remaining_leg_distance_km = distance_to_destination_km
                remaining_leg_energy_needed_kwh = energy_needed_to_destination_kwh
                break

            decoded_shape = route_data.get("decoded_shape", [])
            segments = route_data.get("segments", [])
            if not isinstance(decoded_shape, list) or not isinstance(segments, list):
                raise ValueError("Route-Daten haben ein ungueltiges Format.")

            charging_window_coords, window_start_km, window_end_km = find_charging_window(
                decoded_shape=decoded_shape,
                segments=segments,
                battery_capacity_kwh=BATTERY_CAPACITY_KWH,
                current_soc=current_soc,
            )
            
            print(f"Ladefenster gefunden: von km {window_start_km:.1f} bis km {window_end_km:.1f} auf dieser Etappe.")
            
            if window_start_km is None or window_end_km is None or len(charging_window_coords) < 2:
                raise ValueError("Kein valides Lade-Fenster (15%-5%) auf der aktuellen Route gefunden.")

            start_heading = get_bearing(
                float(charging_window_coords[0][0]), float(charging_window_coords[0][1]),
                float(charging_window_coords[1][0]), float(charging_window_coords[1][1]),
            )
            end_heading = get_bearing(
                float(charging_window_coords[-2][0]), float(charging_window_coords[-2][1]),
                float(charging_window_coords[-1][0]), float(charging_window_coords[-1][1]),
            )

            window_start_loc = _to_location_from_latlon(charging_window_coords[0], heading=start_heading)
            window_end_loc = _to_location_from_latlon(charging_window_coords[-1], heading=end_heading)
            baseline_response = _request_trip([window_start_loc, window_end_loc])
            _, baseline_time_min = _extract_summary(baseline_response)

            bounding_box = get_bounding_box(charging_window_coords)
            chargers = find_chargers(bounding_box)
            
            print(f"OCM hat in dieser Box {len(chargers)} Ladesäulen gefunden.")

            ranked: List[Dict[str, object]] = []
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
                    window_start=window_start_loc,
                    window_end=window_end_loc,
                    baseline_time_min=baseline_time_min,
                )
                max_kw = float(charger.get("max_power_kw", 0.0))
                score = max_kw - (detour_min * 15.0)

                enriched = dict(charger)
                enriched["detour_min"] = detour_min
                enriched["score"] = score
                ranked.append(enriched)

            ranked.sort(key=lambda item: float(item.get("score", -9999.0)), reverse=True)
            
            print(f"Nach Filterung (Radius & Umweg) bleiben {len(ranked)} nutzbare Säulen übrig.")
            
            if not ranked:
                raise ValueError("Kein geeigneter Ladestopp im Lade-Fenster gefunden.")

            best_charger = ranked[0]
            best_lat = float(best_charger["latitude"])
            best_lon = float(best_charger["longitude"])
            best_power_kw = float(best_charger.get("max_power_kw", 0.0))
            best_operator = str(best_charger.get("operator", "Unbekannt"))

            print(f"-> Gewählte Säule: {best_operator} ({best_power_kw} kW) an Koordinate {best_lon}, {best_lat}")

            # SoC bei Ankunft am gewählten Lader
            to_charger_route = get_valhalla_route(current_start, [best_lon, best_lat])
            energy_to_charger_kwh, _ = _compute_route_energy_kwh(
                to_charger_route, f"Zum Ladestopp {len(stops) + 1}"
            )
            energy_at_arrival_kwh = max(0.0, available_energy_kwh - energy_to_charger_kwh)

            target_energy_after_charge_kwh = BATTERY_CAPACITY_KWH * MAX_CHARGE_SOC
            required_charge_kwh = max(0.0, target_energy_after_charge_kwh - energy_at_arrival_kwh)

            if best_power_kw > 0:
                charging_time_min = (required_charge_kwh / best_power_kw) * 60.0
                charging_time_min *= CHARGE_TIME_BUFFER_FACTOR
            else:
                charging_time_min = 0.0

            soc_at_arrival_pct = (energy_at_arrival_kwh / BATTERY_CAPACITY_KWH) * 100.0
            soc_after_charge_pct = (target_energy_after_charge_kwh / BATTERY_CAPACITY_KWH) * 100.0

            chargers_info.append({
                "operator": best_operator,
                "max_power_kw": round(best_power_kw, 1),
                "lat": best_lat,
                "lon": best_lon,
                "charged_kwh": round(required_charge_kwh, 2),
                "charge_time_min": round(charging_time_min, 1),
                "soc_at_arrival_pct": round(soc_at_arrival_pct, 1),
                "soc_after_charge_pct": round(soc_after_charge_pct, 1),
            })

            stops.append({"lat": best_lat, "lon": best_lon})
            current_start = [best_lon, best_lat]
            current_soc = MAX_CHARGE_SOC

            if len(stops) >= MAX_STOPS:  # Absicherung
                print("!!! NOTABBRUCH: MAX_STOPS ERREICHT !!!")
                raise ValueError("Maximale Anzahl Ladestopps überschritten.")

        # Finale Multi-Waypoint Route über alle Stopps
        final_locations: List[Dict[str, object]] = [_to_location(start)]
        final_locations.extend(stops)
        final_locations.append(_to_location(destination))

        final_trip = _request_trip(final_locations)
        total_distance_km, total_time_min = _extract_summary(final_trip)

        total_charge_time_min = sum(float(item.get("charge_time_min", 0.0)) for item in chargers_info)
        total_trip_time_min = total_time_min + total_charge_time_min

        energy_at_destination_kwh = max(0.0, (current_soc * BATTERY_CAPACITY_KWH) - remaining_leg_energy_needed_kwh)
        soc_at_destination_pct = (energy_at_destination_kwh / BATTERY_CAPACITY_KWH) * 100.0

        trip_obj = final_trip.get("trip")
        legs = trip_obj.get("legs", []) if isinstance(trip_obj, dict) else []
        route_geometry: List[Dict[str, object]] = []
        navigation: List[Dict[str, object]] = []
        leg_metrics: List[Dict[str, object]] = []
        trip_energy_kwh = 0.0

        if isinstance(legs, list):
            waypoints_lon_lat: List[List[float]] = [start]
            waypoints_lon_lat.extend([[float(stop["lon"]), float(stop["lat"])] for stop in stops])
            waypoints_lon_lat.append(destination)

            leg_energy_kwh_map: Dict[int, float] = {}
            for idx in range(len(waypoints_lon_lat) - 1):
                leg_route_data = get_valhalla_route(waypoints_lon_lat[idx], waypoints_lon_lat[idx + 1])
                leg_energy_kwh, _ = _compute_route_energy_kwh(leg_route_data, f"Finales Leg {idx + 1}")
                leg_energy_kwh_map[idx] = leg_energy_kwh

            for idx, leg in enumerate(legs):
                if not isinstance(leg, dict):
                    continue

                if len(stops) == 0 and idx == 0:
                    title = "Etappe 1: Fahrt zum Ziel"
                elif idx < len(stops):
                    title = f"Etappe {idx + 1}: Fahrt zum Ladestopp {idx + 1}"
                elif idx == len(stops):
                    title = f"Etappe {idx + 1}: Fahrt zum Ziel"
                else:
                    title = f"Etappe {idx + 1}"

                maneuvers = leg.get("maneuvers", [])
                if not isinstance(maneuvers, list):
                    continue

                distance_km, time_sec = _extract_leg_distance_time(leg)
                elevation_stats = _extract_leg_elevation_stats(leg)
                drive_time_min = time_sec / 60.0
                duration_min = drive_time_min
                distance_m = distance_km * 1000.0
                leg_energy_kwh = float(leg_energy_kwh_map.get(idx, 0.0))
                trip_energy_kwh += leg_energy_kwh
                energy_usage_kwh100 = (leg_energy_kwh / distance_km) * 100.0 if distance_km > 0 else 0.0

                encoded_shape = leg.get("shape", "")
                decoded_leg_shape = (
                    decode_polyline6(encoded_shape) if isinstance(encoded_shape, str) and encoded_shape else []
                )
                route_geometry.append(
                    {
                        "leg_index": idx,
                        "distance_km": round(distance_km, 2),
                        "distance_m": round(distance_m, 1),
                        "time_sec": round(time_sec, 1),
                        "drive_time_min": round(drive_time_min, 2),
                        "duration_min": round(duration_min, 2),
                        "energy_usage_kwh100": round(energy_usage_kwh100, 2),
                        "elevation_gain_m": elevation_stats["elevation_gain_m"],
                        "elevation_loss_m": elevation_stats["elevation_loss_m"],
                        "net_elevation_m": elevation_stats["net_elevation_m"],
                        "encoded_shape": encoded_shape if isinstance(encoded_shape, str) else "",
                        "decoded_shape": decoded_leg_shape,
                    }
                )

                nav_maneuvers: List[Dict[str, object]] = []
                for maneuver in maneuvers:
                    if not isinstance(maneuver, dict):
                        continue
                    length_km = float(maneuver.get("length", 0.0))
                    instruction = str(maneuver.get("instruction", ""))
                    nav_maneuvers.append(
                        {
                            "distance_km": round(length_km, 2),
                            "instruction": instruction,
                        }
                    )

                navigation.append(
                    {
                        "leg_index": idx,
                        "title": title,
                        "distance_km": round(distance_km, 2),
                        "distance_m": round(distance_m, 1),
                        "time_sec": round(time_sec, 1),
                        "drive_time_min": round(drive_time_min, 2),
                        "duration_min": round(duration_min, 2),
                        "energy_usage_kwh100": round(energy_usage_kwh100, 2),
                        "elevation_gain_m": elevation_stats["elevation_gain_m"],
                        "elevation_loss_m": elevation_stats["elevation_loss_m"],
                        "net_elevation_m": elevation_stats["net_elevation_m"],
                        "maneuvers": nav_maneuvers,
                    }
                )
                leg_metrics.append(
                    {
                        "leg_index": idx,
                        "title": title,
                        "distance_km": round(distance_km, 2),
                        "distance_m": round(distance_m, 1),
                        "time_sec": round(time_sec, 1),
                        "drive_time_min": round(drive_time_min, 2),
                        "duration_min": round(duration_min, 2),
                        "energy_usage_kwh100": round(energy_usage_kwh100, 2),
                        "elevation_gain_m": elevation_stats["elevation_gain_m"],
                        "elevation_loss_m": elevation_stats["elevation_loss_m"],
                        "net_elevation_m": elevation_stats["net_elevation_m"],
                    }
                )

        return {
            "summary": {
                "total_distance_km": round(total_distance_km, 2),
                "drive_time_min": round(total_time_min, 1),
                "charge_time_min": round(total_charge_time_min, 1),
                "total_trip_time_min": round(total_trip_time_min, 1),
                "soc_at_destination_pct": round(soc_at_destination_pct, 1),
                "num_stops": len(stops),
                "remaining_leg_distance_km": round(remaining_leg_distance_km, 2),
                "trip_energy_kwh": round(trip_energy_kwh, 2),
            },
            "chargers": chargers_info,
            "route_geometry": route_geometry,
            "navigation": navigation,
            "leg_metrics": leg_metrics,
        }
    except HTTPException:
        raise
    except (ValhallaRouteError, requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
