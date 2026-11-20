from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Quote:
    """Снимок котировки инструмента в момент опроса."""

    market: str  # ключ рынка из конфига, напр. "shares"
    asset_class: str  # shares | bonds | futures
    board: str
    secid: str
    name: str
    last: float | None
    prev: float | None  # опорная цена: закрытие / расчётная цена прошлого дня
    open: float | None = None
    high: float | None = None
    low: float | None = None
    value_today: float | None = None  # оборот за день, руб.
    num_trades: int | None = None
    ts: datetime | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.board, self.secid)

    @property
    def change_pct(self) -> float | None:
        if not self.last or not self.prev:
            return None
        return (self.last / self.prev - 1.0) * 100.0

    @property
    def log_change(self) -> float | None:
        if not self.last or not self.prev or self.last <= 0 or self.prev <= 0:
            return None
        return math.log(self.last / self.prev)

    @property
    def range_pct(self) -> float | None:
        """Внутридневной диапазон (high-low) в % от опорной цены."""
        if self.high is None or self.low is None or not self.prev:
            return None
        return (self.high - self.low) / self.prev * 100.0


@dataclass(slots=True)
class Baseline:
    """Статистика инструмента по дневной истории."""

    secid: str
    n: int  # число дневных доходностей
    sigma_daily: float  # stdev дневных лог-доходностей
    avg_value: float  # средний дневной оборот, руб.
    atr_pct: float  # средний (high-low)/close, %

    @property
    def ann_vol_pct(self) -> float:
        return self.sigma_daily * math.sqrt(252) * 100.0


@dataclass(slots=True)
class Reason:
    kind: str  # STRONG_DAY | FAST_MOVE | ANOMALY_DAY | ANOMALY_FAST | VOLUME_SPIKE
    text: str
    anomaly: bool  # True — аномалия (относительно собственной волатильности/объёма)


@dataclass(slots=True)
class Alert:
    quote: Quote
    reasons: list[Reason] = field(default_factory=list)
    ts: datetime | None = None

    @property
    def is_anomaly(self) -> bool:
        return any(r.anomaly for r in self.reasons)

    @property
    def kinds(self) -> list[str]:
        return [r.kind for r in self.reasons]
