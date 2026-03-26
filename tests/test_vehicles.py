from vehicles import get_vehicle

def test_get_existing_vehicle():
    """Prüft, ob der Tesla korrekt geladen wird."""
    vehicle = get_vehicle("tesla_model_y_lr")
    assert vehicle.name == "Tesla Model Y Long Range"
    assert vehicle.battery_capacity_kwh == 78.1

def test_get_unknown_vehicle_fallback():
    """Prüft, ob bei Quatsch-Eingaben das Standard-Auto kommt."""
    vehicle = get_vehicle("fliegender_teppich")
    assert vehicle.id == "generic_ev"