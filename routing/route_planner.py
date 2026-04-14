"""Der Manager: Orchestriert das gesamte EV-Routing."""

import requests
import folium
from typing import Dict, List, Sequence

from app_config import get_env_int, get_env_str
from models import RouteRequest
from vehicles import VehicleSpecs

# Interne Routing-Imports (gleicher Ordner)
from .routing_engine import compute_route_energy_kwh, extract_leg_elevation_stats, extract_leg_distance_time
from .valhalla_route import decode_polyline6, get_valhalla_route

# Externe Imports aus dem "chargers" Ordner
from chargers.charge_points import find_chargers, find_charging_window, get_bearing, get_bounding_box
from chargers.charger_ranking import get_best_charger

from physics.charging_physics import calculate_charging_time_min

# --- KONFIGURATION ---
VALHALLA_ROUTE_URL = get_env_str("VALHALLA_ROUTE_URL")
VALHALLA_TIMEOUT_SECONDS = get_env_int("VALHALLA_TIMEOUT_SECONDS", 15)

TARGET_RESERVE_PERCENT = 0.10
CHARGE_TIME_BUFFER_FACTOR = 1.15
MIN_CHARGE_DELTA_SOC = 0.40   # Mindestens 40% SOC pro Ladestopp laden
MAX_CHARGE_SOC = 0.80         # Nie über 80% laden
MAX_STOPS = 25

# --- HILFSFUNKTIONEN ---
def _to_location(coord_lon_lat: Sequence[float]) -> Dict[str, float]:
    return {"lon": float(coord_lon_lat[0]), "lat": float(coord_lon_lat[1])}

def _to_location_from_latlon(coord_lat_lon: Sequence[float], heading: int | None = None) -> Dict[str, object]:
    location: Dict[str, object] = {"lat": float(coord_lat_lon[0]), "lon": float(coord_lat_lon[1])}
    if heading is not None:
        location["heading"] = int(heading) % 360
    return location

def _request_baseline_trip(locations: List[Dict[str, object]]) -> float:
    payload = {
        "locations": locations,
        "costing": "auto",
        "directions_options": {"units": "kilometers"},
        "format": "json",
    }
    response = requests.post(VALHALLA_ROUTE_URL, json=payload, timeout=VALHALLA_TIMEOUT_SECONDS)
    response.raise_for_status()
    summary = response.json().get("trip", {}).get("summary", {})
    return float(summary.get("time", 0.0)) / 60.0

# --- HAUPT-ALGORITHMUS ---
def calculate_ev_route(request: RouteRequest, vehicle: VehicleSpecs) -> Dict[str, object]:
    """Berechnet die gesamte Route inkl. Ladestopps und finalem JSON."""
    start = [request.start_lon, request.start_lat]
    destination = [request.dest_lon, request.dest_lat]

    battery_capacity = vehicle.battery_capacity_kwh
    target_reserve_kwh = battery_capacity * TARGET_RESERVE_PERCENT

    current_start = start
    current_soc = request.initial_soc
    stops: List[Dict[str, float]] = []
    chargers_info: List[Dict[str, object]] = []
    remaining_leg_distance_km = 0.0
    remaining_leg_energy_needed_kwh = 0.0

    # --- 1. LADESCHLEIFE ---
    while True:
        route_data = get_valhalla_route(current_start, destination)
        distance_to_destination_km = float(route_data.get("distance_km", 0.0))
        
        energy_needed_to_dest_kwh, _ = compute_route_energy_kwh(route_data, vehicle, f"Reststrecke {len(stops) + 1}")
        available_energy_kwh = current_soc * battery_capacity

        if available_energy_kwh - energy_needed_to_dest_kwh >= target_reserve_kwh:
            remaining_leg_distance_km = distance_to_destination_km
            remaining_leg_energy_needed_kwh = energy_needed_to_dest_kwh
            break

        decoded_shape = route_data.get("decoded_shape", [])
        segments = route_data.get("segments", [])

        charging_window_coords, window_start_km, window_end_km = find_charging_window(
            decoded_shape=decoded_shape, segments=segments,
            battery_capacity_kwh=battery_capacity, current_soc=current_soc,
        )
        
        if not charging_window_coords:
            raise ValueError("Kein valides Lade-Fenster auf der Route gefunden.")

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
        baseline_time_min = _request_baseline_trip([window_start_loc, window_end_loc])

        bounding_box = get_bounding_box(charging_window_coords)
        chargers = find_chargers(bounding_box)

        # ==========================================
        # --- NEU: DIE DEBUG KARTE GENERIEREN ---
        # ==========================================
        import folium
        print(f"-> Zeichne Debug-Karte für Ladefenster {len(stops) + 1}...")
        
        # Karte zentrieren (auf den Anfang des Ladefensters)
        map_center = charging_window_coords[0] if charging_window_coords else current_start
        debug_map = folium.Map(location=map_center, zoom_start=10)

        # 1. Die gesamte befahrene Route (Dünne blaue Linie)
        folium.PolyLine(decoded_shape, color="blue", weight=2, opacity=0.5).add_to(debug_map)

        # 2. Das berechnete Ladefenster (Dicke rote Linie)
        if charging_window_coords:
            folium.PolyLine(charging_window_coords, color="red", weight=6, opacity=0.8).add_to(debug_map)

        # 3. Alle gefundenen Ladesäulen aus der Bounding Box (Grüne Marker)
        for c in chargers:
            c_lat = float(c.get("latitude", 0))
            c_lon = float(c.get("longitude", 0))
            c_kw = c.get("max_power_kw", 0)
            folium.Marker(
                location=[c_lat, c_lon],
                popup=f"{c.get('operator')} ({c_kw} kW)",
                icon=folium.Icon(color="green", icon="bolt", prefix="fa")
            ).add_to(debug_map)

        # HTML Datei im Hauptordner speichern
        debug_map.save(f"debug_map_stop_{len(stops) + 1}.html")
        print(f"-> Karte gespeichert als debug_map_stop_{len(stops) + 1}.html")
        # ==========================================

        # Hier geht dein normaler Code weiter:

        best_charger = get_best_charger(
            chargers=chargers, charging_window_coords=charging_window_coords,
            window_start_loc=window_start_loc, window_end_loc=window_end_loc,
            baseline_time_min=baseline_time_min, current_start=current_start,
            vehicle=vehicle, available_energy_kwh=available_energy_kwh,
            target_kwh=battery_capacity * MAX_CHARGE_SOC, user_price_weight=request.price_time_weight,
        )

        best_lat = float(best_charger["latitude"])
        best_lon = float(best_charger["longitude"])
        best_power_kw = float(best_charger.get("max_power_kw", 0.0))
        best_operator = str(best_charger.get("operator", "Unbekannt"))

        to_charger_route = get_valhalla_route(current_start, [best_lon, best_lat])
        energy_to_charger_kwh, _ = compute_route_energy_kwh(to_charger_route, vehicle, f"Zum Ladestopp {len(stops) + 1}")
        energy_at_arrival_kwh = max(0.0, available_energy_kwh - energy_to_charger_kwh)

        arrival_soc = energy_at_arrival_kwh / battery_capacity
        # Basis: Ankunfts-SOC + mindestens 40%
        base_target_soc = arrival_soc + MIN_CHARGE_DELTA_SOC
        # Bonus bei sehr niedrigem Akku (<20%): bis zu 15% extra, weil die unteren
        # SOC-Bereiche schneller laden und es sich zeitlich kaum bemerkbar macht
        low_soc_bonus = max(0.0, (0.20 - arrival_soc) / 0.20) * 0.15
        smart_target_soc = min(base_target_soc + low_soc_bonus, MAX_CHARGE_SOC)
        target_energy_after_charge_kwh = battery_capacity * smart_target_soc
        required_charge_kwh = max(0.0, target_energy_after_charge_kwh - energy_at_arrival_kwh)

        effective_charge_kw = min(best_power_kw, vehicle.max_charge_power_kw) if best_power_kw > 0 else 0

        # ECHTE LADEKURVE FÜR DAS FRONTEND NUTZEN
        if best_power_kw > 0:
            charging_time_min = calculate_charging_time_min(
                vehicle=vehicle,
                start_kwh=energy_at_arrival_kwh,
                target_kwh=target_energy_after_charge_kwh,
                station_max_kw=best_power_kw
            )
        else:
            charging_time_min = 0.0

        soc_at_arrival_pct = (energy_at_arrival_kwh / battery_capacity) * 100.0
        soc_after_charge_pct = (target_energy_after_charge_kwh / battery_capacity) * 100.0

        chargers_info.append({
            "operator": best_operator,
            "max_power_kw": round(best_power_kw, 1),
            "effective_charge_kw": round(effective_charge_kw, 1),
            "lat": best_lat,
            "lon": best_lon,
            "charged_kwh": round(required_charge_kwh, 2),
            "charge_time_min": round(charging_time_min, 1),
            "soc_at_arrival_pct": round(soc_at_arrival_pct, 1),
            "soc_after_charge_pct": round(soc_after_charge_pct, 1),
        })

        stops.append({"lat": best_lat, "lon": best_lon})
        current_start = [best_lon, best_lat]
        current_soc = smart_target_soc

        if len(stops) >= MAX_STOPS:
            raise ValueError("Maximale Anzahl Ladestopps überschritten.")

    # --- 2. FINALE ROUTE ZUSAMMENBAUEN ---
    final_locations: List[Dict[str, object]] = [_to_location(start)]
    final_locations.extend(stops)
    final_locations.append(_to_location(destination))

    payload = {
        "locations": final_locations,
        "costing": "auto",
        "directions_options": {"units": "kilometers", "elevation": True},
        "format": "json",
    }
    final_trip_resp = requests.post(VALHALLA_ROUTE_URL, json=payload, timeout=VALHALLA_TIMEOUT_SECONDS)
    final_trip_resp.raise_for_status()
    final_trip = final_trip_resp.json()

    trip_obj = final_trip.get("trip", {})
    summary = trip_obj.get("summary", {})
    total_distance_km = float(summary.get("length", 0.0))
    total_time_min = float(summary.get("time", 0.0)) / 60.0

    total_charge_time_min = sum(float(item.get("charge_time_min", 0.0)) for item in chargers_info)
    total_trip_time_min = total_time_min + total_charge_time_min

    energy_at_destination_kwh = max(0.0, (current_soc * battery_capacity) - remaining_leg_energy_needed_kwh)
    soc_at_destination_pct = (energy_at_destination_kwh / battery_capacity) * 100.0

    legs = trip_obj.get("legs", [])
    route_geometry, navigation, leg_metrics = [], [], []
    trip_energy_kwh = 0.0

    if isinstance(legs, list):
        waypoints_lon_lat: List[List[float]] = [start]
        waypoints_lon_lat.extend([[float(stop["lon"]), float(stop["lat"])] for stop in stops])
        waypoints_lon_lat.append(destination)

        leg_energy_kwh_map: Dict[int, float] = {}
        for idx in range(len(waypoints_lon_lat) - 1):
            leg_route_data = get_valhalla_route(waypoints_lon_lat[idx], waypoints_lon_lat[idx + 1])
            leg_energy_kwh, _ = compute_route_energy_kwh(leg_route_data, vehicle, f"Finales Leg {idx + 1}")
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

            distance_km, time_sec = extract_leg_distance_time(leg)
            elevation_stats = extract_leg_elevation_stats(leg)
            drive_time_min = time_sec / 60.0
            leg_energy_kwh = float(leg_energy_kwh_map.get(idx, 0.0))
            trip_energy_kwh += leg_energy_kwh
            energy_usage_kwh100 = (leg_energy_kwh / distance_km) * 100.0 if distance_km > 0 else 0.0

            encoded_shape = leg.get("shape", "")
            decoded_leg_shape = decode_polyline6(encoded_shape) if isinstance(encoded_shape, str) else []
            
            route_geometry.append({
                "leg_index": idx,
                "distance_km": round(distance_km, 2),
                "drive_time_min": round(drive_time_min, 2),
                "energy_usage_kwh100": round(energy_usage_kwh100, 2),
                **elevation_stats,
                "encoded_shape": encoded_shape if isinstance(encoded_shape, str) else "",
                "decoded_shape": decoded_leg_shape,
            })

            nav_maneuvers = [{"distance_km": round(float(m.get("length", 0.0)), 2), "instruction": str(m.get("instruction", ""))} for m in leg.get("maneuvers", []) if isinstance(m, dict)]

            navigation.append({
                "leg_index": idx, "title": title, "distance_km": round(distance_km, 2),
                "drive_time_min": round(drive_time_min, 2), "energy_usage_kwh100": round(energy_usage_kwh100, 2),
                **elevation_stats, "maneuvers": nav_maneuvers,
            })
            leg_metrics.append({
                "leg_index": idx, "title": title, "distance_km": round(distance_km, 2),
                "drive_time_min": round(drive_time_min, 2), "energy_usage_kwh100": round(energy_usage_kwh100, 2),
                **elevation_stats,
            })

    return {
        "summary": {
            "vehicle": vehicle.name,
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