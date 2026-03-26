"""Fahrzeugdatenbank und Spezifikationen für ChargeRout."""

from dataclasses import dataclass
from typing import Dict, List

@dataclass
class VehicleSpecs:
    """Repräsentiert die physikalischen und elektrischen Eigenschaften eines E-Autos."""
    id: str
    name: str
    battery_capacity_kwh: float
    mass_kg: float
    drag_coefficient_cw: float
    frontal_area_m2: float
    rolling_resistance_cr: float
    aux_power_kw: float
    recuperation_efficiency: float
    drivetrain_efficiency: float  
    max_charge_power_kw: float
    # Ladeleistung in kW (Durchschnitt) für die Blöcke: 
    # 0-10%, 10-20%, 20-30%, 30-40%, 40-50%, 50-60%, 60-70%, 70-80%, 80-90%, 90-100%
    charging_curve_10pct: List[float]


# Unsere interne Datenbank
VEHICLE_DB: Dict[str, VehicleSpecs] = {
    "generic_ev": VehicleSpecs(
        id="generic_ev",
        name="Generisches E-Auto (Standard)",
        battery_capacity_kwh=75.0,
        mass_kg=1800.0,
        drag_coefficient_cw=0.22,
        frontal_area_m2=2.22,
        rolling_resistance_cr=0.01,
        aux_power_kw=1.5,
        recuperation_efficiency=0.70,
        drivetrain_efficiency=0.92, 
        max_charge_power_kw=150.0,
        # Solides, durchschnittliches Ladeverhalten (Peak bei 150kW, sanfter Abfall)
        charging_curve_10pct=[120.0, 150.0, 150.0, 130.0, 110.0, 90.0, 70.0, 50.0, 35.0, 20.0]
    ),
    
    "tesla_model_y_lr": VehicleSpecs(
        id="tesla_model_y_lr",    
        name="Tesla Model Y Long Range",
        battery_capacity_kwh=78.1,
        mass_kg=1979.0,
        drag_coefficient_cw=0.23,
        frontal_area_m2=2.54,
        rolling_resistance_cr=0.01,
        aux_power_kw=0.7,
        recuperation_efficiency=0.75,
        drivetrain_efficiency=0.95,  
        max_charge_power_kw=250.0,
        # Tesla: Extrem hoher Peak zu Beginn, dann steiler, linearer Abfall
        charging_curve_10pct=[250.0, 250.0, 210.0, 170.0, 130.0, 100.0, 80.0, 60.0, 40.0, 20.0]
    ),
    
    "vw_id4_pro": VehicleSpecs(
        id="vw_id4_pro",
        name="VW ID.4 Pro",
        battery_capacity_kwh=77.0,
        mass_kg=2124.0,
        drag_coefficient_cw=0.28,
        frontal_area_m2=2.56,
        rolling_resistance_cr=0.012,
        aux_power_kw=1.5,
        recuperation_efficiency=0.70,
        drivetrain_efficiency=0.88,  
        max_charge_power_kw=135.0,
        # VW: Langes Plateau bei 135kW, danach ein treppenartiger Abstieg
        charging_curve_10pct=[120.0, 135.0, 135.0, 120.0, 100.0, 85.0, 70.0, 55.0, 40.0, 25.0]
    ),
    
    "tesla_model_3_lr": VehicleSpecs(
        id="tesla_model_3_lr",    
        name="Tesla Model 3 Long Range",
        battery_capacity_kwh=82.0,
        mass_kg=1800.0,
        drag_coefficient_cw=0.219,
        frontal_area_m2=2.20,
        rolling_resistance_cr=0.008,
        aux_power_kw=0.6,
        recuperation_efficiency=0.75,
        drivetrain_efficiency=0.95,  
        max_charge_power_kw=250.0,
        # Ähnlich wie Model Y, aber durch etwas größeren Akku minimal ausdauernder im mittleren Bereich
        charging_curve_10pct=[250.0, 250.0, 215.0, 175.0, 135.0, 105.0, 85.0, 60.0, 40.0, 20.0]
    ),
}

def get_vehicle(vehicle_id: str = "generic_ev") -> VehicleSpecs:
    """
    Holt die Fahrzeugdaten anhand der ID. 
    Fällt auf das Standardauto zurück, wenn die ID unbekannt ist.
    """
    return VEHICLE_DB.get(vehicle_id, VEHICLE_DB["generic_ev"])