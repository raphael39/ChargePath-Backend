"""Master-Skript fuer ChargeRout: End-to-End EV-Reiseplanung."""

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
from valhalla_route import ValhallaRouteError, decode_polyline6, get_valhalla_route

VALHALLA_ROUTE_URL = get_env_str("VALHALLA_ROUTE_URL")
VALHALLA_TIMEOUT_SECONDS = get_env_int("VALHALLA_TIMEOUT_SECONDS", 15)
VALHALLA_ELEVATION_INTERVAL_M = get_env_int("VALHALLA_ELEVATION_INTERVAL_M", 30)

# Vereinfachte Verbrauchs-/Ladelogik gemäss Anforderung
BATTERY_CAPACITY_KWH = 75.0
AVG_CONSUMPTION_KWH_PER_100KM = 15.3
TARGET_RESERVE_KWH = BATTERY_CAPACITY_KWH * 0.10
CHARGE_TIME_BUFFER_FACTOR = 1.15

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


def _extract_leg_distances_km(trip_response: Dict[str, object]) -> List[float]:
    """Extrahiert leg-Distanzen in km (fuer Start->Lader und Lader->Ziel)."""
    trip = trip_response.get("trip")
    if not isinstance(trip, dict):
        return []
    legs = trip.get("legs")
    if not isinstance(legs, list):
        return []

    leg_lengths: List[float] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        summary = leg.get("summary")
        if isinstance(summary, dict) and "length" in summary:
            leg_lengths.append(float(summary["length"]))
            continue

        maneuvers = leg.get("maneuvers")
        if isinstance(maneuvers, list):
            length_sum = 0.0
            for maneuver in maneuvers:
                if isinstance(maneuver, dict):
                    length_sum += float(maneuver.get("length", 0.0))
            leg_lengths.append(length_sum)
    return leg_lengths


@app.post("/plan-route")
async def plan_route(request: RouteRequest) -> Dict[str, object]:
    try:
        start = [request.start_lon, request.start_lat]
        destination = [request.dest_lon, request.dest_lat]

        # 1) Initiale Route + Ladefenster
        route_data = get_valhalla_route(start, destination)
        decoded_shape = route_data.get("decoded_shape", [])
        segments = route_data.get("segments", [])
        if not isinstance(decoded_shape, list) or not isinstance(segments, list):
            raise ValueError("Route-Daten haben ein ungueltiges Format.")

        charging_window_coords, window_start_km, window_end_km = find_charging_window(
            decoded_shape=decoded_shape,
            segments=segments,
            battery_capacity_kwh=BATTERY_CAPACITY_KWH,
        )
        if window_start_km is None or window_end_km is None or len(charging_window_coords) < 2:
            raise ValueError("Kein valides Lade-Fenster (15%-5%) auf der Route gefunden.")

        # 2) Besten Lader im Ladefenster bewerten
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

        window_start_loc = _to_location_from_latlon(charging_window_coords[0], heading=start_heading)
        window_end_loc = _to_location_from_latlon(charging_window_coords[-1], heading=end_heading)
        baseline_response = _request_trip([window_start_loc, window_end_loc])
        _, baseline_time_min = _extract_summary(baseline_response)

        bounding_box = get_bounding_box(charging_window_coords)
        chargers = find_chargers(bounding_box)

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
        if not ranked:
            raise ValueError("Kein geeigneter Ladestopp im Lade-Fenster gefunden.")

        best_charger = ranked[0]
        best_lat = float(best_charger["latitude"])
        best_lon = float(best_charger["longitude"])
        best_power_kw = float(best_charger.get("max_power_kw", 0.0))
        best_operator = str(best_charger.get("operator", "Unbekannt"))

        # 3) Finale Route Start -> Lader -> Ziel
        final_locations = [
            _to_location(start),
            {"lat": best_lat, "lon": best_lon},
            _to_location(destination),
        ]
        final_trip = _request_trip(final_locations)
        total_distance_km, total_time_min = _extract_summary(final_trip)

        leg_distances_km = _extract_leg_distances_km(final_trip)
        if len(leg_distances_km) >= 2:
            distance_to_charger_km = leg_distances_km[0]
            distance_charger_to_destination_km = leg_distances_km[1]
        else:
            # Fallback auf Einzelrouten, falls die Legs unerwartet fehlen.
            to_charger = get_valhalla_route(start, [best_lon, best_lat])
            to_destination = get_valhalla_route([best_lon, best_lat], destination)
            distance_to_charger_km = float(to_charger["distance_km"])
            distance_charger_to_destination_km = float(to_destination["distance_km"])

        # 4) Einfache Lade-/Energieberechnung
        kwh_per_km = AVG_CONSUMPTION_KWH_PER_100KM / 100.0
        start_energy_kwh = BATTERY_CAPACITY_KWH
        energy_used_to_charger_kwh = kwh_per_km * distance_to_charger_km
        energy_at_charger_kwh = max(0.0, start_energy_kwh - energy_used_to_charger_kwh)

        energy_needed_for_rest_kwh = kwh_per_km * distance_charger_to_destination_km
        required_departure_energy_kwh = energy_needed_for_rest_kwh + TARGET_RESERVE_KWH
        target_departure_energy_kwh = min(BATTERY_CAPACITY_KWH, required_departure_energy_kwh)
        required_charge_kwh = max(0.0, target_departure_energy_kwh - energy_at_charger_kwh)
        energy_after_charging_kwh = energy_at_charger_kwh + required_charge_kwh
        energy_at_destination_kwh = max(0.0, energy_after_charging_kwh - energy_needed_for_rest_kwh)

        soc_at_charger_pct = (energy_at_charger_kwh / BATTERY_CAPACITY_KWH) * 100.0
        soc_after_charging_pct = (energy_after_charging_kwh / BATTERY_CAPACITY_KWH) * 100.0
        soc_at_destination_pct = (energy_at_destination_kwh / BATTERY_CAPACITY_KWH) * 100.0

        if best_power_kw > 0:
            charging_time_min = (required_charge_kwh / best_power_kw) * 60.0
            charging_time_min *= CHARGE_TIME_BUFFER_FACTOR
        else:
            charging_time_min = 0.0

        total_trip_time_min = total_time_min + charging_time_min

        # 5) Strukturierte Response bauen (summary/charger/geometry/navigation)
        trip_obj = final_trip.get("trip")
        legs = trip_obj.get("legs", []) if isinstance(trip_obj, dict) else []
        route_geometry: List[Dict[str, object]] = []
        navigation: List[Dict[str, object]] = []

        if isinstance(legs, list):
            for idx, leg in enumerate(legs):
                if not isinstance(leg, dict):
                    continue

                if idx == 0:
                    title = "Etappe 1: Fahrt zum Ladestopp"
                elif idx == 1:
                    title = "Etappe 2: Fahrt zum Ziel"
                else:
                    title = f"Etappe {idx + 1}"

                maneuvers = leg.get("maneuvers", [])
                if not isinstance(maneuvers, list):
                    continue

                encoded_shape = leg.get("shape", "")
                decoded_leg_shape = (
                    decode_polyline6(encoded_shape) if isinstance(encoded_shape, str) and encoded_shape else []
                )
                route_geometry.append(
                    {
                        "leg_index": idx,
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
                        "maneuvers": nav_maneuvers,
                    }
                )

        return {
            "summary": {
                "total_distance_km": round(total_distance_km, 2),
                "drive_time_min": round(total_time_min, 1),
                "charge_time_min": round(charging_time_min, 1),
                "total_trip_time_min": round(total_trip_time_min, 1),
                "soc_at_charger_pct": round(soc_at_charger_pct, 1),
                "soc_after_charge_pct": round(soc_after_charging_pct, 1),
                "soc_at_destination_pct": round(soc_at_destination_pct, 1),
            },
            "charger": {
                "operator": best_operator,
                "max_power_kw": round(best_power_kw, 1),
                "lat": best_lat,
                "lon": best_lon,
                "charged_kwh": round(required_charge_kwh, 2),
            },
            "route_geometry": route_geometry,
            "navigation": navigation,
        }
    except HTTPException:
        raise
    except (ValhallaRouteError, requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
