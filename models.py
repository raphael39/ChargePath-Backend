"""Datenmodelle und Typ-Definitionen für die API."""

from pydantic import BaseModel, Field

class RouteRequest(BaseModel):
    # Basis-Routendaten
    start_lon: float
    start_lat: float
    dest_lon: float
    dest_lat: float
    
    # Fahrzeug & Start-Akku
    vehicle_id: str = Field(default="tesla_model_3_lr")
    initial_soc: float = Field(default=1.0, ge=0.01, le=1.0)

    # --- Erweiterte Routen-Parameter aus dem Frontend-Menü ---
    target_soc: float = Field(default=0.10, description="Gewünschter SoC am finalen Ziel")
    window_start_soc: float = Field(default=0.20, description="Ab diesem SoC Ladesäulen suchen")
    window_end_soc: float = Field(default=0.05, description="Späteste Ankunft am Lader (Reserve)")
    
    price_time_weight: float | None = Field(default=3.0, description="1 Euro Aufpreis = X Minuten Schmerz")
    
    consumption_factor: float = Field(default=1.0, description="Verbrauchsmultiplikator (z.B. 1.1 für +10%)")
    speed_factor: float = Field(default=1.0, description="Geschwindigkeitsmultiplikator")
    degradation: float = Field(default=0.0, description="Batterie-Degradation (z.B. 0.05 für 5%)")