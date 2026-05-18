"""Schnittstellen und Logik für Ladestationen (OCM) und Geometrie."""

import math
import requests
import re
from typing import Dict, List, Tuple, Optional

# Wir nutzen relative Pfade für deine neue Ordnerstruktur!
from app_config import get_env_str, get_env_int
from routing.valhalla_route import ValhallaRouteError

OCM_API_KEY = get_env_str("OCM_API_KEY")
OCM_URL = "https://api.openchargemap.io/v3/poi"

def parse_usage_cost(cost_str: str) -> float | None:
    "Versucht, aus einem unstrukturierten Text einen sauberen float wert zu extrahieren"
    if not cost_str: 
        return None
    
    cost_lower = cost_str.lower()

    if "free" in cost_lower or "kostenlos" in cost_lower or "gratis" in cost_lower:
        return 0.0
    
    match = re.search(r"(\d+[.,]\d+)", cost_str)
    if match:
        number_str = match.group(1).replace(",",".")
        try:
            return float(number_str)
        except ValueError:
            return None
        
    match_int = re.search(r"(\d+)", cost_str)
    if match_int:
        return float(match_int.group(1))
    
    return None

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Berechnet die echte physische Distanz zwischen zwei GPS-Punkten in Kilometern."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def find_charging_window(
    decoded_shape: List[List[float]],
    segments: List[Dict[str, object]],
    battery_capacity_kwh: float,
    current_soc: float,
    window_start_soc: float, # <-- NEU vom Frontend
    window_end_soc: float    # <-- NEU vom Frontend
) -> Tuple[List[List[float]], float, float]:
    """Findet das Ladefenster mit präziser Interpolation aus den Frontend-Settings."""
    
    current_energy_kwh = battery_capacity_kwh * current_soc
    
    # Ab wie viel VERBRAUCHTEN kWh geht das Fenster auf und zu?
    consumed_kwh_for_window_start = current_energy_kwh - (battery_capacity_kwh * window_start_soc)
    consumed_kwh_for_window_end = current_energy_kwh - (battery_capacity_kwh * window_end_soc)
    
    accumulated_kwh = 0.0
    accumulated_km = 0.0
    window_start_km = None
    window_end_km = None
    
    # 1. Präzise Kilometer-Marken durch Interpolation berechnen
    for seg in segments:
        seg_kwh = float(seg.get("segment_kwh", 0.0))
        dist_km = float(seg.get("distance_km", 0.0))
        
        # Check für den Start des Fensters
        if accumulated_kwh + seg_kwh >= consumed_kwh_for_window_start and window_start_km is None:
            if seg_kwh > 0:
                fraction = (consumed_kwh_for_window_start - accumulated_kwh) / seg_kwh
                window_start_km = accumulated_km + (dist_km * fraction)
            else:
                window_start_km = accumulated_km
                
        # Check für das Ende des Fensters
        if accumulated_kwh + seg_kwh >= consumed_kwh_for_window_end and window_end_km is None:
            if seg_kwh > 0:
                fraction = (consumed_kwh_for_window_end - accumulated_kwh) / seg_kwh
                window_end_km = accumulated_km + (dist_km * fraction)
            else:
                window_end_km = accumulated_km
            break 
            
        accumulated_kwh += seg_kwh
        accumulated_km += dist_km
            
    if window_start_km is not None and window_end_km is None:
        window_end_km = accumulated_km
        
    if window_start_km is None or window_end_km is None:
        return [], 0.0, 0.0

    # 2. Maßband anlegen
    window_coords = []
    current_shape_km = 0.0
    
    for i in range(len(decoded_shape) - 1):
        lat1, lon1 = decoded_shape[i]
        lat2, lon2 = decoded_shape[i+1]
        
        dist = _haversine_km(lat1, lon1, lat2, lon2) 
        
        if current_shape_km + dist >= window_start_km and current_shape_km <= window_end_km:
            window_coords.append([lat1, lon1])
            
        current_shape_km += dist
        
        if current_shape_km > window_end_km:
            window_coords.append([lat2, lon2])
            break

    if not window_coords and decoded_shape:
        safe_start = max(0, len(decoded_shape) - 10)
        window_coords = decoded_shape[safe_start:]

    return window_coords, window_start_km, window_end_km

def get_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    dlon = math.radians(lon2 - lon1)
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return int((math.degrees(math.atan2(x, y)) + 360) % 360)

def get_bounding_box(coords: List[List[float]], buffer_deg: float = 0.05) -> Tuple[float, float, float, float]:
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return min(lats) - buffer_deg, min(lons) - buffer_deg, max(lats) + buffer_deg, max(lons) + buffer_deg

def is_within_window_radius(lat: float, lon: float, window_coords: List[List[float]], radius_km: float = 3.0) -> bool:
    for w_lat, w_lon in window_coords:
        if _haversine_km(lat, lon, w_lat, w_lon) <= radius_km:
            return True
    return False

def find_chargers(bounding_box: Tuple[float, float, float, float]) -> List[Dict[str, object]]:
    min_lat, min_lon, max_lat, max_lon = bounding_box
    bbox_string = f"({max_lat},{min_lon}),({min_lat},{max_lon})"
    
    params = {
        "output": "json", 
        "maxresults": 100, 
        "compact": "false", 
        "verbose": "false",
        "boundingbox": bbox_string,
        "levelid": 3, 
        "minpowerkw": 50
    }
    
    headers = {"X-API-Key": OCM_API_KEY} if OCM_API_KEY else {}
    resp = requests.get(OCM_URL, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    
    chargers = []
    for item in resp.json():
        addr = item.get("AddressInfo", {})
        conn = item.get("Connections", [])
        max_kw = max((float(c.get("PowerKW") or 0) for c in conn), default=0.0)
        price_per_kwh = parse_usage_cost(item.get("UsageCost", ""))
        
        chargers.append({
            "id": item.get("ID"),
            "operator": item.get("OperatorInfo", {}).get("Title", "Unknown"),
            "latitude": addr.get("Latitude"),
            "longitude": addr.get("Longitude"),
            "max_power_kw": max_kw,
            "price_per_kwh": price_per_kwh
        })
    return chargers

def get_detour_min(charger_lat: float, charger_lon: float, window_start: Dict[str, object], window_end: Dict[str, object], baseline_time_min: float) -> float:
    charger_loc = {"lat": charger_lat, "lon": charger_lon}
    payload = {
        "locations": [window_start, charger_loc, window_end],
        "costing": "auto", "directions_options": {"units": "kilometers"}
    }
    try:
        valhalla_url = get_env_str("VALHALLA_ROUTE_URL", "http://localhost:8002/route")
        resp = requests.post(valhalla_url, json=payload, timeout=10)
        resp.raise_for_status()
        detour_time = float(resp.json().get("trip", {}).get("summary", {}).get("time", 0)) / 60.0
        return max(0.0, detour_time - baseline_time_min)
    except Exception:
        return 999.0