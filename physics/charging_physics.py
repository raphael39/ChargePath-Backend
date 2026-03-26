"""Physikalische Berechnung von Ladezeiten mit dem 10-Prozent-Blockmodell."""

from vehicles import VehicleSpecs

def calculate_charging_time_min(
    vehicle: VehicleSpecs, 
    start_kwh: float, 
    target_kwh: float, 
    station_max_kw: float
) -> float:
    """
    Berechnet die Ladezeit, indem die Ladung in 10%-Blöcke unterteilt wird.
    """
    if target_kwh <= start_kwh:
        return 0.0

    cap = vehicle.battery_capacity_kwh
    
    # In Prozent umrechnen (0.0 bis 1.0)
    start_soc = max(0.0, start_kwh / cap)
    target_soc = min(1.0, target_kwh / cap)

    total_time_hours = 0.0

    # Wir iterieren durch die 10 Blöcke (i geht von 0 bis 9)
    for i, block_avg_kw in enumerate(vehicle.charging_curve_10pct):
        block_start_soc = i * 0.10
        block_end_soc = (i + 1) * 0.10
        
        # Sind wir mit unserer Ladung überhaupt in diesem Block unterwegs?
        if target_soc <= block_start_soc or start_soc >= block_end_soc:
            continue
            
        # Wie viel Prozentpunkte laden wir IN DIESEM Block?
        overlap_start = max(start_soc, block_start_soc)
        overlap_end = min(target_soc, block_end_soc)
        
        soc_charged_in_block = overlap_end - overlap_start
        kwh_in_block = soc_charged_in_block * cap
        
        # Flaschenhals: Die Säule kann nicht mehr liefern als sie hat!
        effective_kw = min(block_avg_kw, station_max_kw)
        
        # Zeit = Energie / Leistung
        time_in_block_hours = kwh_in_block / effective_kw
        total_time_hours += time_in_block_hours

    return total_time_hours * 60.0