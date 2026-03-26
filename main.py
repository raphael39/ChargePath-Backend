"""
ChargeRout FastAPI Backend.
Haupt-Einstiegspunkt (Controller) für die API.
"""

from fastapi import FastAPI, HTTPException
from models import RouteRequest
from vehicles import get_vehicle
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