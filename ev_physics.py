"""Physikmodell fuer EV-Verbrauchsberechnung."""

from __future__ import annotations


class EVPhysicsModel:
    """Einfaches Physikmodell zur Verbrauchsschätzung für EV-Routing-Segmente."""

    def __init__(
        self,
        mass_kg: float = 1800.0,
        c_w: float = 0.23,
        area_m2: float = 2.22,
        rho_air: float = 1.225,
        c_r: float = 0.01,
        aux_power_kw: float = 1.5,
        recuperation_efficiency: float = 0.70,
    ) -> None:
        self.mass_kg = mass_kg
        self.c_w = c_w
        self.area_m2 = area_m2
        self.rho_air = rho_air
        self.c_r = c_r
        self.aux_power_kw = aux_power_kw
        self.recuperation_efficiency = recuperation_efficiency

    def calculate_energy_kwh(
        self,
        distance_km: float,
        speed_kmh: float,
        duration_sec: float,
        delta_h_m: float,
    ) -> float:
        """Berechnet den Energiebedarf (kWh) für ein Segment."""
        speed_mps = speed_kmh / 3.6

        f_luft = 0.5 * self.rho_air * self.c_w * self.area_m2 * (speed_mps**2)
        f_roll = self.mass_kg * 9.81 * self.c_r
        f_gesamt = f_luft + f_roll

        distance_m = distance_km * 1000.0
        work_luft_roll_joule = f_gesamt * distance_m

        work_steigung_joule = self.mass_kg * 9.81 * delta_h_m
        work_gesamt_joule = work_luft_roll_joule + work_steigung_joule

        drivetrain_efficiency = 0.9
        if work_gesamt_joule >= 0:
            traction_kwh = (work_gesamt_joule / 3_600_000.0) / drivetrain_efficiency
        else:
            traction_kwh = (work_gesamt_joule / 3_600_000.0) * self.recuperation_efficiency

        aux_energy_kwh = self.aux_power_kw * (duration_sec / 3600.0)
        return traction_kwh + aux_energy_kwh
