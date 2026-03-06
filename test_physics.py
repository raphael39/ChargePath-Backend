"""
Isolierter Test für die Valhalla-Routen-Segmente und das EV-Physikmodell.
"""

from valhalla_route import get_valhalla_route
from ev_physics import EVPhysicsModel

def run_test():
    print("Sende Anfrage an Valhalla...")
    
    # Eine kurze, aber topografisch spannende Route!
    # Von Zürich HB (flach/Tal) hoch zum Zürich Zoo (Zürichberg, ordentlich Steigung).
    # Das sind nur ca. 3,5 km, liefert uns aber ~110 Segmente zum Analysieren.
    start = {"lat": 47.3781, "lon": 8.5401}  # Zürich HB
    end = {"lat": 48.1402, "lon": 11.5583}   # München Hbf

    try:
        route_data = get_valhalla_route(start, end)
    except Exception as e:
        print(f"Fehler beim Routing: {e}")
        return

    segments = route_data.get("segments", [])
    
    print(f"Route gefunden! Gesamtdistanz: {route_data.get('distance_km', 0):.2f} km")
    print(f"Die Route wurde in {len(segments)} Segmente (à ~30m) unterteilt.\n")

    # Physik-Modell instanziieren (Standardwerte: 1800kg, Cw 0.23, etc.)
    physics = EVPhysicsModel()
    
    total_kwh = 0.0
    actual_distance_km = 0.0

    print("-" * 120)
    print(f"{'Seg':>3} | {'Länge':>6} | {'Höhe':>7} | {'Tempo':>9} | {'Verbrauch/100':>13} | {'Manöver'}")
    print("-" * 120)

    previous_speed = 0.0  # Wir starten aus dem Stand

    for i, seg in enumerate(segments):
        dist_km = seg.get("distance_km", 0.0)
        dur_sec = seg.get("duration_sec", 0.0)
        speed_kmh = seg.get("speed_kmh", 0.0)
        delta_h = seg.get("delta_h_m", 0.0)
        instruction = seg.get("instruction", "")

        if dist_km <= 0 or dur_sec <= 0:
            continue

        kwh = physics.calculate_energy_kwh(
            distance_km=dist_km, 
            speed_kmh=speed_kmh, 
            duration_sec=dur_sec, 
            delta_h_m=delta_h,
            initial_speed_kmh=previous_speed  # <-- NEU: Wir übergeben das alte Tempo!
        )
        total_kwh += kwh
        actual_distance_km += dist_km
        kwh_per_100km = (kwh / dist_km) * 100

        print(
            f"{i+1:03d} | "
            f"{dist_km*1000:5.0f}m | "
            f"{delta_h:+6.2f}m | "
            f"{speed_kmh:4.0f} km/h | "
            f"{kwh_per_100km:+9.2f} kWh | "
            f"{instruction[:60]}"
        )
        
        # Für den nächsten Durchlauf speichern wir das aktuelle Tempo als das "alte" Tempo
        previous_speed = speed_kmh

    print("-" * 120)
    print(f"GESAMTVERBRAUCH: {total_kwh:.3f} kWh für {actual_distance_km:.2f} km")
    if actual_distance_km > 0:
        print(f"DURCHSCHNITT:    {(total_kwh / actual_distance_km) * 100:.2f} kWh / 100 km")

if __name__ == "__main__":
    run_test()