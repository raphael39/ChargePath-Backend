"""Konfiguration der Lade-Strategie.

Alle Schwellwerte und Faktoren des Routen-Planers werden aus der .env
gelesen. So lassen sich Verhalten und Parameter aendern, ohne Code
anzufassen. Die Defaults entsprechen den frueher hartkodierten Werten
in `route_planner.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app_config import get_env_float, get_env_int


@dataclass(frozen=True)
class ChargingConfig:
    """Strategie-Parameter fuer die Lade-Schleife des Route Planners."""

    # SoC-Reserve am Ziel (Anteil der Batterie, der als Puffer bleiben soll)
    target_reserve_percent: float

    # Sicherheitsfaktor fuer geschaetzte Ladezeiten
    charge_time_buffer_factor: float

    # Mindest-Delta-SoC pro Ladestopp (z.B. 0.40 = +40 Prozentpunkte)
    min_charge_delta_soc: float

    # Maximaler SoC, auf den geladen wird (Ladekurve flacht oben ab)
    max_charge_soc: float

    # Schwelle fuer "niedriger SoC" beim Bonus-Laden
    low_soc_threshold: float

    # Maximaler zusaetzlicher SoC-Bonus, wenn Ankunfts-SoC sehr niedrig ist
    low_soc_max_bonus: float

    # Notbremse: maximale Anzahl Ladestopps, bevor abgebrochen wird
    max_stops: int


def load_charging_config() -> ChargingConfig:
    """Erstellt eine ChargingConfig aus den aktuellen Env-Variablen."""
    return ChargingConfig(
        target_reserve_percent=get_env_float("TARGET_RESERVE_PERCENT", 0.10),
        charge_time_buffer_factor=get_env_float("CHARGE_TIME_BUFFER_FACTOR", 1.15),
        min_charge_delta_soc=get_env_float("MIN_CHARGE_DELTA_SOC", 0.40),
        max_charge_soc=get_env_float("MAX_CHARGE_SOC", 0.80),
        low_soc_threshold=get_env_float("LOW_SOC_THRESHOLD", 0.20),
        low_soc_max_bonus=get_env_float("LOW_SOC_MAX_BONUS", 0.15),
        max_stops=get_env_int("MAX_STOPS", 25),
    )


# Modul-weit gecachte Default-Instanz. Wird einmal beim Import erstellt.
CHARGING_CONFIG: ChargingConfig = load_charging_config()


def compute_smart_target_soc(arrival_soc: float, config: ChargingConfig = CHARGING_CONFIG) -> float:
    """Berechnet den 'smarten' Lade-Ziel-SoC fuer einen gegebenen Ankunfts-SoC.

    Logik:
    - Basis: ``arrival_soc + min_charge_delta_soc`` (typ. +40 Prozentpunkte)
    - Bonus bei sehr niedrigem Akku: linear interpoliert bis
      ``low_soc_max_bonus``, weil die unteren SOC-Bereiche schneller laden
      und sich der Bonus zeitlich kaum bemerkbar macht.
    - Cap: ``max_charge_soc`` (typ. 0.80) — nie hoeher laden.

    Beispiele (Defaults):
        arrival 5 %  -> base 45 %, bonus 11.25 % -> smart 56.25 %
        arrival 14 % -> base 54 %, bonus 4.5  % -> smart 58.5 %
        arrival 30 % -> base 70 %, bonus 0    % -> smart 70.0 %
        arrival 50 % -> base 90 %, capped     -> smart 80.0 %
    """
    base_target = arrival_soc + config.min_charge_delta_soc
    if config.low_soc_threshold > 0:
        low_soc_bonus = max(
            0.0,
            (config.low_soc_threshold - arrival_soc) / config.low_soc_threshold,
        ) * config.low_soc_max_bonus
    else:
        low_soc_bonus = 0.0
    return min(base_target + low_soc_bonus, config.max_charge_soc)
