"""Detector registry.

To add a detector: subclass :class:`Detector` (reuse ``ctx.structure(...)`` for
swings/breaks instead of recomputing them), return :class:`Level` objects, and
add the class to ``REGISTRY``. The scanner, console report, API and dashboard
pick it up by its ``name``.
"""

from __future__ import annotations

from .base import Detector, DetectorParams
from .inducement import InducementDetector
from .liquidity import LiquidityDetector
from .orderblock import OrderBlockDetector

REGISTRY: dict[str, type[Detector]] = {
    cls.name: cls for cls in (OrderBlockDetector, LiquidityDetector, InducementDetector)
}

DEFAULT_DETECTORS = tuple(REGISTRY)


def parse_detectors(names: str | list[str]) -> tuple[str, ...]:
    if isinstance(names, str):
        names = [n for n in names.split(",") if n.strip()]
    out = []
    for raw in names:
        key = raw.strip().lower()
        if key not in REGISTRY:
            raise ValueError(f"Unknown detector {raw!r}; choose from {', '.join(REGISTRY)}")
        if key not in out:
            out.append(key)
    return tuple(out)


def build_detectors(names: tuple[str, ...], params: DetectorParams) -> list[Detector]:
    return [REGISTRY[n](params) for n in names]


__all__ = ["REGISTRY", "DEFAULT_DETECTORS", "Detector", "DetectorParams", "build_detectors", "parse_detectors"]
