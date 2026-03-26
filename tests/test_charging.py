"""Tests für die neue 10%-Block Ladekurven-Logik."""

from physics.charging_physics import calculate_charging_time_min
from vehicles import get_vehicle

def test_tesla_charging_curve_10pct_blocks():
    """Beweist, dass 40 kWh von 10-60% viel schneller laden als von 50-100%."""
    
    tesla = get_vehicle("tesla_model_y_lr")
    station_kw = 300.0 # 300 kW Hypercharger
    
    # 40 kWh entsprechen beim Model Y (78.1 kWh) ungefähr 51% Akku.
    
    # Szenario A: Der "Sweetspot" (Start bei 10% SoC)
    start_kwh_A = tesla.battery_capacity_kwh * 0.10
    time_A = calculate_charging_time_min(tesla, start_kwh_A, start_kwh_A + 40.0, station_kw)
    
    # Szenario B: Der Stau-Verursacher (Start bei 49% SoC)
    start_kwh_B = tesla.battery_capacity_kwh * 0.49
    time_B = calculate_charging_time_min(tesla, start_kwh_B, start_kwh_B + 40.0, station_kw)
    
    print("\n\n--- ⚡ VIRTUELLER LADE-PRÜFSTAND (10%-Blöcke) ---")
    print(f"Szenario A (10% -> 61%): {time_A:.1f} Minuten")
    print(f"Szenario B (49% -> 100%): {time_B:.1f} Minuten")
    print("------------------------------------------------\n")
    
    # Szenario B muss deutlich länger dauern!
    assert time_B > time_A + 15.0