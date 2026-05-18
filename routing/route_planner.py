"""Der Manager: Orchestriert das gesamte EV-Routing."""

import requests
import folium
from typing import Dict, List, Sequence

from app_config import get_env_int, get_env_str
from models import RouteRequest
from vehicles import VehicleSpecs

# Interne Routing-Imports (gleicher Ordner)
from .charging_config import CHARGING_CONFIG, compute_smart_target_soc
from .routing_engine import compute_route_energy_kwh, extract_leg_elevation_stats, extract_leg_distance_time
from .valhalla_route import decode_polyline6, get_valhalla_route

# Externe Imports aus dem "chargers" Ordner
from chargers.charge_points import find_charging_window, get_bearing
from chargers.chargeindex_client import find_chargers_along_route
from chargers.charger_ranking import rank_chargers

from physics.charging_physics import calculate_charging_time_min

# --- KONFIGURATION ---
VALHALLA_ROUTE_URL = get_env_str("VALHALLA_ROUTE_URL")
VALHALLA_TIMEOUT_SECONDS = get_env_int("VALHALLA_TIMEOUT_SECONDS", 15)

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
    target_reserve_kwh = battery_capacity * CHARGING_CONFIG.target_reserve_percent

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
            window_start_soc=request.window_start_soc,
            window_end_soc=request.window_end_soc,
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

        # Stationen entlang des Lade-Fensters via ChargeIndex API holen.
        # Server-seitig sortiert nach Fahrt-Reihenfolge, gefiltert auf HPC.
        chargers = find_chargers_along_route(charging_window_coords)

        # ==========================================
        # --- DEBUG KARTE: Stationen + Preis-Quelle visualisieren ---
        # Farb-Code:  gruen = echter ChargeIndex-Preis vorhanden
        #             orange = NULL -> Default-Fallback greift
        #             grau   = max_kw = 0 (wird vom Ranking eh ignoriert)
        #
        # Nur aktiv wenn DEBUG_MAPS_ENABLED=true in der .env steht.
        # In Production aus (Filesystem read-only durch systemd-Hardening,
        # und niemand schaut sich die HTML-Karten an).
        # Speicherort: DEBUG_MAPS_DIR (Default: aktuelles Verzeichnis lokal,
        # /opt/routezero/.cache/debug_maps in Production).
        # ==========================================
        if get_env_str("DEBUG_MAPS_ENABLED", "false").lower() in ("true", "1", "yes"):
            import os as _os
            import folium
            debug_maps_dir = get_env_str("DEBUG_MAPS_DIR", ".")
            _os.makedirs(debug_maps_dir, exist_ok=True)
            print(f"-> Zeichne Debug-Karte für Ladefenster {len(stops) + 1}...")

            map_center = charging_window_coords[0] if charging_window_coords else current_start
            debug_map = folium.Map(location=map_center, zoom_start=10)

            # 1. Gesamte Route (duenne blaue Linie)
            folium.PolyLine(decoded_shape, color="blue", weight=2, opacity=0.5).add_to(debug_map)

            # 2. Lade-Fenster (dicke rote Linie)
            if charging_window_coords:
                folium.PolyLine(charging_window_coords, color="red", weight=6, opacity=0.8).add_to(debug_map)

            # 3. Stationen + Preis-Label
            n_with_price = 0
            n_null_price = 0
            for c in chargers:
                c_lat = float(c.get("latitude") or 0)
                c_lon = float(c.get("longitude") or 0)
                c_kw = float(c.get("max_power_kw") or 0)
                c_op = str(c.get("operator_name") or c.get("operator") or "Unbekannt")
                c_city = str(c.get("city") or "")
                c_source_id = str(c.get("source_id") or "")
                api_price = c.get("price_per_kwh")

                # Preis-Status bestimmen
                if api_price is not None and float(api_price) > 0:
                    price_value = float(api_price)
                    price_str = f"{price_value:.2f} €"
                    marker_color = "green"
                    source_str = "ChargeIndex live"
                    n_with_price += 1
                elif c_kw <= 0:
                    price_value = None
                    price_str = "—"
                    marker_color = "lightgray"
                    source_str = "max_kw = 0, wird ignoriert"
                else:
                    price_value = None
                    price_str = "NULL"
                    marker_color = "orange"
                    source_str = "kein API-Preis → Default-Fallback"
                    n_null_price += 1

                # Popup mit vollen Details
                popup_html = (
                    f"<b>{c_op}</b><br/>"
                    f"{c_kw:.0f} kW · {c_city}<br/>"
                    f"Preis: {price_str}<br/>"
                    f"Quelle: {source_str}<br/>"
                    f"<small>{c_source_id}</small>"
                )
                folium.Marker(
                    location=[c_lat, c_lon],
                    popup=folium.Popup(popup_html, max_width=320),
                    icon=folium.Icon(color=marker_color, icon="bolt", prefix="fa"),
                ).add_to(debug_map)

                # Sichtbares Preis-Label neben dem Pin (DivIcon)
                label_bg = "#16a34a" if marker_color == "green" else (
                    "#ea580c" if marker_color == "orange" else "#94a3b8"
                )
                label_html = (
                    f'<div style="background:{label_bg};color:white;'
                    f'padding:2px 6px;border-radius:4px;font-size:11px;'
                    f'font-family:sans-serif;font-weight:600;'
                    f'white-space:nowrap;border:1px solid rgba(0,0,0,0.2);">'
                    f'{price_str} · {c_kw:.0f}kW'
                    f'</div>'
                )
                folium.Marker(
                    location=[c_lat, c_lon],
                    icon=folium.DivIcon(
                        icon_size=(120, 18),
                        icon_anchor=(-12, 6),  # Label rechts neben dem Pin
                        html=label_html,
                    ),
                ).add_to(debug_map)

            # Mini-Legende oben links
            legend_html = (
                '<div style="position:fixed;top:10px;left:60px;z-index:9999;'
                'background:white;padding:8px 10px;border:1px solid #ccc;'
                'border-radius:6px;font:12px sans-serif;box-shadow:0 1px 4px rgba(0,0,0,0.15)">'
                f'<b>Ladefenster {len(stops) + 1}</b><br/>'
                f'<span style="color:#16a34a">●</span> ChargeIndex live: {n_with_price}<br/>'
                f'<span style="color:#ea580c">●</span> NULL → Fallback: {n_null_price}<br/>'
                f'Stationen gesamt: {len(chargers)}'
                '</div>'
            )
            debug_map.get_root().html.add_child(folium.Element(legend_html))

            debug_map_path = _os.path.join(debug_maps_dir, f"debug_map_stop_{len(stops) + 1}.html")
            debug_map.save(debug_map_path)
            print(
                f"-> Karte gespeichert als {debug_map_path} "
                f"(gruen: {n_with_price}, orange/NULL: {n_null_price}, total: {len(chargers)})"
            )
        # ==========================================

        # Hier geht dein normaler Code weiter:

        ranked = rank_chargers(
            chargers=chargers, charging_window_coords=charging_window_coords,
            window_start_loc=window_start_loc, window_end_loc=window_end_loc,
            baseline_time_min=baseline_time_min, current_start=current_start,
            vehicle=vehicle, available_energy_kwh=available_energy_kwh,
            target_kwh=battery_capacity * CHARGING_CONFIG.max_charge_soc,
            user_price_weight=request.price_time_weight,
            window_start_soc=request.window_start_soc,
        )
        best_charger = ranked[0]
        ranked_top_5 = ranked[:5]

        best_lat = float(best_charger["latitude"])
        best_lon = float(best_charger["longitude"])
        best_power_kw = float(best_charger.get("max_power_kw", 0.0))
        best_operator = str(best_charger.get("operator_name") or best_charger.get("operator") or "Unbekannt")
        detour_min = float(best_charger.get("detour_min", 0.0))
        estimated_cost = float(best_charger.get("estimated_cost", 0.0))
        price_per_kwh_used = float(best_charger.get("price_per_kwh_used", 0.0))
        price_source = str(best_charger.get("price_source") or "default_fallback")

        to_charger_route = get_valhalla_route(current_start, [best_lon, best_lat])
        energy_to_charger_kwh, _ = compute_route_energy_kwh(to_charger_route, vehicle, f"Zum Ladestopp {len(stops) + 1}")
        energy_at_arrival_kwh = max(0.0, available_energy_kwh - energy_to_charger_kwh)

        arrival_soc = energy_at_arrival_kwh / battery_capacity
        smart_target_soc = compute_smart_target_soc(arrival_soc)
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

        # Top 5 als kompakte Alternativ-Liste fuer die Mobile App.
        # Reiner Pass-Through aus dem Ranking — keine Neuberechnung mehr im
        # Planner. Die Smart-Target-Logik wird pro Charger bereits in
        # rank_chargers angewendet, d.h. ``soc_after_charge_pct``,
        # ``kwh_to_charge``, ``charge_time_min`` und ``estimated_cost``
        # spiegeln exakt das wider, was ins Ranking eingeflossen ist.
        # Print-Output und API-Antwort stimmen damit ueberein.
        top_5_alternatives: List[Dict[str, object]] = []
        for idx, alt in enumerate(ranked_top_5):
            top_5_alternatives.append({
                "rank": idx + 1,
                "is_chosen": idx == 0,
                "operator_name": str(alt.get("operator_name") or alt.get("operator") or "Unbekannt"),
                "max_power_kw": round(float(alt.get("max_power_kw") or 0.0), 1),
                "lat": float(alt.get("latitude") or 0.0),
                "lon": float(alt.get("longitude") or 0.0),
                "city": alt.get("city"),
                "source_id": alt.get("source_id"),
                "detour_min": round(float(alt.get("detour_min") or 0.0), 1),
                "charge_time_min": round(float(alt.get("charging_time_min") or 0.0), 1),
                "kwh_to_charge": round(float(alt.get("kwh_to_charge") or 0.0), 2),
                "soc_at_arrival_pct": float(alt.get("soc_at_arrival_pct") or 0.0),
                "soc_after_charge_pct": float(alt.get("soc_after_charge_pct") or 0.0),
                "estimated_cost": round(float(alt.get("estimated_cost") or 0.0), 2),
                "price_per_kwh_used": round(float(alt.get("price_per_kwh_used") or 0.0), 2),
                "price_source": str(alt.get("price_source") or "default_fallback"),
                "score": round(float(alt.get("score") or 0.0), 3),
            })

        chargers_info.append({
            "operator": best_operator,
            "operator_name": best_operator,
            "max_power_kw": round(best_power_kw, 1),
            "effective_charge_kw": round(effective_charge_kw, 1),
            "lat": best_lat,
            "lon": best_lon,
            "source_id": best_charger.get("source_id"),
            "charged_kwh": round(required_charge_kwh, 2),
            "charge_time_min": round(charging_time_min, 1),
            "soc_at_arrival_pct": round(soc_at_arrival_pct, 1),
            "soc_after_charge_pct": round(soc_after_charge_pct, 1),
            "detour_min": round(detour_min, 1),
            "estimated_cost": round(estimated_cost, 2),
            "price_per_kwh_used": round(price_per_kwh_used, 2),
            "price_source": price_source,
            "top_5_alternatives": top_5_alternatives,
        })

        stops.append({"lat": best_lat, "lon": best_lon})
        current_start = [best_lon, best_lat]
        current_soc = smart_target_soc

        if len(stops) >= CHARGING_CONFIG.max_stops:
            raise ValueError("Maximale Anzahl Ladestopps überschritten.")

    # --- 2. FINALE ROUTE ZUSAMMENBAUEN ---
    final_locations: List[Dict[str, object]] = [_to_location(start)]
    final_locations.extend(stops)
    final_locations.append(_to_location(destination))

    payload = {
        "locations": final_locations,
        "costing": "auto",
        # elevation_interval auf Root-Level (Valhalla ignoriert es in directions_options)
        "elevation_interval": 30,
        "directions_options": {"units": "kilometers"},
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