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
        elevation_gain_m: float | None = None,
        elevation_loss_m: float | None = None,
    ) -> float:
        """Berechnet den Energiebedarf (kWh) fuer ein Segment.

        Wenn ``elevation_gain_m`` und ``elevation_loss_m`` separat
        uebergeben werden, wird das Hoehenprofil exakt aufgeschluesselt:
        Aufstiegs-Energie wird mit ``drivetrain_efficiency`` aufgenommen,
        Abstiegs-Energie mit ``recuperation_efficiency`` rekuperiert.
        Das ist die korrekte Behandlung wenn Valhalla lange Maneuvers
        liefert, in denen sich Auf-und-Ab abwechseln.

        Fallback (alte API): nur ``delta_h_m`` als Netto-Differenz —
        das unterschaetzt Bergrouten systematisch.
        """
        speed_mps = speed_kmh / 3.6
        initial_speed_mps = initial_speed_kmh / 3.6
        mass = self.vehicle.mass_kg
        drivetrain_eff = self.vehicle.drivetrain_efficiency
        recup_eff = self.vehicle.recuperation_efficiency

        # 1. Luft- und Rollwiderstand (immer positiv -> Energieverbrauch)
        f_luft = 0.5 * self.rho_air * self.vehicle.drag_coefficient_cw \
            * self.vehicle.frontal_area_m2 * (speed_mps ** 2)
        f_roll = mass * 9.81 * self.vehicle.rolling_resistance_cr
        f_gesamt = f_luft + f_roll
        distance_m = distance_km * 1000.0
        work_luft_roll_joule = f_gesamt * distance_m

        # 2. Potentielle Energie (Hoehe).
        # Wenn Gain/Loss separat vorliegen, beide Komponenten mit den
        # richtigen Effizienzen behandeln. Sonst Netto-Approximation.
        if elevation_gain_m is not None and elevation_loss_m is not None:
            work_climb_joule = mass * 9.81 * elevation_gain_m       # aufgewendet
            work_descent_joule = mass * 9.81 * elevation_loss_m     # zurueckgewonnen
            # Aufstieg: durch drivetrain dividiert (Energie aus Batterie -> kinetisch+potentiell)
            # Abstieg: × recup_eff (potentielle -> elektrisch zurueck)
            potential_kwh = (work_climb_joule / 3_600_000.0) / drivetrain_eff \
                          - (work_descent_joule / 3_600_000.0) * recup_eff
        else:
            # Legacy: Netto-Approximation (unterschaetzt Bergrouten)
            work_steigung_joule = mass * 9.81 * delta_h_m
            if work_steigung_joule >= 0:
                potential_kwh = (work_steigung_joule / 3_600_000.0) / drivetrain_eff
            else:
                potential_kwh = (work_steigung_joule / 3_600_000.0) * recup_eff

        # 3. Kinetische Energie (Beschleunigung / Verzoegerung)
        work_kin_joule = 0.5 * mass * (speed_mps ** 2 - initial_speed_mps ** 2)
        if work_kin_joule >= 0:
            kin_kwh = (work_kin_joule / 3_600_000.0) / drivetrain_eff
        else:
            kin_kwh = (work_kin_joule / 3_600_000.0) * recup_eff

        # 4. Reibung -> immer durch drivetrain
        friction_kwh = (work_luft_roll_joule / 3_600_000.0) / drivetrain_eff

        # 5. Nebenverbraucher (Klima, Bordelektronik)
        aux_energy_kwh = self.vehicle.aux_power_kw * (duration_sec / 3600.0)

        return friction_kwh + potential_kwh + kin_kwh + aux_energy_kwh