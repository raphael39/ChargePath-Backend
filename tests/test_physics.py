"""Tests für die physikalische Berechnung."""

from physics.ev_physics import EVPhysicsModel
from vehicles import get_vehicle, VEHICLE_DB

def test_physics_energy_calculation():
    """Prüft, ob die Formel korrekte kWh ausspuckt (Level 1 Test)."""
    vehicle = get_vehicle("generic_ev")
    physics = EVPhysicsModel(vehicle=vehicle)
    energy = physics.calculate_energy_kwh(
        distance_km=10.0,
        speed_kmh=100.0,
        duration_sec=360.0,
        delta_h_m=0.0
    )
    assert energy > 1.0
    assert energy < 3.0


def test_comprehensive_vehicle_physics():
    """ Gibt detaillierten Referenzverbrauch in Wh/km für Geschwindigkeit und Steigung aus."""
    
    # Teil 1 Parameter
    speeds_kmh = [30, 50, 80, 100, 110, 120, 130, 150]
    
    # Teil 2 Parameter (Matrix)
    hill_speeds_kmh = [50, 80, 110]
    gradients_pct = [-4, -2, 0, 2, 4]

    for vehicle_id, vehicle in VEHICLE_DB.items():
        physics = EVPhysicsModel(vehicle=vehicle)

        # --- AUTODETAILS AUSGEBEN ---
        print(f"\n\n{'='*105}")
        print(f"🚗 DER ULTIMATIVE PRÜFSTAND: {vehicle.name}")
        print(f"{'='*105}")
        print(f"Gewicht:         {vehicle.mass_kg} kg")
        print(f"cW-Wert:         {vehicle.drag_coefficient_cw}")
        print(f"Stirnfläche:     {vehicle.frontal_area_m2} m²")
        print(f"Rollwiderstand:  {vehicle.rolling_resistance_cr}")
        print(f"Nebenverbrauch:  {vehicle.aux_power_kw} kW (Klima, Bordcomputer)")
        print(f"Antriebseffiz.:  {vehicle.drivetrain_efficiency * 100} %")
        print(f"Rekuperation:    {vehicle.recuperation_efficiency * 100} %")
        print(f"{'-'*105}")

        # ==========================================
        # TEIL 1: FLACHE STRECKE (Geschwindigkeiten)
        # ==========================================
        print(f"🏁 TEIL 1: FLACHE STRECKE (0 % Steigung)")
        print(f"{'-'*105}")
        print(f"{'Tempo':<8} | {'Gesamtverbrauch':<17} | {'Zusammensetzung pro Kilometer (in Wattstunden)':<45}")
        print(f"{'-'*105}")

        previous_wh_per_km = 0.0

        for speed in speeds_kmh:
            duration_sec = (1.0 / speed) * 3600.0
            
            energy_kwh = physics.calculate_energy_kwh(
                distance_km=1.0, speed_kmh=speed, duration_sec=duration_sec, 
                delta_h_m=0.0, initial_speed_kmh=speed
            )
            
            wh_per_km_total = energy_kwh * 1000.0
            kwh_per_100km = wh_per_km_total / 10.0

            # Breakdown
            speed_mps = speed / 3.6
            gravity = 9.81
            rho_air = getattr(physics, 'rho_air', 1.225) 
            eff = vehicle.drivetrain_efficiency

            f_luft = 0.5 * rho_air * vehicle.drag_coefficient_cw * vehicle.frontal_area_m2 * (speed_mps**2)
            wind_wh_km = ((f_luft * 1000.0) / 3_600_000.0) / eff * 1000.0

            f_roll = vehicle.mass_kg * gravity * vehicle.rolling_resistance_cr
            roll_wh_km = ((f_roll * 1000.0) / 3_600_000.0) / eff * 1000.0

            aux_wh_km = (vehicle.aux_power_kw * (duration_sec / 3600.0)) * 1000.0

            print(f"{speed:>3} km/h | {kwh_per_100km:>7.1f} kWh/100km | = Wind: {wind_wh_km:>5.1f} Wh + Roll: {roll_wh_km:>4.1f} Wh + Aux: {aux_wh_km:>4.1f} Wh")

            if previous_wh_per_km > 0 and speed >= 80:
                assert wh_per_km_total > previous_wh_per_km, f"Physikfehler bei {vehicle.name}"
            previous_wh_per_km = wh_per_km_total


        # ==========================================
        # TEIL 2: BERGE & GEFÄLLE (Matrix)
        # ==========================================
        print(f"\n⛰️  TEIL 2: BERGE & GEFÄLLE (Matrix)")
        print(f"{'-'*105}")
        print(f"{'Tempo':<8} | {'Steigung':<8} | {'Gesamtverbrauch':<17} | {'Zusammensetzung pro Kilometer (in Wattstunden)':<45}")
        print(f"{'-'*105}")

        for hill_speed in hill_speeds_kmh:
            for grad in gradients_pct:
                delta_h_m = (grad / 100.0) * 1000.0 
                duration_sec = (1.0 / hill_speed) * 3600.0
                
                energy_kwh = physics.calculate_energy_kwh(
                    distance_km=1.0, speed_kmh=hill_speed, duration_sec=duration_sec, 
                    delta_h_m=delta_h_m, initial_speed_kmh=hill_speed
                )
                
                wh_per_km_total = energy_kwh * 1000.0
                kwh_per_100km = wh_per_km_total / 10.0

                # Breakdown
                speed_mps = hill_speed / 3.6
                gravity = 9.81
                rho_air = getattr(physics, 'rho_air', 1.225)
                eff = vehicle.drivetrain_efficiency

                f_luft = 0.5 * rho_air * vehicle.drag_coefficient_cw * vehicle.frontal_area_m2 * (speed_mps**2)
                wind_wh_km = ((f_luft * 1000.0) / 3_600_000.0) / eff * 1000.0

                f_roll = vehicle.mass_kg * gravity * vehicle.rolling_resistance_cr
                roll_wh_km = ((f_roll * 1000.0) / 3_600_000.0) / eff * 1000.0

                aux_wh_km = (vehicle.aux_power_kw * (duration_sec / 3600.0)) * 1000.0

                # Höhenenergie
                e_pot_joule = vehicle.mass_kg * gravity * delta_h_m
                
                if grad >= 0:
                    hoehe_wh_km = (e_pot_joule / 3600.0) / eff
                else:
                    hoehe_wh_km = (e_pot_joule / 3600.0) * vehicle.recuperation_efficiency

                print(f"{hill_speed:>3} km/h | {grad:>2} %     | {kwh_per_100km:>7.1f} kWh/100km | = Wind: {wind_wh_km:>4.1f} Wh + Roll: {roll_wh_km:>4.1f} Wh + Aux: {aux_wh_km:>4.1f} Wh + Höhe: {hoehe_wh_km:>6.1f} Wh")
            
            # Ein kleiner Trennstrich nach jeder Tempogruppe für perfekte Lesbarkeit
            print(f"{'-'*105}")

        print(f"{'='*105}\n")