"""Physikmodell fuer EV-Verbrauchsberechnung."""

from __future__ import annotations
from vehicles import VehicleSpecs


class EVPhysicsModel:
    """Einfaches Physikmodell zur Verbrauchsschätzung für EV-Routing-Segmente."""

    def __init__(
        self,
        vehicle: VehicleSpecs,
        rho_air: float = 1.225,  # Nur noch Umweltfaktoren hier!
    ) -> None:
        self.vehicle = vehicle
        self.rho_air = rho_air

    def calculate_energy_kwh(
        self,
        distance_km: float,
        speed_kmh: float,
        duration_sec: float,
        delta_h_m: float,
        initial_speed_kmh: float = 0.0,
    ) -> float:
        """Berechnet den Energiebedarf (kWh) für ein Segment."""
        speed_mps = speed_kmh / 3.6
        initial_speed_mps = initial_speed_kmh / 3.6

        # 1. Luft- und Rollwiderstand
        f_luft = 0.5 * self.rho_air * self.vehicle.drag_coefficient_cw * self.vehicle.frontal_area_m2 * (speed_mps**2)
        f_roll = self.vehicle.mass_kg * 9.81 * self.vehicle.rolling_resistance_cr
        f_gesamt = f_luft + f_roll

        distance_m = distance_km * 1000.0
        work_luft_roll_joule = f_gesamt * distance_m

        # 2. Potenzielle Energie (Höhe)
        work_steigung_joule = self.vehicle.mass_kg * 9.81 * delta_h_m
        
        # 3. Kinetische Energie (Beschleunigung/Verzögerung)
        work_kin_joule = 0.5 * self.vehicle.mass_kg * (speed_mps**2 - initial_speed_mps**2)

        # 4. Gesamtarbeit summieren
        work_gesamt_joule = work_luft_roll_joule + work_steigung_joule + work_kin_joule

        # 5. Antriebsstrang-Effizienz anwenden
        if work_gesamt_joule >= 0:
            # <-- GEÄNDERT: Greift jetzt auf self.vehicle zu!
            traction_kwh = (work_gesamt_joule / 3_600_000.0) / self.vehicle.drivetrain_efficiency
        else:
            traction_kwh = (work_gesamt_joule / 3_600_000.0) * self.vehicle.recuperation_efficiency

        # 6. Nebenverbraucher (Klima, Bordelektronik)
        aux_energy_kwh = self.vehicle.aux_power_kw * (duration_sec / 3600.0)
        
        return traction_kwh + aux_energy_kwh