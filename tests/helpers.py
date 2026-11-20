import math
import random
from datetime import date, timedelta

from moex_watcher.markets import MarketSpec


class FakeHistory:
    """Синтетическая дневная история: инструмент -> (σ, оборот). Выходные — пустые."""

    def __init__(self, instruments: dict[str, tuple[float, float]], seed: int = 1):
        self.instruments = instruments
        self.rng = random.Random(seed)
        self.calls: list[date] = []

    def history_day(self, spec: MarketSpec, day: date):
        self.calls.append(day)
        if day.weekday() >= 5:
            return []
        rows = []
        for secid, (sigma, value) in self.instruments.items():
            # цена — детерминированное случайное блуждание по дню
            r = random.Random(f"{secid}{day}")
            steps = [date(2026, 1, 1) + timedelta(days=k) for k in range((day - date(2026, 1, 1)).days + 1)]
            price = 100 * math.exp(sum(random.Random(f"{secid}{d}").gauss(0, sigma)
                                       for d in steps if d.weekday() < 5))
            rows.append({"BOARDID": spec.board, "SECID": secid, "OPEN": price, "HIGH": price * (1 + sigma),
                         "LOW": price * (1 - sigma), "CLOSE": price, "LEGALCLOSEPRICE": price,
                         "VALUE": value * r.uniform(0.8, 1.2)})
        return rows
