"""Unit conversion for calculator inputs. Height in centimetres and height in metres must not be mixed."""
from __future__ import annotations

_FACTORS = {
    ("cm", "m"): 0.01,
    ("m", "cm"): 100.0,
    ("mm", "m"): 0.001,
    ("m", "mm"): 1000.0,
    ("kg", "g"): 1000.0,
    ("g", "kg"): 0.001,
}


def convert(value: float, src: str, dst: str) -> float:
    if src == dst:
        return float(value)
    factor = _FACTORS.get((src, dst))
    if factor is None:
        raise ValueError(f"Cannot convert {src} to {dst}")
    return float(value) * factor
