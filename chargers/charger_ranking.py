"""Logik für das Bewerten und Filtern von Ladestationen."""

import math
import json
import os
from typing import Any, Dict, List, Optional

from app_config import get_env_float
from chargers.charge_points import is_within_window_radius
from chargers.chargeindex_client import dedup_chargers
from physics.charging_physics import calculate_charging_time_min
from routing.charging_config import compute_smart_target_soc
from routing.routing_engine import compute_route_energy_kwh
from routing.valhalla_route import (
    ValhallaRouteError,
    get_route_via_intermediate,
)
from vehicles import VehicleSpecs

# Default-Preis, wenn weder ChargeIndex noch operator_prices.json einen Wert liefern.
# Realistischer DE-HPC-Schnitt 2026 (vorher 0.55).
DEFAULT_FALLBACK_PRICE_EUR_KWH = 0.89

# Werte fuer ``price_source`` im Charger-Output
PRICE_SOURCE_LIVE = "chargeindex_live"
PRICE_SOURCE_MANUAL = "manual_override"
PRICE_SOURCE_FALLBACK = "default_fallback"

# Wie viele Charger nach Stufe-1-Pre-Ranking durch den teuren Valhalla-Pass
# (Stufe 2) laufen. Top-K=20 ist ein guter Puffer: die 5 angezeigten
# Alternativen sind sehr robust gegen Pre-Ranking-Schaetzfehler.
PRECISE_RANK_TOP_K = 20

# Zusatz-Bypass fuer Stufe 2: Charger mit einem bekannten Preis unter dieser
# Schwelle werden zusaetzlich zu den Top-K durch die Praezise-Routing-Stufe
# geschickt — auch wenn sie im Cheap-Score nicht in die Top-K geschafft haben.
# Damit gehen guenstige Sparfuchs-Optionen nie verloren, nur weil sie
# z.B. einen kleinen Umweg haben. Charger mit price_source="default_fallback"
# (also unbekannter Preis) werden NICHT durchgewunken — der 0.89-Fallback ist
# eh ueber der Schwelle, und ein unklarer Preis ist kein guter Grund fuer
# einen zusaetzlichen Valhalla-Call.
CHEAP_PRICE_BYPASS_EUR_KWH = 0.50

# Approx-Parameter fuer Stufe 1 (kein Valhalla-Call)
_AVG_DETOUR_SPEED_KMH = 50.0      # typische Abfahrtsstrasse
_DETOUR_ROUGHNESS = 1.3           # Luftlinie -> echte Fahrstrecke
_ROUGH_CONSUMPTION_KWH_PER_KM = 0.18  # Tesla M3 LR Autobahn-Durchschnitt


def load_operator_prices(filepath="operator_prices.json") -> dict:
    """Lädt die manuellen Ad-Hoc-Preise aus einer lokalen JSON-Datei."""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Warnung: Konnte operator_prices.json nicht lesen: {e}")
    return {"default": DEFAULT_FALLBACK_PRICE_EUR_KWH}  # Fallback, falls Datei fehlt

def _resolve_price(
    charger: Dict[str, Any],
    custom_prices: Dict[str, Any],
    default_price: float,
) -> tuple[float, str, str]:
    """Bestimmt Preis + Quelle + Operator-Name fuer einen Charger.

    Returns: (price_per_kwh, price_source, operator_name)
    """
    operator = str(charger.get("operator_name") or charger.get("operator") or "Unbekannt")
    op_lower = operator.lower()

    # Stufe 1: manuelle JSON-Overrides
    for key, price in custom_prices.items():
        if key != "default" and key.lower() in op_lower:
            return float(price), PRICE_SOURCE_MANUAL, operator

    # Stufe 2: Live-Preis aus der ChargeIndex-API
    api_price = charger.get("price_per_kwh")
    if api_price is not None and float(api_price) > 0.0:
        return float(api_price), PRICE_SOURCE_LIVE, operator

    # Stufe 3: Default-Fallback
    return default_price, PRICE_SOURCE_FALLBACK, operator


def _build_score_dict(
    charger: Dict[str, Any],
    *,
    vehicle: VehicleSpecs,
    arrival_kwh: float,
    energy_to_charger_kwh: float,
    detour_min: float,
    price_per_kwh: float,
    price_source: str,
    operator: str,
    price_weight_factor: float,
    target_kwh_cap: float,
    is_precise: bool,
) -> Optional[Dict[str, Any]]:
    """Baut das angereicherte Ranking-Dict fuer einen einzelnen Charger.

    Gibt ``None`` zurueck, wenn der Charger keine valide ``max_power_kw`` hat.
    """
    max_kw = float(charger.get("max_power_kw") or 0.0)
    if max_kw <= 0.0:
        return None

    battery = vehicle.battery_capacity_kwh
    arrival_soc = arrival_kwh / battery if battery > 0 else 0.0
    smart_target_soc = compute_smart_target_soc(arrival_soc)
    per_charger_target_kwh = min(battery * smart_target_soc, target_kwh_cap)
    kwh_to_charge = max(0.0, per_charger_target_kwh - arrival_kwh)

    charging_time_min = calculate_charging_time_min(
        vehicle=vehicle,
        start_kwh=arrival_kwh,
        target_kwh=per_charger_target_kwh,
        station_max_kw=max_kw,
    )
    estimated_cost = price_per_kwh * kwh_to_charge

    time_pain_min = detour_min + charging_time_min
    safe_kwh = max(1.0, kwh_to_charge)
    score = (time_pain_min / safe_kwh) + (price_per_kwh * price_weight_factor)

    enriched = dict(charger)
    enriched["operator_name"] = operator
    enriched["detour_min"] = detour_min
    enriched["charging_time_min"] = charging_time_min
    enriched["estimated_cost"] = estimated_cost
    enriched["price_per_kwh_used"] = price_per_kwh
    enriched["price_source"] = price_source
    enriched["kwh_to_charge"] = kwh_to_charge
    enriched["arrival_kwh_est"] = arrival_kwh
    enriched["energy_to_charger_kwh"] = energy_to_charger_kwh
    enriched["target_kwh_est"] = per_charger_target_kwh
    enriched["soc_at_arrival_pct"] = round(arrival_soc * 100.0, 1)
    enriched["soc_after_charge_pct"] = round(smart_target_soc * 100.0, 1)
    enriched["score"] = score
    enriched["scoring_stage"] = "precise" if is_precise else "cheap"
    return enriched


def _score_cheap(
    charger: Dict[str, Any],
    *,
    vehicle: VehicleSpecs,
    window_start_loc: Dict[str, Any],
    window_start_soc: float,
    custom_prices: Dict[str, Any],
    default_price: float,
    price_weight_factor: float,
    target_kwh_cap: float,
) -> Optional[Dict[str, Any]]:
    """Stufe 1: Cheap-Score ohne Valhalla-Call.

    Distanz vom Window-Start zum Charger: ChargeIndex liefert
    ``along_route_m`` (Position entlang der Polyline) und ``distance_m``
    (senkrechte Distanz). Daraus eine bessere Schaetzung als Euklid +
    Konstante:
      - Fahrt-km bis Charger = (along_route_m + distance_m * roughness) / 1000
      - Detour-Zeit = 2 * distance_m * roughness / detour_speed
    Verbrauch ist mit ``_ROUGH_CONSUMPTION_KWH_PER_KM`` flach approximiert
    — gut genug, um die schwachen Kandidaten auszusortieren.
    """
    along_m = charger.get("along_route_m")
    perp_m = charger.get("distance_m")
    if along_m is None or perp_m is None:
        # Charger ohne Routing-Metadaten kommen aus einer Nicht-along-route-Quelle.
        # Konservativ aussortieren — Variante B/C deckt sie eh nicht ab.
        return None

    along_km = float(along_m) / 1000.0
    perp_km = float(perp_m) / 1000.0

    # Fahrt bis Charger inkl. Abfahrt-Umweg
    drive_km = along_km + perp_km * _DETOUR_ROUGHNESS
    energy_to_charger_kwh = drive_km * _ROUGH_CONSUMPTION_KWH_PER_KM
    energy_at_window_start = vehicle.battery_capacity_kwh * window_start_soc
    arrival_kwh = max(0.0, energy_at_window_start - energy_to_charger_kwh)

    # Detour-Zeit: hin und zurueck vom Routen-Punkt zum Charger
    detour_min = 2.0 * perp_km * _DETOUR_ROUGHNESS / _AVG_DETOUR_SPEED_KMH * 60.0

    price_per_kwh, price_source, operator = _resolve_price(charger, custom_prices, default_price)
    return _build_score_dict(
        charger,
        vehicle=vehicle,
        arrival_kwh=arrival_kwh,
        energy_to_charger_kwh=energy_to_charger_kwh,
        detour_min=detour_min,
        price_per_kwh=price_per_kwh,
        price_source=price_source,
        operator=operator,
        price_weight_factor=price_weight_factor,
        target_kwh_cap=target_kwh_cap,
        is_precise=False,
    )


def _score_precise(
    charger: Dict[str, Any],
    *,
    vehicle: VehicleSpecs,
    window_start_lon_lat: List[float],
    window_end_lon_lat: List[float],
    window_start_soc: float,
    baseline_time_min: float,
    custom_prices: Dict[str, Any],
    default_price: float,
    price_weight_factor: float,
    target_kwh_cap: float,
) -> Optional[Dict[str, Any]]:
    """Stufe 2: Precise-Score via Valhalla 3-Punkt-Call mit Elevation.

    Liefert echte Distanz/Detour-Zeit und nutzt das volle Physik-Modell
    fuer die Energie von Window-Start bis Charger.
    """
    charger_lon = charger.get("longitude")
    charger_lat = charger.get("latitude")
    if charger_lon is None or charger_lat is None:
        return None

    try:
        trip = get_route_via_intermediate(
            window_start_lon_lat,
            [float(charger_lon), float(charger_lat)],
            window_end_lon_lat,
        )
    except ValhallaRouteError:
        return None  # Charger nicht erreichbar -> aussortieren

    leg0 = trip["legs"][0]
    leg0_route_data = {
        "decoded_shape": leg0["decoded_shape"],
        "segments": leg0["segments"],
    }
    energy_to_charger_kwh, _ = compute_route_energy_kwh(leg0_route_data, vehicle, "rank-precise")

    energy_at_window_start = vehicle.battery_capacity_kwh * window_start_soc
    arrival_kwh = max(0.0, energy_at_window_start - energy_to_charger_kwh)
    detour_min = max(0.0, trip["total_time_min"] - baseline_time_min)

    price_per_kwh, price_source, operator = _resolve_price(charger, custom_prices, default_price)
    return _build_score_dict(
        charger,
        vehicle=vehicle,
        arrival_kwh=arrival_kwh,
        energy_to_charger_kwh=energy_to_charger_kwh,
        detour_min=detour_min,
        price_per_kwh=price_per_kwh,
        price_source=price_source,
        operator=operator,
        price_weight_factor=price_weight_factor,
        target_kwh_cap=target_kwh_cap,
        is_precise=True,
    )


def rank_chargers(
    chargers: List[Dict[str, object]],
    charging_window_coords: List[List[float]],
    window_start_loc: Dict[str, object],
    window_end_loc: Dict[str, object],
    baseline_time_min: float,
    current_start: List[float],
    vehicle: VehicleSpecs,
    available_energy_kwh: float,
    target_kwh: float,
    user_price_weight: float | None = None,
    window_start_soc: float = 0.20,
    top_k: int = PRECISE_RANK_TOP_K,
) -> List[Dict[str, object]]:
    """Zweistufige Rangliste der Lader im Fenster.

    1. **Dedup**: Charger im 200m-Umkreis mit gleichem Operator, gleicher
       kW und gleichem Tarif werden zu einem Repraesentant zusammengefasst.
    2. **Stufe 1 — Cheap Pre-Rank**: Alle (deduplizierten) Charger werden
       mit ChargeIndex-Metadaten (``along_route_m``, ``distance_m``) und
       einer flachen 0.18 kWh/km-Annahme bewertet. Kein Valhalla-Call.
    3. **Stufe 2 — Precise Re-Rank**: Die Top ``top_k`` aus Stufe 1
       bekommen jeweils einen vollen Valhalla-3-Punkt-Call mit Elevation,
       aus dem das echte Detour und die echte Physik-Energie kommen.
    4. Es wird die nach Stufe-2-Score sortierte Liste zurueckgegeben.
       Die ersten 5 Eintraege landen in der API-Antwort als
       ``top_5_alternatives``.
    """
    if user_price_weight is not None:
        price_weight_factor = float(user_price_weight)
    else:
        price_weight_factor = get_env_float("PRICE_TIME_WEIGHT_FACTOR", 3.0)

    custom_prices = load_operator_prices()
    default_price = float(custom_prices.get("default", DEFAULT_FALLBACK_PRICE_EUR_KWH))

    # --- 0. Dedup ---
    raw_count = len(chargers)
    chargers_deduped = dedup_chargers(chargers, distance_threshold_m=200.0)
    deduped_count = len(chargers_deduped)
    if raw_count > deduped_count:
        print(f"[rank_chargers] Dedup: {raw_count} Charger -> {deduped_count} Standorte "
              f"({raw_count - deduped_count} Duplikate zusammengefasst)")

    # --- 1. Stufe 1: Cheap Pre-Ranking ---
    cheap_ranked: List[Dict[str, Any]] = []
    for charger in chargers_deduped:
        lat = charger.get("latitude")
        lon = charger.get("longitude")
        if lat is None or lon is None:
            continue
        charger_lat = float(lat)
        charger_lon = float(lon)

        # Ping-Pong-Schutz: nicht den Lader auswaehlen, an dem wir gerade stehen
        if abs(charger_lat - current_start[1]) < 0.005 and abs(charger_lon - current_start[0]) < 0.005:
            continue

        # Radius-Filter: Lader muss in der Naehe der Fenster-Polylinie liegen
        if not is_within_window_radius(charger_lat, charger_lon, charging_window_coords):
            continue

        scored = _score_cheap(
            charger,
            vehicle=vehicle,
            window_start_loc=window_start_loc,
            window_start_soc=window_start_soc,
            custom_prices=custom_prices,
            default_price=default_price,
            price_weight_factor=price_weight_factor,
            target_kwh_cap=target_kwh,
        )
        if scored is not None:
            cheap_ranked.append(scored)

    if not cheap_ranked:
        raise ValueError("Kein geeigneter Ladestopp im Lade-Fenster gefunden.")

    cheap_ranked.sort(key=lambda item: float(item.get("score", float("inf"))))

    # --- 2. Stufe 2: Precise Re-Ranking fuer Top K + Cheap-Price-Bypass ---
    top_k_candidates = cheap_ranked[:max(1, top_k)]

    # Sparfuchs-Bypass: zusaetzliche Charger aus dem Rest der Stufe-1-Liste,
    # die einen bekannten Preis unter CHEAP_PRICE_BYPASS_EUR_KWH haben.
    # Damit landen guenstige Stationen nie unter dem Tisch, nur weil sie im
    # Score-Ranking knapp ausserhalb der Top-K liegen (z.B. groesserer Umweg).
    bypass_candidates: List[Dict[str, Any]] = []
    for cand in cheap_ranked[max(1, top_k):]:
        if cand.get("price_source") == PRICE_SOURCE_FALLBACK:
            continue  # unbekannter Preis -> kein Bypass
        price = cand.get("price_per_kwh_used")
        if price is None:
            continue
        if float(price) < CHEAP_PRICE_BYPASS_EUR_KWH:
            bypass_candidates.append(cand)

    if bypass_candidates:
        print(f"[rank_chargers] +{len(bypass_candidates)} Sparfuchs-Bypass "
              f"(Preis < {CHEAP_PRICE_BYPASS_EUR_KWH} EUR/kWh, ausserhalb Top-{top_k})")
        top_k_candidates = top_k_candidates + bypass_candidates

    window_start_lon_lat = [
        float(window_start_loc["lon"]),
        float(window_start_loc["lat"]),
    ]
    window_end_lon_lat = [
        float(window_end_loc["lon"]),
        float(window_end_loc["lat"]),
    ]

    precise_ranked: List[Dict[str, Any]] = []
    for cand in top_k_candidates:
        scored = _score_precise(
            cand,
            vehicle=vehicle,
            window_start_lon_lat=window_start_lon_lat,
            window_end_lon_lat=window_end_lon_lat,
            window_start_soc=window_start_soc,
            baseline_time_min=baseline_time_min,
            custom_prices=custom_prices,
            default_price=default_price,
            price_weight_factor=price_weight_factor,
            target_kwh_cap=target_kwh,
        )
        if scored is not None:
            precise_ranked.append(scored)

    if not precise_ranked:
        # Notnagel: kein Charger hat Stufe 2 ueberlebt (Valhalla unerreichbar).
        # Cheap-Liste zurueckgeben, damit die App nicht ohne Optionen dasteht.
        print("[rank_chargers] WARN: Stufe 2 lieferte 0 Charger, fallback auf Stufe-1-Ranking")
        ranked = cheap_ranked
    else:
        precise_ranked.sort(key=lambda item: float(item.get("score", float("inf"))))
        ranked = precise_ranked

    print(f"[rank_chargers] {deduped_count} Standorte -> {len(cheap_ranked)} Stufe 1 -> "
          f"{len(top_k_candidates)} Kandidaten (Top {top_k} + {len(bypass_candidates)} Bypass) -> "
          f"{len(precise_ranked)} Stufe 2")

    # --- TERMINAL-AUSGABE ---
    print(f"\n\n{'='*95}")
    print(f"🏆 TOP-20 LADESÄULEN-RANKING (Gewichtung: 1€ = {price_weight_factor} Min)")
    if price_weight_factor < 0.5:
        print(f"⚠️  WARNUNG: price_weight_factor={price_weight_factor} ist sehr niedrig — "
              f"Preis spielt im Ranking fast keine Rolle.")
        print(f"   Erwartet wird typisch 1.0 (zeitorientiert) bis 3.0 (preisorientiert).")
        print(f"   Pruefe den 'price_time_weight'-Wert im /plan-route-Request.")
    print(f"{'='*95}")

    source_glyph = {
        PRICE_SOURCE_LIVE: "live",
        PRICE_SOURCE_MANUAL: "manual",
        PRICE_SOURCE_FALLBACK: "FALLBACK",
    }
    print_limit = min(20, len(ranked))
    for i, charger in enumerate(ranked[:print_limit], 1):
        operator = charger.get("operator_name", "Unbekannt")
        kw = charger.get("max_power_kw", 0)
        score = charger.get("score", 0.0)
        detour = charger.get("detour_min", 0.0)
        charge_time = charger.get("charging_time_min", 0.0)
        cost = charger.get("estimated_cost", 0.0)
        price_kwh = charger.get("price_per_kwh_used", DEFAULT_FALLBACK_PRICE_EUR_KWH)
        price_src = source_glyph.get(str(charger.get("price_source")), "?")
        kwh_need = charger.get("kwh_to_charge", 0.0)
        arr_pct = float(charger.get("soc_at_arrival_pct") or 0.0)
        after_pct = float(charger.get("soc_after_charge_pct") or 0.0)
        merged = int(charger.get("merged_count") or 1)
        stage = str(charger.get("scoring_stage") or "?")

        # Score-Aufschluesselung: time_pain/kwh + cost_pain/kwh
        safe_kwh = max(1.0, float(kwh_need))
        time_pain = (float(detour) + float(charge_time)) / safe_kwh
        cost_pain = float(price_kwh) * price_weight_factor

        medal = "🥇 " if i == 1 else "🥈 " if i == 2 else "🥉 " if i == 3 else f"{i:>2}."
        merged_str = f" [{merged}x EVSE]" if merged > 1 else ""

        print(f"{medal} {operator} ({kw} kW){merged_str} | {arr_pct:.1f}% → {after_pct:.1f}% | stage={stage}")
        print(f"      Score: {score:.3f} = Zeit {time_pain:.3f} + Preis {cost_pain:.3f}")
        print(f"      Bedarf: {kwh_need:.1f} kWh | Ladezeit {charge_time:.1f} min | Umweg {detour:.1f} min")
        print(f"      Preis: {cost:.2f} € ({price_kwh:.3f} €/kWh, {price_src})")
        print(f"{'-'*95}")
    if len(ranked) > print_limit:
        print(f"... + {len(ranked) - print_limit} weitere unterhalb der Top 20")
    print("\n")

    return ranked


# Backward-compat-Alias: aelterer Code, der nur den besten Lader wollte.
def get_best_charger(*args, **kwargs) -> Dict[str, object]:
    """Deprecated. Bevorzugt ``rank_chargers(...)[0]`` verwenden."""
    ranked = rank_chargers(*args, **kwargs)
    return ranked[0]