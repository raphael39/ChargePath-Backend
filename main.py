"""
ChargeRout FastAPI Backend.
Haupt-Einstiegspunkt (Controller) für die API.
"""

# WICHTIG: http_cache MUSS vor allen Modulen importiert werden, die
# ``requests`` verwenden, damit der globale Patch greift.
from http_cache import install_http_cache, get_cache_stats, clear_http_cache
install_http_cache()

from fastapi import FastAPI, HTTPException, Query
from models import RouteRequest
from vehicles import get_vehicle, list_vehicles, VEHICLE_DB
from routing.route_planner import calculate_ev_route

app = FastAPI(title="ChargeRout API")

@app.post("/plan-route")
async def plan_route(request: RouteRequest) -> dict:
    """
    Nimmt Start, Ziel und Fahrzeug-ID entgegen, 
    plant die komplette Route inkl. Ladestopps und gibt das fertige JSON zurück.
    """
    try:
        # 1. Das gewünschte Fahrzeug aus der Datenbank laden
        vehicle = get_vehicle(request.vehicle_id)

        # ==========================================
        # 🔍 DEBUG PRINT (Kompakt)
        # ==========================================
        print("\n" + "="*50)
        print(f"🚀 ROUTE: [{request.start_lat}, {request.start_lon}] ➔ [{request.dest_lat}, {request.dest_lon}]")
        print(f"🚙 AUTO:  {vehicle.name} | 🔋 SOC: {request.initial_soc * 100:.0f}% | ⚖️ Faktor: {request.price_time_weight or '.env'}")
        print("="*50 + "\n")
        # ==========================================

        # 2. Die gesamte Route (inkl. Ladeschleife und Physik) berechnen lassen
        response_data = calculate_ev_route(request, vehicle)

        # 3. Dem iOS-Frontend die fertigen Daten servieren
        return response_data

    except Exception as exc:
        print(f"\n[ERROR] Backend Absturz: {exc}\n")
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/cache/stats")
def cache_stats() -> dict:
    """Zeigt, wie viele Eintraege aktuell im HTTP-Cache liegen.

    Praktisch zum Debuggen, ob Cache-Hits wirklich greifen — nach dem ersten
    /plan-route-Call sollte ``cached_responses`` deutlich gestiegen sein.
    """
    return get_cache_stats()


@app.post("/cache/clear")
def cache_clear() -> dict:
    """Komplett-Reset des Caches. Naechster /plan-route-Call geht wieder live."""
    clear_http_cache()
    return {"cleared": True}


@app.get("/vehicles")
def vehicles_list(
    architecture_voltage: int | None = Query(
        default=None,
        description="Optional filtern auf 400 oder 800 (V-Architektur).",
    ),
    min_battery_kwh: float | None = Query(
        default=None,
        description="Optional: nur Vehicles mit nutzbarer Akku-Kapazitaet >= diesem Wert.",
    ),
) -> dict:
    """Liste aller verfuegbaren Fahrzeuge fuer die Mobile-App-Vehicle-Picker-UI.

    Liefert die kompakte Variante (id, name, Modeljahr, Akku-kWh, max kW, Volt).
    Legacy-Aliase wie ``tesla_model_3_lr`` sind ausgefiltert — angezeigt
    werden nur kanonische, versionierte IDs (z.B. ``tesla_model_3_lr_2024_highland``).

    Optionale Filter:
    - ``architecture_voltage=800`` zeigt nur 800V-Plattformen (Ioniq 5, EV6, Q6 e-tron, ...)
    - ``min_battery_kwh=70`` blendet Kleinwagen aus (Spring, Zoe, ID.3 Pure)
    """
    vehicles = list_vehicles()

    if architecture_voltage is not None:
        vehicles = [v for v in vehicles if v["architecture_voltage"] == architecture_voltage]
    if min_battery_kwh is not None:
        vehicles = [v for v in vehicles if v["battery_capacity_kwh"] >= min_battery_kwh]

    # Stabil nach Markenname + Modelljahr sortieren — angenehm fuer Picker-UI
    vehicles.sort(key=lambda v: (v["name"], v["model_year_range"]))

    return {
        "count": len(vehicles),
        "vehicles": vehicles,
    }


@app.get("/vehicles/{vehicle_id}")
def vehicle_detail(vehicle_id: str) -> dict:
    """Vollstaendige Physik-Specs eines Fahrzeugs (alle ``VehicleSpecs``-Felder).

    Praktisch fuer einen "Details"-View in der App, oder zum Debuggen wieso
    Auto X eine bestimmte Reichweite-Schaetzung bekommt.

    Liefert 404 wenn die ID weder in der kanonischen DB noch als Legacy-Alias
    bekannt ist.
    """
    if vehicle_id not in VEHICLE_DB:
        raise HTTPException(status_code=404, detail=f"Unbekannte vehicle_id: {vehicle_id}")

    v = VEHICLE_DB[vehicle_id]
    return {
        "id": v.id,
        "name": v.name,
        "model_year_range": v.model_year_range,
        "battery_capacity_kwh": v.battery_capacity_kwh,
        "battery_capacity_gross_kwh": v.battery_capacity_gross_kwh,
        "mass_kg": v.mass_kg,
        "drag_coefficient_cw": v.drag_coefficient_cw,
        "frontal_area_m2": v.frontal_area_m2,
        "rolling_resistance_cr": v.rolling_resistance_cr,
        "aux_power_kw": v.aux_power_kw,
        "recuperation_efficiency": v.recuperation_efficiency,
        "drivetrain_efficiency": v.drivetrain_efficiency,
        "max_charge_power_kw": v.max_charge_power_kw,
        "charging_curve_10pct": v.charging_curve_10pct,
        "architecture_voltage": v.architecture_voltage,
        "notes": v.notes,
    }