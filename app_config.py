"""Zentrale Konfiguration via .env und Umgebungsvariablen."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(dotenv_path: Path | None = None) -> None:
    """Laedt KEY=VALUE Eintraege aus einer .env-Datei in os.environ."""
    env_path = dotenv_path or (Path(__file__).resolve().parent / ".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def get_env_str(name: str, default: str = "") -> str:
    """Liest einen String-Wert aus der Umgebung."""
    value = os.getenv(name)
    return value if value is not None else default


def get_env_int(name: str, default: int) -> int:
    """Liest einen Integer-Wert aus der Umgebung mit Fallback."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def get_env_float(name: str, default: float) -> float:
    """Liest einen Float-Wert aus der Umgebung mit Fallback."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


load_dotenv()
