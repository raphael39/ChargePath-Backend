"""Datenmodelle und Typ-Definitionen für die API."""

from pydantic import BaseModel, Field

class RouteRequest(BaseModel):
    """Das Schema für eine eingehende Routen-Anfrage der App."""
    start_lon: float
    start_lat: float
    dest_lon: float
    dest_lat: float
    
    # --- NEU ---
    # Wir fügen die vehicle_id hinzu. Wenn die iOS-App nichts mitschickt, 
    # fällt FastAPI automatisch auf unser "generic_ev" zurück.
    vehicle_id: str = Field(
        default="tesla_model_3_lr", 
        description="Die ID des Fahrzeugs aus der internen Datenbank (z.B. 'tesla_model_y_lr')"
    )

    # --- NEU: Start-Akkustand ---
    initial_soc: float = Field(
        default=1.0, 
        ge=0.01,  # Greater than or equal to 1% (Schutz vor "Auto ist schon leer")
        le=1.0,   # Less than or equal to 100%
        description="Akkustand beim Start der Route (0.01 bis 1.0). Standard ist 1.0 (100%)."
    )

    # --- NEU: Der Geizfaktor aus der App ---
    price_time_weight: float | None = Field(
        default=None, 
        description="Faktor: 1 Euro Aufpreis = X Minuten Schmerz. Überschreibt die .env, falls gesetzt."
    )