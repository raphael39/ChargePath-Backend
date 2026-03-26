"""Logik für das Bewerten und Filtern von Ladestationen."""

import math
from typing import Dict, List

from app_config import get_env_float
from chargers.charge_points import is_within_window_radius, get_detour_min
from physics.charging_physics import calculate_charging_time_min
from vehicles import VehicleSpecs

def get_best_charger(
    chargers: List[Dict[str, object]],
    charging_window_coords: List[List[float]],
    window_start_loc: Dict[str, object],
    window_end_loc: Dict[str, object],
    baseline_time_min: float,
    current_start: List[float],
    vehicle: VehicleSpecs,        
    available_energy_kwh: float,  
    target_kwh: float,   
    user_price_weight: float | None = None,         
) -> Dict[str, object]:
    """
    Sucht den besten Lader basierend auf dem geringsten 'Pain-Score per kWh'.
    Berechnet die Ankunfts-Energie dynamisch ab dem Start des Ladefensters.
    """
    ranked: List[Dict[str, object]] = []
    
    # 1. Lade unsere Slider-Konfigurationen aus der .env
    soc_window_start = get_env_float("SOC_WINDOW_START", 0.20)
    if user_price_weight is not None:
        price_weight_factor = float(user_price_weight)
    else:
        # Fallback auf die .env, falls die App nichts geschickt hat
        price_weight_factor = get_env_float("PRICE_TIME_WEIGHT_FACTOR", 3.0)
    
    for charger in chargers:
        lat = charger.get("latitude")
        lon = charger.get("longitude")
        if lat is None or lon is None:
            continue

        charger_lat = float(lat)
        charger_lon = float(lon)
        
        # 1. PING-PONG SCHUTZ
        if abs(charger_lat - current_start[1]) < 0.005 and abs(charger_lon - current_start[0]) < 0.005:
            continue

        # 2. RADIUS FILTER
        if not is_within_window_radius(charger_lat, charger_lon, charging_window_coords):
            continue

        # ==========================================
        # HOCHPRÄZISE AKKU-SCHÄTZUNG (Ab Ladefenster-Start)
        # ==========================================
        energy_at_window_start = vehicle.battery_capacity_kwh * soc_window_start
        
        # Distanz vom Start des Ladefensters zur Ladesäule (Satz des Pythagoras)
        dx = (charger_lon - float(window_start_loc["lon"])) * 71.5
        dy = (charger_lat - float(window_start_loc["lat"])) * 111.3
        
        estimated_dist_km = math.sqrt(dx**2 + dy**2) * 1.3
        estimated_energy_used = estimated_dist_km * 0.18 # Durchschnittsverbrauch für das kurze Stück
        
        arrival_kwh = max(0.0, energy_at_window_start - estimated_energy_used)
        kwh_to_charge = max(0.0, target_kwh - arrival_kwh)
        # ==========================================

        # 3. UMWEG BERECHNEN (Exakt via Valhalla)
        detour_min = get_detour_min(
            charger_lat=charger_lat,
            charger_lon=charger_lon,
            window_start=window_start_loc,
            window_end=window_end_loc,
            baseline_time_min=baseline_time_min,
        )

        # 4. PREIS BERECHNEN
        price_per_kwh = float(charger.get("price_per_kwh") or 0.55)
        estimated_cost_euro = price_per_kwh * kwh_to_charge
        
        # 5. LADEZEIT BERECHNEN
        max_kw = float(charger.get("max_power_kw", 0.0))
        if max_kw <= 0.0:
            continue
            
        charging_time_min = calculate_charging_time_min(
            vehicle=vehicle,
            start_kwh=arrival_kwh,
            target_kwh=target_kwh,
            station_max_kw=max_kw
        )
        
        # ==========================================
        # DIE EFFIZIENZ-LOGIK (Pain per kWh)
        # ==========================================
        time_pain_min = detour_min + charging_time_min
        safe_kwh_to_charge = max(1.0, kwh_to_charge) # Schutz vor Division durch Null
        
        # Wie viel Zeit kostet 1 kWh?
        time_pain_per_kwh = time_pain_min / safe_kwh_to_charge
        
        # Wie viel Geld kostet 1 kWh? (Gewichtet mit unserem Faktor aus der .env)
        cost_pain_per_kwh = price_per_kwh * price_weight_factor
        
        # Finaler Effizienz-Score (Je kleiner, desto besser!)
        score = time_pain_per_kwh + cost_pain_per_kwh
        # ==========================================

        # Werte speichern
        enriched = dict(charger)
        enriched["detour_min"] = detour_min
        enriched["charging_time_min"] = charging_time_min
        enriched["estimated_cost"] = estimated_cost_euro
        enriched["kwh_to_charge"] = kwh_to_charge
        enriched["arrival_kwh_est"] = arrival_kwh
        enriched["score"] = score
        ranked.append(enriched)

    # Sortieren: Der kleinste "Schmerz pro kWh" gewinnt!
    ranked.sort(key=lambda item: float(item.get("score", float('inf'))))
    
    if not ranked:
        raise ValueError("Kein geeigneter Ladestopp im Lade-Fenster gefunden.")

    # --- TERMINAL-AUSGABE ---
    print(f"\n\n{'='*95}")
    print(f"🏆 TOP LADESÄULEN-RANKING (Gewichtung: 1€ = {price_weight_factor} Min)")
    print(f"{'='*95}")
    
    top_5 = ranked[:5]
    for i, charger in enumerate(top_5, 1):
        operator = charger.get("operator_name", "Unbekannt")
        if operator == "Unbekannt":
            operator = charger.get("operator", "Unbekannt")
            
        kw = charger.get("max_power_kw", 0)
        score = charger.get("score", 0.0)
        detour = charger.get("detour_min", 0.0)
        charge_time = charger.get("charging_time_min", 0.0)
        cost = charger.get("estimated_cost", 0.0)
        price_kwh = float(charger.get("price_per_kwh") or 0.55)
        kwh_need = charger.get("kwh_to_charge", 0.0)
        arr_kwh = charger.get("arrival_kwh_est", 0.0)
        arr_pct = (arr_kwh / vehicle.battery_capacity_kwh) * 100
        
        medal = "🥇 " if i == 1 else "   "
        
        print(f"{medal}{i}. {operator} ({kw} kW) | Ankunft mit {arr_pct:.1f}% Akku")
        print(f"      Effizienz-Score: {score:.2f} Pain/kWh (Bedarf: {kwh_need:.1f} kWh)")
        print(f"      Details:         Ladezeit {charge_time:.1f} Min | Umweg {detour:.1f} Min | Preis {cost:.2f} € ({price_kwh:.2f} €/kWh)")
        print(f"{'-'*95}")
    print("\n")

    return ranked[0]