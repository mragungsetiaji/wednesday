from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from ..levels import Level
from ..structure import Context


@dataclass(frozen=True)
class DetectorParams:
    """Settings shared by all detectors (one object so the CLI/.env/API stay in sync)."""

    swing_length: int = 5  # bars each side for swing highs/lows (external structure)
    zone: Literal["wick", "body"] = "wick"  # OB zone: full candle range or open/close
    mitigation: Literal["close", "wick"] = "close"  # what invalidates an OB
    eq_tolerance: float = 0.1  # equal highs/lows: max price gap as a multiple of ATR(14)
    idm_length: int = 2  # bars each side for the internal swings used by inducement


class Detector(ABC):
    """A detector turns closed candles of one timeframe into :class:`Level` objects.

    Return every level found in the window, active or ended; set ``ended_time``
    on levels that were mitigated/swept so the scanner can report recent events.
    """

    name: str  # registry key, also Level.detector
    title: str  # display name

    def __init__(self, params: DetectorParams):
        self.params = params

    @abstractmethod
    def detect(self, ctx: Context) -> list[Level]: ...
